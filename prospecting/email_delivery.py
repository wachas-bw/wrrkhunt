"""Plain-text Gmail SMTP delivery and IMAP reply/bounce processing."""
from __future__ import annotations

import email
import imaplib
import json
import random
import re
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage, Message
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from typing import Any

from .config import KEYCHAIN_SERVICE, SENDER_EMAIL, keychain_get
from .copy_engine import generate_and_store, inbound_reply_request, validate_stored_message
from .db import Database, StateError
from .policy import regional_gate
from .util import (
    compliance_email_body, delivery_content_hash, in_business_window, iso,
    next_business_window, normalize_email, parse_iso, utcnow,
)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
IMAP_HOST = "imap.gmail.com"
OPTOUT_RE = re.compile(
    r"\b(?:unsubscribe|opt[ -]?out|remove me|stop (?:emailing|contacting)|do not (?:email|contact)|"
    r"don't (?:email|contact)|no more emails)\b", re.I,
)
BOUNCE_RE = re.compile(r"(?:undeliver|delivery status notification|mail delivery|failure notice|returned mail)", re.I)
AUTH_QUOTA_RE = re.compile(r"(?:auth|credential|application-specific password|quota|rate limit|daily user sending)", re.I)


def build_message(row: dict[str, Any], settings: dict[str, Any], parent_external_id: str = "") -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((settings.get("sender_name", "Wachas"), settings["sender_email"]))
    message["To"] = row["to_address"]
    message["Subject"] = row["subject"]
    message["Date"] = formatdate(localtime=True)
    message_id = make_msgid(domain="wrrk.ai")
    message["Message-ID"] = message_id
    message["List-Unsubscribe"] = f"<mailto:{settings['sender_email']}?subject=unsubscribe>"
    if parent_external_id:
        message["In-Reply-To"] = parent_external_id
        message["References"] = parent_external_id
    message.set_content(compliance_email_body(row["body"], settings))
    return message


def _channel_ready(db: Database, channel: str) -> tuple[bool, str]:
    state = db.channel(channel)
    if state["emergency_stop"]:
        return False, "emergency stop is active"
    if state["paused"]:
        return False, state["reason"]
    return True, ""


def gmail_health(db: Database) -> tuple[bool, str]:
    if not str(db.setting("business_postal_address", "") or "").strip():
        reason = "A valid business postal address is not configured"
        db.set_channel("email", paused=True, reason=reason, credential_status="missing")
        return False, reason
    password = keychain_get()
    if not password:
        db.set_channel("email", paused=True, reason="Gmail app password is not configured",
                       credential_status="missing")
        return False, "Gmail app password is not configured"
    try:
        client = imaplib.IMAP4_SSL(IMAP_HOST, ssl_context=ssl.create_default_context(), timeout=25)
        client.login(SENDER_EMAIL, password)
        client.logout()
    except Exception as exc:
        db.set_channel("email", paused=True, reason=f"Gmail health check failed: {exc}",
                       credential_status="failed")
        return False, str(exc)
    db.set_channel("email", paused=False, reason="", credential_status="healthy")
    return True, "healthy"


def _exact_hash(row: dict[str, Any], settings: dict[str, Any]) -> bool:
    expected = delivery_content_hash(
        row["channel"], row["kind"], row["to_address"], row["subject"], row["body"],
        json.loads(row["evidence_ids_json"]), settings,
    )
    return expected == row["content_hash"] == row["approved_hash"]


