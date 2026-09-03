"""Short-lived launchd worker; independent channels share one crash-safe lease."""
from __future__ import annotations

import os
import uuid
from datetime import timedelta
from typing import Any

from .db import Database
from .email_delivery import send_due
from .linkedin_delivery import post_due
from .copy_engine import validate_stored_message
from .scheduling import reschedule_overdue
from .util import iso, utcnow


def _dry_channel(db: Database, channel: str) -> dict[str, Any]:
    connector_guard = (
        "AND NOT EXISTS (SELECT 1 FROM delivery_events e WHERE e.message_id=messages.id "
        "AND e.channel='email' AND e.event_type='gmail_scheduled' "
        "AND e.id>COALESCE((SELECT MAX(c.id) FROM delivery_events c "
        "WHERE c.message_id=messages.id AND c.channel='email' "
        "AND c.event_type='gmail_schedule_cancelled'),0)) "
        if channel == "email" else ""
    )
    rows = db.rows(
        "SELECT id,scheduled_for FROM messages WHERE channel=? AND status='scheduled' "
        f"AND scheduled_for<=? {connector_guard}ORDER BY scheduled_for", (channel, iso()))
    errors = {int(row["id"]): validate_stored_message(db, int(row["id"])) for row in rows}
    state = db.channel(channel)
    ready = not state["paused"] and not state["emergency_stop"]
    valid = [message_id for message_id, values in errors.items() if not values]
    result = {
        "dry_run": True, "due": len(rows), "valid": len(valid),
        "would_attempt": len(valid) if ready else 0,
        "channel_ready": ready,
        "errors": {key: value for key, value in errors.items() if value},
    }
    if channel == "linkedin" and db.setting("linkedin_posting_mode", "manual") == "manual":
        result["would_attempt"] = 0
        result["manual_action_required"] = len(valid) if ready else 0
    return result


def run_worker(db: Database, channel: str = "all", dry_run: bool = False) -> dict[str, Any]:
    db.initialize()
    if dry_run:
        result: dict[str, Any] = {"status": "dry_run", "rescheduled_overdue": 0}
        if channel in {"all", "email"}:
            result["email"] = _dry_channel(db, "email")
        if channel in {"all", "linkedin"}:
            result["linkedin"] = _dry_channel(db, "linkedin")
        return result
    owner = f"{os.getpid()}-{uuid.uuid4()}"
    lease_minutes = int(db.setting("worker_lease_minutes", 30))
    if not db.acquire_lease(
            "delivery-worker", owner, iso(utcnow() + timedelta(minutes=lease_minutes))):
        return {"status": "skipped", "reason": "another worker holds the lease"}
    result: dict[str, Any] = {"status": "completed"}
    try:
        result["rescheduled_overdue"] = reschedule_overdue(db)
        if channel in {"all", "email"}:
            result["email"] = send_due(db)
        if channel in {"all", "linkedin"}:
            result["linkedin"] = post_due(db)
        return result
    finally:
        db.release_lease("delivery-worker", owner)


def cleanup_retention(db: Database) -> int:
    """Purge completed-campaign prospect data after 180 days; keep suppressions."""
    days = int(db.setting("retention_days", 180))
    cutoff = iso(utcnow() - timedelta(days=days))
    campaigns = db.rows(
        "SELECT id FROM campaigns WHERE status='completed' AND completed_at IS NOT NULL AND completed_at<?",
        (cutoff,),
    )
    removed = 0
    for campaign in campaigns:
        with db.transaction(immediate=True) as conn:
            message_ids = [r[0] for r in conn.execute(
                "SELECT id FROM messages WHERE campaign_id=?", (campaign["id"],)).fetchall()]
            if message_ids:
                marks = ",".join("?" for _ in message_ids)
                conn.execute(f"DELETE FROM approvals WHERE message_id IN ({marks})", message_ids)
                conn.execute(f"DELETE FROM delivery_events WHERE message_id IN ({marks})", message_ids)
            conn.execute("DELETE FROM messages WHERE campaign_id=?", (campaign["id"],))
            conn.execute("DELETE FROM posts WHERE prospect_id IN (SELECT id FROM prospects WHERE campaign_id=?)",
                         (campaign["id"],))
            conn.execute("DELETE FROM posts WHERE source_run_id IN "
                         "(SELECT id FROM source_runs WHERE campaign_id=?)", (campaign["id"],))
            removed += conn.execute("DELETE FROM prospects WHERE campaign_id=?", (campaign["id"],)).rowcount
    return removed
