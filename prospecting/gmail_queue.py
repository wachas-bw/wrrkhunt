"""Helpers for assigning Gmail UI schedules without relaxing delivery policy."""
from __future__ import annotations

import json
import random
from bisect import bisect_right
from collections import defaultdict, deque
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import SENDER_EMAIL
from .copy_engine import validate_stored_message
from .db import Database, StateError
from .email_delivery import _record_bounce_and_maybe_pause
from .policy import regional_gate
from .util import in_business_window, iso, next_business_window, normalize_email, parse_iso, utcnow


def _ceil_minute(value: datetime) -> datetime:
    """Return a Gmail-compatible minute boundary without scheduling earlier."""
    if value.second or value.microsecond:
        value += timedelta(minutes=1)
    return value.replace(second=0, microsecond=0)


def preflight_connector_send(db: Database, message_id: int) -> dict[str, Any]:
    """Reserve one exact Gmail-connector send after every live gate passes."""
    row = db.row(
        "SELECT m.*,p.market,p.registrable_domain,p.corporate_type FROM messages m "
        "JOIN prospects p ON p.id=m.prospect_id WHERE m.id=? AND m.channel='email'",
        (message_id,),
    )
    errors: list[str] = []
    if not row:
        return {"ok": False, "errors": ["email message not found"]}
    if row["status"] != "scheduled":
        errors.append("message is not scheduled")
    errors.extend(validate_stored_message(db, message_id))
    settings = db.settings()
    state = db.channel("email")
    if state["emergency_stop"]:
        errors.append("email emergency stop is active")
    connector_queue_pause = (
        state["credential_status"] == "gmail_connector"
        and "connector delivery" in str(state["reason"] or "").lower()
    )
    if state["paused"] and not connector_queue_pause:
        errors.append(state["reason"] or "email channel is paused")
    if db.is_suppressed("email", row["to_address"]):
        errors.append("recipient is suppressed")
    if db.is_suppressed("domain", row["registrable_domain"]):
        errors.append("recipient domain is suppressed")
    errors.extend(regional_gate(
        settings, row["market"], corporate_type=row["corporate_type"],
        email=row["to_address"],
    ))
    now = utcnow()
    scheduled = parse_iso(row["scheduled_for"])
    if not scheduled:
        errors.append("scheduled time is missing")
    elif scheduled > now + timedelta(seconds=30):
        errors.append("scheduled time has not arrived")
    elif now - scheduled > timedelta(minutes=int(settings.get("overdue_grace_minutes", 15))):
        errors.append("scheduled action is overdue and must be rescheduled")
    if not in_business_window(
        now, row["market"], settings["market_policies"],
        int(settings["business_hour_start"]), int(settings["business_hour_end"]),
    ):
        errors.append("recipient is outside the local business window")
    if errors:
        return {"ok": False, "errors": sorted(set(errors))}

    reserved = db.row(
        "SELECT 1 FROM delivery_events WHERE message_id=? AND channel='email' "
        "AND event_type='gmail_send_reserved' LIMIT 1", (message_id,),
    )
    if not reserved:
        try:
            db.reserve_daily_action(
                "email", "gmail_send", iso()[:10], int(settings["email_daily_cap"])
            )
        except Exception as exc:
            return {"ok": False, "errors": [str(exc)]}
        db.record_event(
            "email", "gmail_send_reserved", message_id=message_id,
            details={"content_hash": row["content_hash"], "account": "wachas@wrrk.ai"},
        )
    return {
        "ok": True, "errors": [], "message_id": message_id,
        "content_hash": row["content_hash"], "to_address": row["to_address"],
    }


def record_connector_send(db: Database, message_id: int, gmail_id: str,
                          thread_id: str, draft_id: str) -> None:
    """Commit a definitive Gmail connector result to canonical state."""
    row = db.row("SELECT content_hash,status FROM messages WHERE id=?", (message_id,))
    if not row or row["status"] != "scheduled":
        raise ValueError("only the exact scheduled message can be recorded as sent")
    db.mark_delivered(message_id, gmail_id)
    db.set_thread_id(message_id, thread_id)
    db.record_event(
        "email", "sent", message_id=message_id, external_id=gmail_id,
        details={
            "content_hash": row["content_hash"], "gmail_thread_id": thread_id,
            "draft_id": draft_id, "account": "wachas@wrrk.ai",
        },
    )


def _connector_timestamp(value: str, label: str) -> str:
    moment = parse_iso(value)
    if not moment:
        raise StateError(f"{label} timestamp is missing or invalid")
    if moment > utcnow() + timedelta(minutes=5):
        raise StateError(f"{label} timestamp is in the future")
    return iso(moment)