def _record_bounce_and_maybe_pause(
    db: Database,
    message_id: int,
    detail: str,
    *,
    external_id: str | None = None,
    occurred_at: str | None = None,
    event_details: dict[str, Any] | None = None,
) -> bool:
    """Record one unique bounce and apply the rolling safety threshold."""
    if external_id and db.row(
        "SELECT 1 FROM delivery_events WHERE channel='email' AND event_type='bounce' "
        "AND external_id=? LIMIT 1", (external_id,),
    ):
        return False
    details = {"detail": detail[:1000]}
    details.update(event_details or {})
    db.record_event(
        "email", "bounce", message_id=message_id, external_id=external_id,
        details=details, occurred_at=occurred_at,
    )
    sent = db.rows(
        "SELECT id FROM messages WHERE channel='email' AND sent_at IS NOT NULL "
        "ORDER BY sent_at DESC LIMIT 20")
    ids = [row["id"] for row in sent]
    if not ids:
        return True
    placeholders = ",".join("?" for _ in ids)
    count = db.row(f"SELECT COUNT(DISTINCT message_id) AS n FROM delivery_events "
                   f"WHERE event_type='bounce' AND message_id IN ({placeholders})", ids)["n"]
    if int(count) >= 2:
        db.set_channel("email", paused=True,
                       reason="two bounces occurred in the rolling last 20 sent messages")
    return True


