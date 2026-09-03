"""Runtime paths, policy defaults, and macOS Keychain access."""
from __future__ import annotations

import getpass
import json
import os
import subprocess
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
DEFAULT_HOME = Path.home() / "Library" / "Application Support" / "wrrkhunt"
APP_HOME = Path(os.environ.get("WRRKHUNT_HOME", DEFAULT_HOME)).expanduser().resolve()
DB_PATH = APP_HOME / "wrrkhunt.sqlite3"
LINKEDIN_PROFILE = APP_HOME / "browser-profile" / "linkedin"
META_PROFILE = APP_HOME / "browser-profile" / "meta"
LOG_DIR = APP_HOME / "automation-logs"

SENDER_EMAIL = "wachas@wrrk.ai"
SENDER_NAME = "Wachas"
SENDER_TITLE = "Founding engineer, wrrk.ai"
BOOKING_URL = "https://wrrk.ai/book/wrrkaidemo"

MARKET_POLICIES: dict[str, dict[str, Any]] = {
    "IN": {"enabled": True, "timezone": "Asia/Kolkata", "corporate_only": False},
    "AE": {"enabled": True, "timezone": "Asia/Dubai", "corporate_only": False},
    "SG": {"enabled": True, "timezone": "Asia/Singapore", "corporate_only": False},
    "GB": {"enabled": True, "timezone": "Europe/London", "corporate_only": True},
    # US is additionally gated on a configured postal address.
    "US": {"enabled": True, "timezone": "America/New_York", "corporate_only": False},
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "sender_email": SENDER_EMAIL,
    "sender_name": SENDER_NAME,
    "sender_title": SENDER_TITLE,
    "business_legal_name": "wrrk.ai",
    "business_postal_address": "",
    "dashboard_host": "127.0.0.1",
    "dashboard_port": 8765,
    "fit_threshold": 75,
    "email_daily_cap": 20,
    "gmail_schedule_timezone": "Asia/Kolkata",
    "email_copy_style": "founder_booking_note_v4",
    "email_booking_url": BOOKING_URL,
    "linkedin_daily_cap": 5,
    # Manual browser review is the default. Automated comment submission is available
    # only through LinkedIn's approved OAuth-backed Community Management API.
    "linkedin_posting_mode": "manual",
    "linkedin_post_discovery_mode": "manual",
    "linkedin_api_version": "202607",
    "linkedin_api_redirect_uri": "http://127.0.0.1:8766/callback",
    "linkedin_api_client_id": "",
    "linkedin_api_actor_urn": "",
    "linkedin_api_authorized_member_urn": "",
    "linkedin_api_authorized_member_name": "",
    "linkedin_api_scope": "",
    "linkedin_api_token_expires_at": "",
    "linkedin_api_refresh_expires_at": "",
    "linkedin_api_refresh_available": False,
    "email_pacing_min_minutes": 7,
    "email_pacing_max_minutes": 15,
    "linkedin_pacing_min_minutes": 8,
    "linkedin_pacing_max_minutes": 15,
    "business_hour_start": 9,
    "business_hour_end": 17,
    "overdue_grace_minutes": 15,
    "domain_cooldown_days": 90,
    "author_cooldown_days": 14,
    "post_max_age_hours": 48,
    "worker_lease_minutes": 30,
    "retention_days": 180,
    "apify_monthly_budget_usd": 5.0,
    "apify_run_budget_usd": 0.50,
    "market_policies": MARKET_POLICIES,
    "allowed_product_claims": [
        "wrrk.ai gives small teams one workspace for customer conversations, CRM, tasks, people operations, and business tools.",
        "wrrk.ai helps small teams keep customer conversations and the work that follows in one place.",
        "I am building wrrk.ai to keep customer conversations connected to the follow-up work for small teams.",
        "I am building wrrk.ai so a customer message and the next task stay connected for small teams.",
        "I am building wrrk.ai to give small teams one place to see a customer conversation and its next action.",
        "I am building wrrk.ai to help small teams carry context from a customer message into the work that follows.",
        "A tailored wrrk.ai demo can be prepared around the prospect's publicly visible workflow.",
    ],
    "forbidden_claims": [
        "AI meeting notetaker",
        "lead discovery across six platforms",
        "Ziwo voice",
        "public REST API",
        "Meta Ads orchestration",
        "Google Ads orchestration",
        "replaces $400/mo",
    ],
}

KEYCHAIN_SERVICE = "wrrkhunt.gmail.app-password"
LINKEDIN_CLIENT_SECRET_SERVICE = "wrrkhunt.linkedin.client-secret"
LINKEDIN_ACCESS_TOKEN_SERVICE = "wrrkhunt.linkedin.access-token"
LINKEDIN_REFRESH_TOKEN_SERVICE = "wrrkhunt.linkedin.refresh-token"


def ensure_home() -> None:
    """Create private runtime directories outside Git."""
    for path in (APP_HOME, LINKEDIN_PROFILE, META_PROFILE, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.chmod(0o700)
        except OSError:
            pass


def keychain_get(service: str = KEYCHAIN_SERVICE, account: str = SENDER_EMAIL) -> str:
    """Read a secret from macOS Keychain, then use the documented env fallback."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().replace(" ", "")
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    if service == KEYCHAIN_SERVICE:
        return os.environ.get("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    return ""


def keychain_set(secret: str, service: str = KEYCHAIN_SERVICE,
                 account: str = SENDER_EMAIL) -> None:
    secret = secret.strip().replace(" ", "")
    if not secret:
        raise ValueError("empty secret")
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-a", account, "-s", service,
         "-w", secret],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "could not write macOS Keychain")


def setup_gmail_interactive(postal_address: str = "") -> dict[str, str]:
    """Collect the app password without echoing it and persist it in Keychain."""
    password = getpass.getpass(f"Gmail app password for {SENDER_EMAIL}: ")
    if len(password.replace(" ", "")) != 16:
        raise ValueError("a Gmail app password must contain 16 characters")
    keychain_set(password)
    if not postal_address:
        postal_address = input("Valid business postal address (required for email sending): ").strip()
    if not postal_address:
        raise ValueError("a valid business postal address is required for email sending")
    return {"business_postal_address": postal_address}


def json_value(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
