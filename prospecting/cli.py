"""Command-line interface for local prospecting automation."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from .config import APP_HOME, DB_PATH, REPO_ROOT, setup_gmail_interactive
from .audit import audit_pending
from .copy_engine import prepare, revalidate_queue
from .dashboard import serve
from .db import Database
from .discovery import discover
from .email_delivery import gmail_health, poll_inbox
from .exporter import default_email_export_path, export_email_contacts
from .gmail_queue import reconcile_gmail_snapshot
from .launchd import install as install_launchd, uninstall as uninstall_launchd
from .linkedin_api import DEFAULT_REDIRECT_URI, LinkedInAPIStop, setup_linkedin_api
from .linkedin_delivery import linkedin_health, setup_linkedin
from .migration import import_legacy
from .phone_prospecting import prospect_phones
from .worker import cleanup_retention, run_worker


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _channels(value: str) -> list[str]:
    return ["email", "linkedin"] if value == "all" else [value]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="automation", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the private runtime database")

    setup = sub.add_parser("setup", help="configure a credential-backed channel")
    setup.add_argument("channel", choices=["gmail", "linkedin", "linkedin-api"])
    setup.add_argument("--postal-address", default="")
    setup.add_argument("--client-id", default="", help="LinkedIn Developer app Client ID")
    setup.add_argument("--actor-urn", default="me",
                       help="'me' (default), or an exact person/organization URN")
    setup.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI,
                       help="exact loopback redirect URI registered in the LinkedIn app")

    legacy = sub.add_parser("import-legacy", help="import existing files into the held legacy campaign")
    legacy.add_argument("--dry-run", action="store_true")

    sourcing = sub.add_parser("discover", help="discover and audit fresh candidates")
    sourcing.add_argument("--markets", default="IN,AE,SG,GB,US")
    sourcing.add_argument("--mix", action="store_true", help="use the planned 50/30/20 pools")
    sourcing.add_argument("--target", type=int, default=60)
    sourcing.add_argument("--source", choices=["all", "exa", "meta", "apify"], default="all")
    exa_variant = sourcing.add_mutually_exclusive_group()
    exa_variant.add_argument("--expansion", action="store_true",
                             help="use a second disjoint 50/30/20 Exa query set")
    exa_variant.add_argument("--fresh-wave", action="store_true",
                             help="use a third disjoint 50/30/20 Exa query set")
    exa_variant.add_argument("--conversion-wave", action="store_true",
                             help="use a fourth disjoint 50/30/20 Exa query set")
    sourcing.add_argument("--no-audit", action="store_true")
    audit = sub.add_parser("audit", help="audit already-discovered fresh candidates")
    audit.add_argument("--limit", type=int, default=60)
    audit.add_argument("--revalidate", action="store_true", help="re-run gates on the review queue")

    prep = sub.add_parser("prepare", help="generate Codex drafts for review")
    prep.add_argument("--email-limit", type=int, default=20)
    prep.add_argument("--comment-limit", type=int, default=5)

    dashboard = sub.add_parser("serve", help="serve the loopback-only approval dashboard")
    dashboard.add_argument("--port", type=int, default=0)

    worker = sub.add_parser("worker", help="run one delivery worker cycle")
    worker.add_argument("--channel", choices=["all", "email", "linkedin"], default="all")
    worker.add_argument("--dry-run", action="store_true",
                        help="inspect due items without counters, rescheduling, or external actions")
    sub.add_parser("inbox", help="poll Gmail for replies, bounces, and opt-outs")
    reconcile = sub.add_parser(
        "reconcile-gmail",
        help="reconcile a connector-read Gmail JSON snapshot without sending mail",
    )
    reconcile.add_argument("--input", default="-", help="snapshot JSON path, or - for stdin")

    for command in ("pause", "resume"):
        action = sub.add_parser(command)
        action.add_argument("channel", choices=["all", "email", "linkedin"], default="all", nargs="?")
    sub.add_parser("status")
    health = sub.add_parser("health")
    health.add_argument("channel", choices=["all", "email", "linkedin"], default="all", nargs="?")
    sub.add_parser("install-launchd")
    sub.add_parser("uninstall-launchd")
    sub.add_parser("cleanup")
    export_emails = sub.add_parser("export-emails", help="export found email contacts to a private CSV")
    export_emails.add_argument("--output", default="")
    export_emails.add_argument("--recommended-only", action="store_true")
    phone_run = sub.add_parser(
        "prospect-phones",
        help="run isolated, read-only UAE business-phone prospecting and export private CSVs",
    )
    phone_run.add_argument("--market", choices=["AE"], default="AE")
    phone_run.add_argument("--target", type=int, default=100)
    phone_run.add_argument("--angle", choices=["ai-whatsapp"], default="ai-whatsapp")
    phone_run.add_argument("--city-priority", default="Dubai")
    phone_run.add_argument("--no-import", action="store_true",
                           help="explicitly document that no candidate is imported (always enforced)")
    phone_run.add_argument(
        "--output", default=str(
            APP_HOME / "exports" / f"wrrkhunt_uae_phone_leads_{date.today().isoformat()}.csv"
        ),
    )
    phone_run.add_argument("--canonical-db", default=str(DB_PATH), help=argparse.SUPPRESS)
    phone_run.add_argument("--max-candidates", type=int, default=250, help=argparse.SUPPRESS)
    phone_run.add_argument("--skip-exa", action="store_true", help=argparse.SUPPRESS)
    phone_run.add_argument("--broker-input", default="", help=argparse.SUPPRESS)
    phone_run.add_argument("--verification-input", default="", help=argparse.SUPPRESS)
    phone_run.add_argument("--candidate-input", default="", help=argparse.SUPPRESS)
    phone_run.add_argument("--stage-only", action="store_true", help=argparse.SUPPRESS)
    phone_run.add_argument("--finalize-stage", default="", help=argparse.SUPPRESS)
    return parser


def status(db: Database) -> dict[str, Any]:
    db.initialize()
    channels = [dict(row) for row in db.rows("SELECT * FROM channel_state ORDER BY channel")]
    campaigns = [dict(row) for row in db.rows(
        "SELECT c.name,c.status,COUNT(p.id) AS prospects FROM campaigns c "
        "LEFT JOIN prospects p ON p.campaign_id=c.id GROUP BY c.id ORDER BY c.id")]
    messages = {row["status"]: row["n"] for row in db.rows(
        "SELECT status,COUNT(*) AS n FROM messages GROUP BY status")}
    linkedin_mode = db.setting("linkedin_posting_mode", "manual")
    return {
        "runtime_home": str(APP_HOME), "database": str(db.path), "channels": channels,
        "campaigns": campaigns, "messages": messages,
        "setup_gates": {
            "postal_address": bool(db.setting("business_postal_address", "")),
            "linkedin_posting_mode": linkedin_mode,
            "linkedin_post_discovery_mode": db.setting(
                "linkedin_post_discovery_mode", "manual"),
            "linkedin_manual_ready": (
                linkedin_mode == "manual"
                and not bool(db.channel("linkedin")["paused"])
            ),
            "linkedin_official_api_ready": (
                linkedin_mode == "official_api"
                and bool(db.setting("linkedin_api_client_id", ""))
                and bool(db.setting("linkedin_api_actor_urn", ""))
                and bool(db.setting("linkedin_api_authorized_member_urn", ""))
                and bool(db.setting("linkedin_api_scope", ""))
                and bool(db.setting("linkedin_api_token_expires_at", ""))
                and not bool(db.channel("linkedin")["paused"])
            ),
        },
    }


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "prospect-phones":
        # This dispatch deliberately occurs before Database.initialize(). The canonical
        # SQLite file is opened by phone_prospecting with mode=ro + query_only only.
        _print(prospect_phones(
            output=Path(args.output), canonical_db=Path(args.canonical_db),
            target=args.target, max_candidates=args.max_candidates,
            use_exa=not args.skip_exa,
            broker_input=Path(args.broker_input) if args.broker_input else None,
            verification_input=(Path(args.verification_input) if args.verification_input else None),
            candidate_input=Path(args.candidate_input) if args.candidate_input else None,
            stage_only=args.stage_only,
            finalize_stage=Path(args.finalize_stage) if args.finalize_stage else None,
        ))
        return
    db = Database()
    db.initialize()
    if args.command == "init":
        _print(status(db))
    elif args.command == "setup":
        if args.channel == "gmail":
            values = setup_gmail_interactive(args.postal_address)
            for key, value in values.items():
                db.set_setting(key, value)
            healthy, detail = gmail_health(db)
            _print({"gmail": "healthy" if healthy else "blocked", "detail": detail,
                    "us_enabled": healthy and bool(values.get("business_postal_address"))})
        elif args.channel == "linkedin":
            _print(setup_linkedin(db))
        else:
            if not args.client_id:
                raise SystemExit(
                    "setup linkedin-api requires --client-id; "
                    "the app must already have LinkedIn Community Management API approval"
                )
            try:
                _print(setup_linkedin_api(
                    db, client_id=args.client_id, actor_urn=args.actor_urn,
                    redirect_uri=args.redirect_uri,
                ))
            except LinkedInAPIStop as exc:
                raise SystemExit(str(exc)) from None
    elif args.command == "import-legacy":
        _print({"dry_run": args.dry_run, "counts": import_legacy(db, dry_run=args.dry_run)})
    elif args.command == "discover":
        markets = [value.strip().upper() for value in args.markets.split(",") if value.strip()]
        use = args.source
        _print(discover(
            db, markets, args.target, use_exa=use in {"all", "exa"},
            use_meta=use in {"all", "meta"}, use_apify=use in {"all", "apify"},
            audit=not args.no_audit, expansion=args.expansion, fresh_wave=args.fresh_wave,
            conversion_wave=args.conversion_wave,
        ))
    elif args.command == "audit":
        if args.revalidate:
            _print(revalidate_queue(db))
            return
        results = audit_pending(db, "fresh", args.limit)
        _print({"audited": len(results),
                "qualified": sum(item.get("status") == "qualified" for item in results),
                "results": results})
    elif args.command == "prepare":
        _print(prepare(db, args.email_limit, args.comment_limit))
    elif args.command == "serve":
        serve(db, args.port or None)
    elif args.command == "worker":
        _print(run_worker(db, args.channel, dry_run=args.dry_run))
    elif args.command == "inbox":
        _print(poll_inbox(db))
    elif args.command == "reconcile-gmail":
        if args.input == "-":
            snapshot = json.load(sys.stdin)
        else:
            snapshot = json.loads(Path(args.input).expanduser().read_text())
        if not isinstance(snapshot, dict):
            raise SystemExit("Gmail snapshot must be a JSON object")
        _print(reconcile_gmail_snapshot(db, snapshot))
    elif args.command in {"pause", "resume"}:
        paused = args.command == "pause"
        for channel in _channels(args.channel):
            if not paused and db.channel(channel)["emergency_stop"]:
                raise SystemExit(f"{channel}: clear emergency stop in dashboard before resuming")
            db.set_channel(channel, paused=paused, reason="manual pause" if paused else "")
        _print(status(db))
    elif args.command == "status":
        _print(status(db))
    elif args.command == "health":
        result = {}
        if args.channel in {"all", "email"}:
            result["email"] = gmail_health(db)
        if args.channel in {"all", "linkedin"}:
            result["linkedin"] = linkedin_health(db)
        _print(result)
    elif args.command == "install-launchd":
        _print({"installed": [str(path) for path in install_launchd()]})
    elif args.command == "uninstall-launchd":
        _print({"removed": [str(path) for path in uninstall_launchd()]})
    elif args.command == "cleanup":
        _print({"prospects_removed": cleanup_retention(db)})
    elif args.command == "export-emails":
        output = Path(args.output).expanduser() if args.output else default_email_export_path()
        _print(export_email_contacts(db, output, recommended_only=args.recommended_only))


if __name__ == "__main__":
    main()