def send_due(db: Database, limit: int = 20) -> dict[str, Any]:
    ready, reason = _channel_ready(db, "email")
    if not ready:
        return {"sent": 0, "blocked": reason}
    settings = db.settings()
    if not str(settings.get("business_postal_address") or "").strip():
        reason = "A valid business postal address is not configured"
        db.set_channel("email", paused=True, reason=reason, credential_status="missing")
        return {"sent": 0, "blocked": reason}
    password = keychain_get()
    if not password:
        db.set_channel("email", paused=True, reason="Gmail app password is missing",
                       credential_status="missing")
        return {"sent": 0, "blocked": "Gmail app password is missing"}
    rows = db.rows(
        "SELECT m.*,p.market,p.registrable_domain,p.corporate_type FROM messages m "
        "JOIN prospects p ON p.id=m.prospect_id WHERE m.channel='email' AND m.status='scheduled' "
        "AND m.scheduled_for<=? AND NOT EXISTS ("
        "SELECT 1 FROM delivery_events e WHERE e.message_id=m.id "
        "AND e.channel='email' AND e.event_type='gmail_scheduled' "
        "AND e.id>COALESCE((SELECT MAX(c.id) FROM delivery_events c "
        "WHERE c.message_id=m.id AND c.channel='email' "
        "AND c.event_type='gmail_schedule_cancelled'),0)) "
        "ORDER BY m.scheduled_for LIMIT ?", (iso(), limit),
    )
    stale = next((row for row in rows if int(row["attempt_count"]) > 0), None)
    if stale:
        reason = "a prior SMTP attempt ended without a definitive recorded outcome"
        db.mark_failed(stale["id"], reason, count_attempt=False)
        db.set_channel("email", paused=True, reason=reason)
        return {"sent": 0, "failed": 1, "blocked": reason}
    sent = failed = 0
    latest = db.row(
        "SELECT MAX(updated_at) AS latest FROM messages "
        "WHERE channel='email' AND attempt_count>0")
    pacing_cursor = parse_iso(latest["latest"] if latest else None)
    rng = random.SystemRandom()
    for raw in rows:
        row = dict(raw)
        lint_errors = validate_stored_message(db, row["id"])
        if lint_errors:
            db.mark_failed(row["id"], "; ".join(lint_errors))
            failed += 1
            continue
        if not _exact_hash(row, settings):
            db.mark_failed(row["id"], "immutable approved content hash mismatch")
            failed += 1
            continue
        if db.is_suppressed("email", row["to_address"]) or db.is_suppressed("domain", row["registrable_domain"]):
            db.suppress("domain", row["registrable_domain"], "suppression found before delivery", "worker")
            failed += 1
            continue
        errors = regional_gate(settings, row["market"], corporate_type=row["corporate_type"],
                               email=row["to_address"])
        if errors:
            db.mark_failed(row["id"], "; ".join(errors))
            failed += 1
            continue
        if not in_business_window(utcnow(), row["market"], settings["market_policies"],
                                  int(settings["business_hour_start"]), int(settings["business_hour_end"])):
            # The scheduler will move this to the next valid recipient-local window.
            continue
        now = utcnow()
        pacing_min = int(settings["email_pacing_min_minutes"])
        if pacing_cursor and now < pacing_cursor + timedelta(minutes=pacing_min):
            pacing_cursor = max(now, pacing_cursor) + timedelta(minutes=rng.randint(
                pacing_min, int(settings["email_pacing_max_minutes"])))
            when = next_business_window(
                pacing_cursor, row["market"], settings["market_policies"],
                int(settings["business_hour_start"]), int(settings["business_hour_end"]),
            )
            db.reschedule(row["id"], iso(when), "rescheduled to preserve actual SMTP pacing")
            pacing_cursor = when
            continue
        if row["kind"] == "initial":
            duplicate = db.row(
                "SELECT 1 FROM messages m JOIN prospects p ON p.id=m.prospect_id "
                "WHERE p.registrable_domain=? AND m.id!=? AND m.kind='initial' "
                "AND m.status IN ('sent','replied') AND m.sent_at>=? LIMIT 1",
                (row["registrable_domain"], row["id"],
                 iso(utcnow() - timedelta(days=int(settings.get("domain_cooldown_days", 90))))),
            )
            if duplicate:
                db.mark_failed(row["id"], "another initial message already reached this company")
                failed += 1
                continue
        try:
            db.reserve_daily_action("email", "smtp_attempt", iso()[:10],
                                    int(settings["email_daily_cap"]))
            db.reserve_message_attempt(row["id"])
        except StateError:
            break
        pacing_cursor = utcnow()
        parent_external = ""
        if row["parent_message_id"]:
            parent = db.row("SELECT external_id FROM messages WHERE id=?", (row["parent_message_id"],))
            parent_external = parent["external_id"] if parent and parent["external_id"] else ""
        message = build_message(row, settings, parent_external)
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=30) as smtp:
                smtp.login(settings["sender_email"], password)
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            db.mark_failed(row["id"], f"SMTP authentication failed: {exc}", count_attempt=False)
            db.set_channel("email", paused=True, reason="SMTP authentication failure",
                           credential_status="failed")
            failed += 1
            break
        except smtplib.SMTPResponseException as exc:
            detail = f"SMTP {exc.smtp_code}: {exc.smtp_error!r}"
            db.mark_failed(row["id"], detail, count_attempt=False)
            if AUTH_QUOTA_RE.search(detail):
                db.set_channel("email", paused=True, reason="SMTP authentication or quota failure")
            elif exc.smtp_code >= 500:
                _record_bounce_and_maybe_pause(db, row["id"], detail)
                db.suppress("email", row["to_address"], "SMTP hard bounce", "smtp")
            failed += 1
            if db.channel("email")["paused"]:
                break
            continue
        except Exception as exc:
            # Submission state can be uncertain after a network error. Do not retry and risk a duplicate.
            db.mark_failed(row["id"], f"uncertain SMTP submission state: {exc}",
                           count_attempt=False)
            db.set_channel("email", paused=True, reason="uncertain SMTP submission state")
            failed += 1
            break
        external_id = str(message["Message-ID"])
        db.mark_delivered(row["id"], external_id)
        db.set_thread_id(row["id"], parent_external or external_id)
        db.record_event("email", "sent", message_id=row["id"], external_id=external_id,
                        details={"content_hash": row["content_hash"]})
        sent += 1
    return {"sent": sent, "failed": failed, "due": len(rows)}


