"""Single-candidate worker for supervised phone prospecting audits."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from .phone_prospecting import _audit_candidate


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    context = json.loads(Path(args.context).read_text(encoding="utf-8"))
    state = {key: set(value) for key, value in context["state"].items()}
    accepted, rejected = _audit_candidate(
        candidate, state, context.get("broker_index", {}), context.get("verifications", {}),
    )
    output = Path(args.output).resolve()
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=output.parent, delete=False,
    ) as handle:
        json.dump({"accepted": accepted, "rejected": rejected}, handle, ensure_ascii=False)
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    os.replace(temporary, output)
    output.chmod(0o600)


if __name__ == "__main__":
    main()
