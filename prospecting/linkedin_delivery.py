"""LinkedIn workflow dispatcher and manual-browser fallback."""
from __future__ import annotations

import json
import webbrowser
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .copy_engine import validate_stored_message
from .db import Database, StateError
from .util import (
    content_hash, iso, normalize_linkedin, parse_iso, registrable_domain, text_hash,
    utcnow,
)

MANUAL_MODE = "manual"
MANUAL_REASON = (
    "manual browser submission only; wrrkhunt never controls your signed-in LinkedIn "
    "session or types, clicks, or posts for you"
)


class LinkedInStop(RuntimeError):
    pass


def setup_linkedin(db: Database) -> dict[str, str]:
    """Enable the safe workflow and open LinkedIn in the user's normal browser.

    Login and posting are deliberately not inspected or controlled by this program.
    """
    db.set_setting("linkedin_posting_mode", MANUAL_MODE)
    db.set_setting("linkedin_identity_url", "")
    db.set_setting("linkedin_identity_name", "")
    db.set_channel(
        "linkedin", paused=False, reason=MANUAL_REASON, credential_status="manual",
    )
    opened = webbrowser.open("https://www.linkedin.com/feed/", new=2)
    return {
        "mode": MANUAL_MODE,
        "browser_opened": str(bool(opened)).lower(),
        "detail": MANUAL_REASON,
    }


def linkedin_health(db: Database) -> tuple[bool, str]:
    """Report health for the configured manual or official-API workflow."""
    mode = db.setting("linkedin_posting_mode", MANUAL_MODE)
    if mode == "official_api":
        from .linkedin_api import linkedin_api_health
        return linkedin_api_health(db)
    if mode != MANUAL_MODE:
        reason = f"unknown LinkedIn posting mode: {mode}"
        db.set_channel("linkedin", paused=True, reason=reason, credential_status="failed")
        return False, reason
    state = db.channel("linkedin")
    if state["emergency_stop"]:
        return False, "emergency stop active"
    db.set_channel(
        "linkedin", paused=bool(state["paused"]),
        reason=state["reason"] if state["paused"] else MANUAL_REASON,
        credential_status="manual",
    )
    return True, MANUAL_REASON


def _linkedin_url(value: str, allowed_paths: tuple[str, ...]) -> str:
    normalized = normalize_linkedin(value)
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or parsed.hostname not in {"linkedin.com", "www.linkedin.com"}:
        return ""
    if not any(parsed.path.startswith(prefix) for prefix in allowed_paths):
        return ""
    return normalized


def add_manual_post(db: Database, *, post_url: str, author_url: str, author_name: str,
                    post_text: str, role: str, market: str, published_at: str,
                    prospect_domain: str = "") -> int:
    """Store a post transcribed by the user from their normal browser."""
    if db.setting("linkedin_post_discovery_mode", "manual") != "manual":
        raise StateError("manual LinkedIn post intake is not enabled")
    post_url = _linkedin_url(post_url, ("/posts/", "/feed/update/"))
    author_url = _linkedin_url(author_url, ("/in/", "/company/"))
    if not post_url:
        raise StateError("enter a valid LinkedIn post URL")
    if not author_url:
        raise StateError("enter a valid LinkedIn profile or company URL")
    if author_url.endswith("/posts"):
        author_url = author_url[:-6]
    author_name = " ".join((author_name or "").split())
    post_text = (post_text or "").strip()
    if not author_name:
        raise StateError("author name is required")
    if len(post_text) < 40:
        raise StateError("paste enough of the post to verify a specific comment")
    role = (role or "").strip().lower()
    if role not in {"prospect", "influencer"}:
        raise StateError("role must be prospect or influencer")
    market = (market or "").strip().upper()
    policies = db.setting("market_policies", {})
    policy = policies.get(market)
    if not policy or not policy.get("enabled"):
        raise StateError("select an enabled target market")
    try:
        published = datetime.fromisoformat((published_at or "").replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=ZoneInfo(policy["timezone"]))
        published = published.astimezone(UTC)
    except (KeyError, TypeError, ValueError):
        raise StateError("enter the post's visible publication date and time") from None
    now = utcnow()
    if published > now + timedelta(minutes=5):
        raise StateError("post publication time cannot be in the future")
    if published < now - timedelta(hours=int(db.setting("post_max_age_hours", 48))):
        raise StateError("only posts visibly published within the last 48 hours are accepted")
    if db.is_suppressed("linkedin", author_url):
        raise StateError("LinkedIn author is suppressed")

    campaign_id = db.ensure_campaign("fresh", "fresh", "active")
    prospect_id = None
    if role == "prospect":
        root = registrable_domain(prospect_domain)
        if not root:
            raise StateError("a qualified prospect domain is required for a prospect post")
        prospect = db.row(
            "SELECT p.* FROM prospects p JOIN campaigns c ON c.id=p.campaign_id "
            "WHERE c.name='fresh' AND p.registrable_domain=? AND p.fit_score>=? "
            "AND p.status NOT IN ('discovered','audited','blocked','rejected','suppressed','failed','replied')",
            (root, int(db.setting("fit_threshold", 75))),
        )
        if not prospect:
            raise StateError("prospect domain is not qualified at the current threshold")
        prospect_id = int(prospect["id"])

    run_id = db.create_source_run(
        campaign_id, "manual_linkedin", post_url, market, role,
    )
    with db.transaction(immediate=True) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO posts(prospect_id,source_run_id,author_name,author_url,post_url,"
            "text,text_hash,published_at,market,role,status,metadata_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (prospect_id, run_id, author_name, author_url, post_url, post_text,
             text_hash(post_text), iso(published), market, role, "discovered",
             json.dumps({"entered_by": "local-user", "publication_time_basis": "user-visible"}),
             iso()),
        )
        post_id = int(cur.lastrowid or 0)
    if not post_id:
        db.finish_source_run(run_id, status="duplicate", candidates=0,
                             budget_note="manually supplied; no external access")
        raise StateError("this LinkedIn post is already in the local database")
    db.finish_source_run(run_id, status="completed", candidates=1,
                         budget_note="manually supplied; no external access")
    return post_id