def _plain_text(message: Message) -> str:
    if message.is_multipart():
        chunks = []
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    chunks.append(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace"))
                except Exception:
                    continue
        return "\n".join(chunks)
    payload = message.get_payload(decode=True)
    return payload.decode(message.get_content_charset() or "utf-8", "replace") if payload else ""


def _match_original(db: Database, message: Message) -> dict[str, Any] | None:
    references = " ".join([message.get("In-Reply-To", ""), message.get("References", "")])
    ids = re.findall(r"<[^>]+>", references)
    for external_id in reversed(ids):
        row = db.row("SELECT * FROM messages WHERE external_id=?", (external_id,))
        if row:
            return dict(row)
    sender = normalize_email(parseaddr(message.get("From", ""))[1])
    if sender:
        row = db.row(
            "SELECT * FROM messages WHERE channel='email' AND to_address=? "
            "AND status IN ('sent','replied') ORDER BY sent_at DESC LIMIT 1", (sender,),
        )
        return dict(row) if row else None
    return None


def poll_inbox(db: Database, limit: int = 100) -> dict[str, int | str]:
    password = keychain_get()
    if not password:
        db.set_channel("email", paused=True, reason="Gmail app password is missing",
                       credential_status="missing")
        return {"processed": 0, "error": "missing Gmail app password"}
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, ssl_context=ssl.create_default_context(), timeout=30)
        imap.login(SENDER_EMAIL, password)
        imap.select("INBOX", readonly=True)
    except Exception as exc:
        db.set_channel("email", paused=True, reason=f"IMAP authentication failure: {exc}",
                       credential_status="failed")
        return {"processed": 0, "error": str(exc)}
    last_uid = int(db.setting("imap_last_uid", 0) or 0)
    status, data = imap.uid("search", None, f"UID {last_uid + 1}:*")
    if status != "OK":
        imap.logout()
        return {"processed": 0, "error": "IMAP UID search failed"}
    uids = [int(x) for x in (data[0] or b"").split() if int(x) > last_uid][:limit]
    processed = replies = bounces = optouts = 0
    for uid in uids:
        status, parts = imap.uid("fetch", str(uid), "(RFC822)")
        if status != "OK" or not parts or not isinstance(parts[0], tuple):
            continue
        message = email.message_from_bytes(parts[0][1])
        original = _match_original(db, message)
        db.set_setting("imap_last_uid", uid)
        processed += 1
        if not original:
            continue
        body = _plain_text(message).strip()
        subject = message.get("Subject", "")
        sender = parseaddr(message.get("From", ""))[1]
        is_bounce = bool(BOUNCE_RE.search(subject) or re.search(r"mailer-daemon|postmaster", sender, re.I))
        if is_bounce:
            _record_bounce_and_maybe_pause(db, original["id"], subject or body[:300])
            db.suppress("email", original["to_address"], "delivery bounce", "imap")
            bounces += 1
            continue
        auto_submitted = message.get("Auto-Submitted", "no").lower() != "no"
        if auto_submitted or re.search(r"out of (?:the )?office|automatic reply", subject, re.I):
            db.record_event("email", "automated_reply", message_id=original["id"],
                            details={"subject": subject[:300]})
            continue
        db.mark_replied(original["id"])
        db.record_event("email", "human_reply", message_id=original["id"],
                        external_id=message.get("Message-ID"), details={"subject": subject[:300]})
        replies += 1
        if OPTOUT_RE.search(body):
            db.record_event(
                "email", "opt_out", message_id=original["id"],
                external_id=message.get("Message-ID"),
                details={"source": "reply"},
            )
            db.suppress("email", original["to_address"], "reply opt-out", "imap")
            prospect = db.row("SELECT registrable_domain FROM prospects WHERE id=?", (original["prospect_id"],))
            if prospect:
                db.suppress("domain", prospect["registrable_domain"], "reply opt-out", "imap")
            optouts += 1
            continue
        evidence_id = db.add_evidence(
            original["prospect_id"], "inbound_reply", f"gmail://inbox/{uid}",
            body[:1200] or subject, "high", subject, detected_at=iso(),
        )
        request = inbound_reply_request(db, original, evidence_id, body)
        generate_and_store(db, [request])
    imap.logout()
    return {"processed": processed, "replies": replies, "bounces": bounces, "optouts": optouts}