def _active_gmail_schedule(db: Database, gmail_id: str) -> tuple[Any, dict[str, Any]]:
    event = db.row(
        "SELECT e.*,m.status,m.to_address,m.subject,m.content_hash,m.approved_hash,"
        "m.external_id AS message_external_id,m.thread_id,m.prospect_id FROM delivery_events e "
        "JOIN messages m ON m.id=e.message_id WHERE e.channel='email' "
        "AND e.event_type='gmail_scheduled' AND e.external_id=? "
        "ORDER BY e.id DESC LIMIT 1", (gmail_id,),
    )
    if not event:
        raise StateError("Gmail sent ID does not match a recorded scheduled message")
    cancelled = db.row(
        "SELECT MAX(id) AS id FROM delivery_events WHERE message_id=? AND channel='email' "
        "AND event_type='gmail_schedule_cancelled'", (event["message_id"],),
    )
    if cancelled and cancelled["id"] and int(cancelled["id"]) > int(event["id"]):
        raise StateError("the matching Gmail schedule was recorded as cancelled")
    try:
        details = json.loads(event["details_json"] or "{}")
    except json.JSONDecodeError as exc:
        raise StateError("the recorded Gmail schedule has invalid audit details") from exc
    return event, details


def record_native_gmail_send(
    db: Database,
    *,
    account: str,
    gmail_id: str,
    thread_id: str,
    to_address: str,
    subject: str,
    sent_at: str,
) -> dict[str, Any]:
    """Reconcile one Gmail-native scheduled send using exact immutable identifiers."""
    if normalize_email(account) != SENDER_EMAIL:
        raise StateError(f"Gmail snapshot belongs to {account or 'an unknown account'}")
    event, details = _active_gmail_schedule(db, gmail_id)
    message_id = int(event["message_id"])
    recipient = normalize_email(to_address)
    if recipient != normalize_email(event["to_address"]):
        raise StateError(f"Gmail recipient mismatch for message {message_id}")
    if str(subject or "").strip() != str(event["subject"] or "").strip():
        raise StateError(f"Gmail subject mismatch for message {message_id}")
    if not event["approved_hash"] or event["approved_hash"] != event["content_hash"]:
        raise StateError(f"approved content hash mismatch for message {message_id}")
    if details.get("content_hash") and details["content_hash"] != event["content_hash"]:
        raise StateError(f"scheduled content hash mismatch for message {message_id}")
    expected_threads = {
        value for value in (event["thread_id"], details.get("gmail_thread_id")) if value
    }
    if expected_threads and thread_id not in expected_threads:
        raise StateError(f"Gmail thread mismatch for message {message_id}")
    timestamp = _connector_timestamp(sent_at, "sent")
    if event["status"] in {"sent", "replied"}:
        if event["message_external_id"] != gmail_id:
            raise StateError(f"message {message_id} already has a different external ID")
        return {"message_id": message_id, "recorded": False}
    if event["status"] != "scheduled":
        raise StateError(f"message {message_id} is {event['status']}, not scheduled")
    db.mark_delivered(message_id, gmail_id, delivered_at=timestamp)
    db.set_thread_id(message_id, thread_id)
    if not db.row(
        "SELECT 1 FROM delivery_events WHERE channel='email' AND event_type='sent' "
        "AND external_id=? LIMIT 1", (gmail_id,),
    ):
        db.record_event(
            "email", "sent", message_id=message_id, external_id=gmail_id,
            occurred_at=timestamp,
            details={
                "content_hash": event["content_hash"], "gmail_thread_id": thread_id,
                "account": SENDER_EMAIL, "source": "gmail_native_schedule_reconciliation",
            },
        )
    return {"message_id": message_id, "recorded": True}


def record_native_gmail_bounce(
    db: Database,
    *,
    account: str,
    gmail_id: str,
    thread_id: str,
    failed_address: str,
    detail: str,
    occurred_at: str,
) -> dict[str, Any]:
    """Reconcile one hard bounce and suppress only the failed mailbox."""
    if normalize_email(account) != SENDER_EMAIL:
        raise StateError(f"Gmail snapshot belongs to {account or 'an unknown account'}")
    address = normalize_email(failed_address)
    row = db.row(
        "SELECT * FROM messages WHERE channel='email' AND thread_id=? AND to_address=? "
        "ORDER BY sent_at DESC LIMIT 1", (thread_id, address),
    )
    if not row or row["status"] not in {"sent", "replied"}:
        raise StateError("bounce does not match a sent Gmail message")
    timestamp = _connector_timestamp(occurred_at, "bounce")
    recorded = _record_bounce_and_maybe_pause(
        db, int(row["id"]), detail, external_id=gmail_id, occurred_at=timestamp,
        event_details={
            "gmail_thread_id": thread_id, "failed_address": address,
            "account": SENDER_EMAIL, "source": "gmail_connector_reconciliation",
        },
    )
    db.suppress("email", address, "Gmail hard bounce: address not found", "gmail_connector")
    return {"message_id": int(row["id"]), "recorded": recorded}