def _exact_hash(row: dict[str, Any]) -> bool:
    expected = content_hash(
        row["channel"], row["kind"], row["to_address"], row["subject"],
        row["body"], json.loads(row["evidence_ids_json"]),
    )
    return expected == row["content_hash"] == row["approved_hash"]


def post_due(db: Database, limit: int = 5) -> dict[str, Any]:
    """Dispatch to the official API or report the due manual-review queue."""
    mode = db.setting("linkedin_posting_mode", MANUAL_MODE)
    if mode == "official_api":
        from .linkedin_api import post_due_api
        return post_due_api(db, limit)
    if mode != MANUAL_MODE:
        reason = f"unknown LinkedIn posting mode: {mode}"
        db.set_channel("linkedin", paused=True, reason=reason, credential_status="failed")
        return {"posted": 0, "blocked": reason}
    state = db.channel("linkedin")
    if state["paused"] or state["emergency_stop"]:
        return {"posted": 0, "blocked": state["reason"] or "channel paused"}
    rows = db.rows(
        "SELECT id FROM messages WHERE channel='linkedin' AND status='scheduled' "
        "AND scheduled_for<=? ORDER BY scheduled_for LIMIT ?", (iso(), limit),
    )
    return {
        "posted": 0,
        "manual_action_required": len(rows),
        "detail": MANUAL_REASON,
    }


def confirm_manual_post(db: Database, message_id: int) -> str:
    """Record the user's confirmation after they post the exact approved comment.

    This function performs no browser or network action. It retains the immutable-hash,
    suppression, author-cooldown, freshness, and daily-cap guarantees for audit history.
    """
    if db.setting("linkedin_posting_mode", MANUAL_MODE) != MANUAL_MODE:
        raise StateError("manual-post confirmation is available only in manual LinkedIn mode")
    raw = db.row(
        "SELECT m.*,p.post_url,p.author_url,p.published_at,p.status AS post_status "
        "FROM messages m JOIN posts p ON p.id=m.post_id WHERE m.id=?", (message_id,),
    )
    if not raw:
        raise StateError("LinkedIn comment not found")
    row = dict(raw)
    if row["channel"] != "linkedin" or row["kind"] != "comment":
        raise StateError("only a LinkedIn comment can be marked manually posted")
    if row["status"] != "scheduled":
        raise StateError("schedule the approved comment before posting it manually")
    if parse_iso(row["scheduled_for"]) and parse_iso(row["scheduled_for"]) > utcnow():
        raise StateError("this manual comment slot is not due yet")
    lint_errors = validate_stored_message(db, message_id)
    if lint_errors:
        raise StateError("approved comment failed lint: " + "; ".join(lint_errors))
    if not _exact_hash(row):
        raise StateError("immutable approved comment hash mismatch")
    if db.is_suppressed("linkedin", row["author_url"]):
        raise StateError("LinkedIn author is suppressed")
    max_age = timedelta(hours=int(db.setting("post_max_age_hours", 48)))
    published_at = parse_iso(row["published_at"])
    if not published_at or published_at < utcnow() - max_age:
        raise StateError("LinkedIn post is no longer fresh enough for this queue")
    duplicate = db.row(
        "SELECT 1 FROM messages m JOIN posts p ON p.id=m.post_id "
        "WHERE p.post_url=? AND m.id!=? AND m.status='posted' LIMIT 1",
        (row["post_url"], message_id),
    )
    if duplicate:
        raise StateError("this post already has a recorded comment")
    cooldown = int(db.setting("author_cooldown_days", 14))
    author_duplicate = db.row(
        "SELECT 1 FROM messages old JOIN posts p ON p.id=old.post_id "
        "WHERE p.author_url=? AND old.id!=? AND old.status='posted' "
        "AND old.sent_at>=? LIMIT 1",
        (row["author_url"], message_id, iso(utcnow() - timedelta(days=cooldown))),
    )
    if author_duplicate:
        raise StateError(f"{cooldown}-day author cooldown would be violated")

    db.reserve_daily_action(
        "linkedin", "comment_attempt", iso()[:10],
        int(db.setting("linkedin_daily_cap", 5)),
    )
    external_id = f"{normalize_linkedin(row['post_url'])}#manual-{row['content_hash'][:12]}"
    db.mark_delivered(message_id, external_id, posted=True)
    db.record_event(
        "linkedin", "manually_posted", message_id=message_id, external_id=external_id,
        details={
            "content_hash": row["content_hash"],
            "assertion": "local user confirmed posting the exact approved comment",
        },
    )
    with db.transaction(immediate=True) as conn:
        conn.execute("UPDATE posts SET status='posted' WHERE id=?", (row["post_id"],))
    return external_id
