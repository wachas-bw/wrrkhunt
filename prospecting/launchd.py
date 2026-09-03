"""Generate and manage per-user macOS LaunchAgents."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import APP_HOME, LOG_DIR, REPO_ROOT, ensure_home, keychain_get
from .db import Database

AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
PREFIX = "ai.wrrk.prospecting"
RUNTIME_ROOT = APP_HOME / "launchd-runtime"
RUNTIME_REPO = RUNTIME_ROOT / "wrrkhunt"
RUNTIME_VENV = RUNTIME_REPO / ".venv"


def _find_user_executable(name: str) -> Path | None:
    """Resolve login-shell tools that launchd does not put on PATH."""
    found = shutil.which(name)
    if found:
        return Path(found).expanduser()
    nvm_root = Path(os.environ.get("NVM_DIR", str(Path.home() / ".nvm"))).expanduser()
    candidates = sorted(
        nvm_root.glob(f"versions/node/*/bin/{name}"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    return next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


def _launchd_environment() -> dict[str, str]:
    """Build a minimal deterministic environment with the user's NVM tools."""
    directories: list[str] = []
    executables = {
        name: _find_user_executable(name) for name in ("node", "mcporter")
    }
    for path in (Path(sys.executable).resolve(), *executables.values()):
        if path:
            parent = str(path.parent)
            if parent not in directories:
                directories.append(parent)
    for directory in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                      "/usr/sbin", "/sbin"):
        if directory not in directories:
            directories.append(directory)
    environment = {
        "WRRKHUNT_HOME": str(APP_HOME),
        "PATH": ":".join(directories),
    }
    if executables["mcporter"]:
        environment["WRRKHUNT_MCPORTER_BIN"] = str(executables["mcporter"])
    return environment


def _ensure_runtime_git_repository(path: Path) -> None:
    """Give Codex exec a private Git working tree without bypassing its safety check."""
    git = shutil.which("git") or "/usr/bin/git"
    result = subprocess.run(
        [git, "init", "--quiet", str(path)], capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "could not initialize launchd runtime Git repository")


def _copy_runtime_tree(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )


def _stage_runtime() -> None:
    """Stage launchd code outside macOS-protected Documents directories.

    Recent macOS releases can leave a background Python process waiting on a
    Files-and-Folders permission check when its interpreter or import path is
    beneath Documents.  The foreground CLI remains the source of truth; agents
    run a small private snapshot under Application Support instead.
    """
    ensure_home()
    RUNTIME_REPO.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("prospecting", "sources", "enrich"):
        _copy_runtime_tree(REPO_ROOT / name, RUNTIME_REPO / name)

    # `codex exec` intentionally refuses to run outside a Git working tree.
    # The staged snapshot is private application state, so initialize it as its
    # own repository instead of weakening Codex with --skip-git-repo-check.
    _ensure_runtime_git_repository(RUNTIME_REPO)

    config_source = REPO_ROOT.parent / "config"
    _copy_runtime_tree(config_source, RUNTIME_ROOT / "config")

    source_venv = REPO_ROOT / ".venv"
    runtime_python = RUNTIME_VENV / "bin" / "python"
    if not runtime_python.exists():
        if not (source_venv / "bin" / "python").exists():
            raise RuntimeError("run automation setup before installing LaunchAgents")
        _copy_runtime_tree(source_venv, RUNTIME_VENV)

    # Keep an existing free-credit token available to the staged discovery job
    # without ever placing it in Git or a plist.
    token_candidates = (
        (REPO_ROOT.parent / "jobhunt" / ".apify_token",
         RUNTIME_ROOT / "jobhunt" / ".apify_token"),
        (REPO_ROOT / ".apify_token", RUNTIME_REPO / ".apify_token"),
    )
    for source, destination in token_candidates:
        if source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(source, destination)
            destination.chmod(0o600)
            break


def _program() -> list[str]:
    venv_python = RUNTIME_VENV / "bin" / "python"
    python = str(venv_python if venv_python.exists() else Path(sys.executable).resolve())
    return [python, "-m", "prospecting"]


def _base(label: str, args: list[str]) -> dict[str, Any]:
    ensure_home()
    return {
        "Label": label,
        "ProgramArguments": _program() + args,
        "WorkingDirectory": str(RUNTIME_REPO),
        "EnvironmentVariables": _launchd_environment(),
        "StandardOutPath": str(LOG_DIR / f"{label}.out.log"),
        "StandardErrorPath": str(LOG_DIR / f"{label}.err.log"),
        "ProcessType": "Background",
    }


def definitions(*, include_delivery: bool = True) -> dict[str, dict[str, Any]]:
    weekdays_discover = [{"Weekday": day, "Hour": 7, "Minute": 30} for day in range(2, 7)]
    weekdays_prepare = [{"Weekday": day, "Hour": 9, "Minute": 0} for day in range(2, 7)]
    values = {
        f"{PREFIX}.dashboard": {**_base(f"{PREFIX}.dashboard", ["serve"]),
                                 "RunAtLoad": True, "KeepAlive": True,
                                 "ProcessType": "Interactive"},
        f"{PREFIX}.discover": {**_base(
            f"{PREFIX}.discover", ["discover", "--markets", "IN,AE,SG,GB,US", "--mix"]),
            "StartCalendarInterval": weekdays_discover},
        f"{PREFIX}.prepare": {**_base(f"{PREFIX}.prepare", ["prepare"]),
                               "StartCalendarInterval": weekdays_prepare},
    }
    if include_delivery:
        values[f"{PREFIX}.worker"] = {
            **_base(f"{PREFIX}.worker", ["worker"]),
            "RunAtLoad": True, "StartInterval": 300,
        }
        values[f"{PREFIX}.inbox"] = {
            **_base(f"{PREFIX}.inbox", ["inbox"]),
            "RunAtLoad": True, "StartInterval": 900,
        }
    return values


def install(load: bool = True) -> list[Path]:
    _stage_runtime()
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    uid = os.getuid()
    db = Database()
    db.initialize()
    connector_only = (
        db.channel("email")["credential_status"] == "gmail_connector"
        and not bool(keychain_get())
    )
    selected = definitions(include_delivery=not connector_only)
    omitted = set(definitions()) - set(selected)
    for label in omitted:
        stale = AGENT_DIR / f"{label}.plist"
        if stale.exists():
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(stale)],
                           capture_output=True, check=False)
            stale.unlink()
    for label, value in selected.items():
        path = AGENT_DIR / f"{label}.plist"
        path.write_bytes(plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True))
        paths.append(path)
        if load:
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)],
                           capture_output=True, check=False)
            result = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)],
                                    capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or f"could not load {label}")
    return paths


def uninstall() -> list[Path]:
    paths = []
    uid = os.getuid()
    for label in definitions():
        path = AGENT_DIR / f"{label}.plist"
        if path.exists():
            subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)],
                           capture_output=True, check=False)
            path.unlink()
            paths.append(path)
    return paths
