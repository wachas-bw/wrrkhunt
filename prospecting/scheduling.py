"""Approval release, channel pacing, and recipient-local business windows."""
from __future__ import annotations

import random
from datetime import timedelta
from typing import Iterable

from .db import Database, StateError
from .copy_engine import validate_stored_message
from .policy import regional_gate
from .util import iso, next_business_window, parse_iso, utcnow


def _daily_release_count(db: Database, channel: str) -> int:
    row = db.row(
        "SELECT COUNT(*) AS n FROM approvals a JOIN messages m ON m.id=a.message_id "
        "WHERE a.action='released' AND m.channel=? AND substr(a.created_at,1,10)=?",
        (channel, iso()[:10]),
    )
    return int(row["n"] if row else 0)


def _channel_cursor(db: Database, channel: str):
    now = utcnow()
    row = db.row(
        "SELECT MAX(COALESCE(sent_at,scheduled_for)) AS latest FROM messages "
        "WHERE channel=? AND status IN ('scheduled','sent','posted')",
        (channel,),
    )
    latest = parse_iso(row["latest"] if row else None)
    return max(now, latest) if latest else now


def release_messages(db: Database, message_ids: Iterable[int], channel: str) -> dict[str, object]:
    settings = db.settings()
    cap = int(settings[f"{channel}_daily_cap"])
    pacing_min = int(settings[f"{channel}_pacing_min_minutes"])
    pacing_max = int(settings[f"{channel}_pacing_max_minutes"])
    policies = settings["market_policies"]
    released: list[int] = []
    blocked: dict[int, str] = {}
    cursor = _channel_cursor(db, channel)
    for message_id in message_ids:
        if _daily_release_count(db, channel) >= cap:
            blocked[int(message_id)] = f"daily release cap of {cap} reached"
            continue
        row = db.row(
            "SELECT m.*,COALESCE(p.market,po.market,'') AS market,p.corporate_type,"
            "po.post_url,po.author_url "
            "FROM messages m LEFT JOIN prospects p ON p.id=m.prospect_id "
            "LEFT JOIN posts po ON po.id=m.post_id WHERE m.id=?", (message_id,),
        )
        if not row or row["channel"] != channel:
            blocked[int(message_id)] = "message or channel not found"
            continue
        lint_errors = validate_stored_message(db, int(message_id))
        if lint_errors:
            blocked[int(message_id)] = "; ".join(lint_errors)
            continue
        if channel == "email":
            errors = regional_gate(settings, row["market"],
                                   corporate_type=row["corporate_type"] or "unknown",
                                   email=row["to_address"])
            if errors:
                blocked[int(message_id)] = "; ".join(errors)
                continue
        else:
            linkedin_mode = settings.get("linkedin_posting_mode", "manual")
            if linkedin_mode not in {"manual", "official_api"}:
                blocked[int(message_id)] = f"unknown LinkedIn posting mode: {linkedin_mode}"
                continue
            if linkedin_mode == "official_api":
                from .linkedin_api import target_urn_from_url
                if not target_urn_from_url(row["post_url"] or ""):
                    blocked[int(message_id)] = (
                        "official API mode requires a LinkedIn post URL containing an activity/share ID"
                    )
                    continue
            if not row["author_url"] or db.is_suppressed("linkedin", row["author_url"]):
                blocked[int(message_id)] = "LinkedIn author is missing or suppressed"
                continue
            cutoff = iso(utcnow() - timedelta(days=int(settings["author_cooldown_days"])))
            author_duplicate = db.row(
                "SELECT 1 FROM messages old JOIN posts p ON p.id=old.post_id "
                "WHERE p.author_url=? AND old.id!=? "
                "AND old.status IN ('scheduled','posted') "
                "AND COALESCE(old.sent_at,old.scheduled_for,old.created_at)>=? LIMIT 1",
                (row["author_url"], message_id, cutoff),
            )
            if author_duplicate:
                blocked[int(message_id)] = "LinkedIn author is inside the 14-day cooldown"
                continue
        cursor += timedelta(minutes=random.SystemRandom().randint(pacing_min, pacing_max))
        scheduled = next_business_window(
            cursor, row["market"], policies,
            int(settings["business_hour_start"]), int(settings["business_hour_end"]),
        )
        try:
            db.reserve_daily_action(channel, "release", iso()[:10], cap)
            db.release_message(int(message_id), iso(scheduled))
            released.append(int(message_id))
            cursor = scheduled
        except StateError as exc:
            blocked[int(message_id)] = str(exc)
    return {"released": released, "blocked": blocked}


def reschedule_overdue(db: Database) -> int:
    settings = db.settings()
    grace = timedelta(minutes=int(settings.get("overdue_grace_minutes", 15)))
    cutoff = utcnow() - grace
    rows = db.rows(
        "SELECT m.id,m.scheduled_for,COALESCE(p.market,po.market,'') AS market,m.channel "
        "FROM messages m LEFT JOIN prospects p ON p.id=m.prospect_id "
        "LEFT JOIN posts po ON po.id=m.post_id WHERE m.status='scheduled' AND m.scheduled_for<? "
        "AND NOT (m.channel='email' AND EXISTS ("
        "SELECT 1 FROM delivery_events e WHERE e.message_id=m.id "
        "AND e.channel='email' AND e.event_type='gmail_scheduled' "
        "AND e.id>COALESCE((SELECT MAX(c.id) FROM delivery_events c "
        "WHERE c.message_id=m.id AND c.channel='email' "
        "AND c.event_type='gmail_schedule_cancelled'),0))) "
        "ORDER BY m.channel,m.scheduled_for,m.id",
        (iso(cutoff),),
    )
    changed = 0
    policies = settings["market_policies"]
    cursors = {channel: _channel_cursor(db, channel) for channel in ("email", "linkedin")}
    rng = random.SystemRandom()
    for row in rows:
        if (row["channel"] == "linkedin" and
                settings.get("linkedin_posting_mode", "manual") == "manual"):
            # A human owns the final timing and click. Keep the reserved item visible
            # instead of repeatedly moving it away from the review desk.
            continue
        pace_min = int(settings[f"{row['channel']}_pacing_min_minutes"])
        pace_max = int(settings[f"{row['channel']}_pacing_max_minutes"])
        proposed = cursors[row["channel"]] + timedelta(minutes=rng.randint(pace_min, pace_max))
        next_time = next_business_window(
            proposed, row["market"], policies,
            int(settings["business_hour_start"]), int(settings["business_hour_end"]),
        )
        db.reschedule(row["id"], iso(next_time), "rescheduled after missed local-time window")
        cursors[row["channel"]] = next_time
        changed += 1
    return changed