def record_native_gmail_reply(
    db: Database,
    *,
    account: str,
    gmail_id: str,
    thread_id: str,
    from_address: str,
    subject: str,
    body: str,
    occurred_at: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reconcile a human reply, cancel follow-ups, and store evidence without sending."""
    if normalize_email(account) != SENDER_EMAIL:
        raise StateError(f"Gmail snapshot belongs to {account or 'an unknown account'}")
    sender = normalize_email(from_address)
    row = db.row(
        "SELECT * FROM messages WHERE channel='email' AND thread_id=? AND to_address=? "
        "ORDER BY sent_at DESC LIMIT 1", (thread_id, sender),
    )
    if not row or row["status"] not in {"sent", "replied"}:
        raise StateError("reply does not match a sent Gmail message and recipient")
    timestamp = _connector_timestamp(occurred_at, "reply")
    if db.row(
        "SELECT 1 FROM delivery_events WHERE channel='email' AND event_type='human_reply' "
        "AND external_id=? LIMIT 1", (gmail_id,),
    ):
        return {"message_id": int(row["id"]), "recorded": False}
    db.mark_replied(int(row["id"]), note="human reply reconciled from Gmail")
    event_details = {
        "subject": str(subject or "")[:300], "from": sender,
        "gmail_thread_id": thread_id, "account": SENDER_EMAIL,
        "source": "gmail_connector_reconciliation",
    }
    event_details.update(details or {})
    db.record_event(
        "email", "human_reply", message_id=int(row["id"]), external_id=gmail_id,
        details=event_details, occurred_at=timestamp,
    )
    evidence_id = None
    excerpt = str(body or "").strip() or str(subject or "").strip()
    if row["prospect_id"] and excerpt:
        evidence_id = db.add_evidence(
            int(row["prospect_id"]), "inbound_reply", f"gmail://message/{gmail_id}",
            excerpt[:1200], "high", str(subject or "")[:300], detected_at=timestamp,
            metadata={"gmail_thread_id": thread_id, "from": sender},
        )
    return {"message_id": int(row["id"]), "recorded": True, "evidence_id": evidence_id}


def reconcile_gmail_snapshot(db: Database, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Apply a connector-read Gmail snapshot in delivery order, failing each mismatch closed."""
    account = str(snapshot.get("account") or "")
    result: dict[str, Any] = {
        "account": account, "sent": [], "bounces": [], "replies": [], "errors": [],
    }
    operations = (
        ("sent", record_native_gmail_send),
        ("bounces", record_native_gmail_bounce),
        ("replies", record_native_gmail_reply),
    )
    for collection, recorder in operations:
        rows = snapshot.get(collection, [])
        if not isinstance(rows, list):
            result["errors"].append(f"{collection}: expected an array")
            continue
        for index, raw in enumerate(rows):
            if not isinstance(raw, dict):
                result["errors"].append(f"{collection}[{index}]: expected an object")
                continue
            values = dict(raw)
            values["account"] = account
            values["gmail_id"] = str(values.pop("id", values.get("gmail_id", "")))
            if collection == "sent":
                to_value = values.pop("to", values.get("to_address", ""))
                if isinstance(to_value, list):
                    to_value = to_value[0] if len(to_value) == 1 else ""
                values["to_address"] = str(to_value or "")
                values["sent_at"] = str(values.pop("email_ts", values.get("sent_at", "")))
            elif collection == "bounces":
                values["occurred_at"] = str(values.pop("email_ts", values.get("occurred_at", "")))
            else:
                values["occurred_at"] = str(values.pop("email_ts", values.get("occurred_at", "")))
                values["from_address"] = str(values.pop("from", values.get("from_address", "")))
            try:
                outcome = recorder(db, **values)
                if outcome["recorded"]:
                    result[collection].append(outcome["message_id"])
            except (StateError, TypeError, ValueError) as exc:
                identity = values.get("gmail_id", f"row-{index}")
                result["errors"].append(f"{collection}:{identity}: {exc}")
    return result


def rebuild_gmail_schedule(db: Database, *, start_delay_minutes: int = 15) -> list[dict[str, Any]]:
    """Rebuild the unsent Gmail queue with global pacing and regional windows.

    The Gmail connector does not expose scheduled send, so the resulting rows are
    consumed by the authenticated Gmail UI. Existing immutable content and
    approval hashes are untouched.
    """
    settings = db.settings()
    rows = db.rows(
        "SELECT m.id,m.to_address,m.subject,m.content_hash,m.approved_hash,p.market "
        "FROM messages m JOIN prospects p ON p.id=m.prospect_id "
        "WHERE m.channel='email' AND m.status='scheduled' "
        "ORDER BY m.scheduled_for,m.id"
    )
    queues: dict[str, deque[Any]] = defaultdict(deque)
    for row in rows:
        if not row["approved_hash"] or row["approved_hash"] != row["content_hash"]:
            continue
        queues[row["market"]].append(row)

    policies = settings["market_policies"]
    start_hour = int(settings["business_hour_start"])
    end_hour = int(settings["business_hour_end"])
    pace_min = int(settings["email_pacing_min_minutes"])
    pace_max = int(settings["email_pacing_max_minutes"])
    account_tz = ZoneInfo(str(settings.get("gmail_schedule_timezone", "Asia/Kolkata")))
    cap = int(settings["email_daily_cap"])
    day_counts: dict[str, int] = defaultdict(int)
    for action in db.rows(
        "SELECT COALESCE(sent_at,scheduled_for) AS action_at FROM messages "
        "WHERE channel='email' AND status IN ('sent','replied') "
        "AND COALESCE(sent_at,scheduled_for) IS NOT NULL"
    ):
        moment = parse_iso(action["action_at"])
        if moment:
            day_counts[moment.astimezone(account_tz).date().isoformat()] += 1
    cursor = _ceil_minute(utcnow() + timedelta(minutes=start_delay_minutes))
    rng = random.SystemRandom()
    rotation = deque(["GB", "AE", "IN", "US", "SG"])
    planned: list[dict[str, Any]] = []

    while any(queues.values()):
        proposed = cursor + timedelta(minutes=rng.randint(pace_min, pace_max))
        candidates: dict[str, Any] = {}
        for market, queue in queues.items():
            if queue:
                candidates[market] = next_business_window(
                    proposed, market, policies, start_hour, end_hour
                )
        earliest = min(candidates.values())
        day = earliest.astimezone(account_tz).date().isoformat()
        if day_counts[day] >= cap:
            local = earliest.astimezone(account_tz)
            cursor = datetime.combine(
                local.date() + timedelta(days=1), time(), tzinfo=account_tz
            ).astimezone(UTC)
            continue
        tied = {market for market, when in candidates.items() if when == earliest}
        chosen = next((market for market in rotation if market in tied), sorted(tied)[0])
        while rotation[0] != chosen:
            rotation.rotate(-1)
        rotation.rotate(-1)
        row = queues[chosen].popleft()
        with db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE messages SET scheduled_for=?,last_error=NULL,updated_at=? WHERE id=?",
                (iso(earliest), iso(), row["id"]),
            )
        db.record_event(
            "email", "gmail_rescheduled", message_id=int(row["id"]),
            details={"scheduled_for": iso(earliest), "account": "wachas@wrrk.ai"},
        )
        planned.append({
            "id": int(row["id"]), "to_address": row["to_address"],
            "subject": row["subject"], "market": chosen,
            "scheduled_for": iso(earliest),
        })
        day_counts[day] += 1
        cursor = earliest
    return planned


def fill_gmail_schedule(
    db: Database,
    message_ids: list[int],
    *,
    start_delay_minutes: int = 15,
    rng: random.Random | random.SystemRandom | None = None,
) -> list[dict[str, Any]]:
    """Move only newly released messages into safe gaps in Gmail's existing queue.

    Existing Gmail schedules are immutable here: changing them in SQLite without
    changing Gmail itself would make the audit trail inaccurate. New messages are
    placed around those fixed instants with global pacing, recipient-local business
    windows, and the configured per-day account cap.
    """
    target_ids = list(dict.fromkeys(int(value) for value in message_ids))
    if not target_ids:
        return []

    settings = db.settings()
    placeholders = ",".join("?" for _ in target_ids)
    rows = db.rows(
        "SELECT m.id,m.to_address,m.subject,m.content_hash,m.approved_hash,p.market "
        "FROM messages m JOIN prospects p ON p.id=m.prospect_id "
        f"WHERE m.id IN ({placeholders}) AND m.channel='email' AND m.status='scheduled'",
        target_ids,
    )
    by_id = {int(row["id"]): row for row in rows}
    if set(by_id) != set(target_ids):
        missing = sorted(set(target_ids) - set(by_id))
        raise ValueError(f"messages are not released email items: {missing}")
    for row in rows:
        if not row["approved_hash"] or row["approved_hash"] != row["content_hash"]:
            raise ValueError(f"message {row['id']} does not have an immutable approved hash")

    queues: dict[str, deque[Any]] = defaultdict(deque)
    for message_id in target_ids:
        row = by_id[message_id]
        queues[row["market"]].append(row)

    fixed_rows = db.rows(
        "SELECT scheduled_for FROM messages WHERE channel='email' AND status='scheduled' "
        f"AND id NOT IN ({placeholders}) AND scheduled_for IS NOT NULL ORDER BY scheduled_for",
        target_ids,
    )
    fixed = [value for row in fixed_rows if (value := parse_iso(row["scheduled_for"]))]

    account_tz = ZoneInfo(str(settings.get("gmail_schedule_timezone", "Asia/Kolkata")))
    cap = int(settings["email_daily_cap"])
    day_counts: dict[str, int] = defaultdict(int)
    action_rows = db.rows(
        "SELECT COALESCE(sent_at,scheduled_for) AS action_at FROM messages "
        "WHERE channel='email' AND status IN ('scheduled','sent','replied') "
        f"AND id NOT IN ({placeholders}) AND COALESCE(sent_at,scheduled_for) IS NOT NULL",
        target_ids,
    )
    for row in action_rows:
        moment = parse_iso(row["action_at"])
        if moment:
            day_counts[moment.astimezone(account_tz).date().isoformat()] += 1

    policies = settings["market_policies"]
    start_hour = int(settings["business_hour_start"])
    end_hour = int(settings["business_hour_end"])
    pace_min = int(settings["email_pacing_min_minutes"])
    pace_max = int(settings["email_pacing_max_minutes"])
    cursor = _ceil_minute(utcnow() + timedelta(minutes=start_delay_minutes))
    randomizer = rng or random.SystemRandom()
    rotation = deque(["GB", "AE", "IN", "US", "SG"])
    fixed_index = bisect_right(fixed, cursor)
    planned: list[dict[str, Any]] = []

    while any(queues.values()):
        while fixed_index < len(fixed) and fixed[fixed_index] <= cursor:
            cursor = max(cursor, fixed[fixed_index])
            fixed_index += 1

        proposed = cursor + timedelta(minutes=randomizer.randint(pace_min, pace_max))
        candidates = {
            market: next_business_window(proposed, market, policies, start_hour, end_hour)
            for market, queue in queues.items() if queue
        }
        earliest = min(candidates.values())

        # A pre-existing Gmail schedule is a fixed send event. Advance past it if
        # the proposed addition would cross it or leave less than the minimum gap.
        if fixed_index < len(fixed) and fixed[fixed_index] < earliest + timedelta(minutes=pace_min):
            cursor = fixed[fixed_index]
            fixed_index += 1
            continue

        day = earliest.astimezone(account_tz).date().isoformat()
        if day_counts[day] >= cap:
            local = earliest.astimezone(account_tz)
            next_midnight = datetime.combine(local.date() + timedelta(days=1), time(), tzinfo=account_tz)
            cursor = next_midnight.astimezone(UTC)
            fixed_index = bisect_right(fixed, cursor)
            continue

        tied = {market for market, when in candidates.items() if when == earliest}
        chosen = next((market for market in rotation if market in tied), sorted(tied)[0])
        while rotation[0] != chosen:
            rotation.rotate(-1)
        rotation.rotate(-1)
        row = queues[chosen].popleft()
        with db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE messages SET scheduled_for=?,last_error=NULL,updated_at=? WHERE id=?",
                (iso(earliest), iso(), row["id"]),
            )
        db.record_event(
            "email", "gmail_rescheduled", message_id=int(row["id"]),
            details={"scheduled_for": iso(earliest), "account": "wachas@wrrk.ai", "mode": "gap_fill"},
        )
        planned.append({
            "id": int(row["id"]), "to_address": row["to_address"],
            "subject": row["subject"], "market": chosen,
            "scheduled_for": iso(earliest),
        })
        day_counts[day] += 1
        cursor = earliest

    return planned
