from __future__ import annotations

import csv
import hashlib
import json
import random
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from prospecting.config import LINKEDIN_ACCESS_TOKEN_SERVICE, REPO_ROOT
from prospecting.copy_engine import (
    BOOKING_COPY_STYLE, CodexCopyError, CopyRequest, comment_requests, email_requests, generate_and_store, lint_comment,
    lint_email, prepare, rewrite_email_messages,
    validate_stored_message,
)
from prospecting.dashboard import create_app
from prospecting.db import Database, StateError
from prospecting.discovery import (
    MetaNetworkUnavailable, _goto_meta_with_retry, exa_queries, infer_market,
    infer_post_market, parse_apify_date, parse_exa_output,
)
from prospecting.email_delivery import _record_bounce_and_maybe_pause, poll_inbox, send_due
from prospecting.exporter import export_email_contacts
from prospecting.gmail_queue import (
    fill_gmail_schedule, preflight_connector_send, rebuild_gmail_schedule,
    reconcile_gmail_snapshot, record_connector_send,
)
from prospecting.migration import import_legacy
from prospecting.linkedin_delivery import (
    add_manual_post, confirm_manual_post, post_due, setup_linkedin,
)
from prospecting.linkedin_api import (
    LinkedInHTTPError, _create_comment, setup_linkedin_api, target_urn_from_url,
)
from prospecting.launchd import (
    _ensure_runtime_git_repository, definitions as launchd_definitions,
)
from prospecting.policy import address_problem, published_address_matches_business, regional_gate
from prospecting.scheduling import release_messages, reschedule_overdue
from prospecting.util import (
    content_hash, in_business_window, iso, next_business_window, normalize_domain,
    normalize_email, parse_iso, registrable_domain, utcnow,
)
from prospecting.worker import run_worker


GOOD_BODY = """Hi there,

Example Company publishes WhatsApp, Instagram, email, and a contact form as ways for customers to get in touch. If the same request moves between those routes, assigning an owner and keeping the next action attached to the conversation can take extra coordination.

I'm building wrrk.ai to keep customer conversations connected to the follow-up work for small teams. Would a 15-minute tailored demo using those contact paths be useful?

Wachas
Founding engineer, wrrk.ai"""
GOOD_SUBJECT = "A cleaner customer handoff"
GOOD_BOOKING_BODY = """Hi there,

Example Company publishes WhatsApp, Instagram, email, and a contact form for customer questions. When one request moves between those routes, keeping ownership and the next action clear can take extra coordination.

I'm building wrrk.ai to keep customer conversations connected to the follow-up work for small teams. Would a 15-minute tailored demo around those contact paths be useful?
Book a convenient time: https://wrrk.ai/book/wrrkaidemo

Wachas
Founding engineer, wrrk.ai"""


class TempDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "db.sqlite3")
        self.db.initialize()
        self.db.set_setting("business_postal_address", "1 Test Street, Bengaluru 560001, India")

    def tearDown(self):
        self.tmp.cleanup()

    def qualified(self, market: str = "IN", corporate_type: str = "unknown") -> tuple[int, int, int, int]:
        campaign = self.db.ensure_campaign("fresh")
        serial = len(self.db.rows("select id from prospects"))
        domain = f"company{serial}.example{serial}.com"
        pid, _ = self.db.upsert_prospect(
            campaign, domain=domain,
            company="Example Company", market=market, pool="service_smb",
        )
        evidence_id = self.db.add_evidence(
            pid, "channel_summary", f"https://{domain}/contact",
            "WhatsApp, Instagram, email and a contact form are visibly published.", "high",
        )
        cid = self.db.add_contact(
            pid, f"hello{pid}@{domain}", kind="role",
            published_url=f"https://{domain}/contact",
            excerpt=f"Contact hello{pid}@{domain}", mx=True, primary=True,
        )
        self.db.transition_prospect(pid, "audited", fit_score=90, confidence="high",
                                    corporate_type=corporate_type, audited_at=iso())
        self.db.transition_prospect(pid, "qualified", qualified_at=iso())
        return campaign, pid, cid, evidence_id

    def pending_message(self, market: str = "IN", corporate_type: str = "unknown") -> int:
        campaign, pid, cid, evidence_id = self.qualified(market, corporate_type)
        contact = self.db.row("SELECT email FROM contacts WHERE id=?", (cid,))
        mid = self.db.create_message(
            campaign, "email", "initial", GOOD_BODY, prospect_id=pid, contact_id=cid,
            to_address=contact["email"], subject=GOOD_SUBJECT, evidence_ids=[evidence_id],
        )
        self.db.mark_pending(mid)
        return mid


class NormalizationTests(unittest.TestCase):
    def test_domain_email_and_registrable_normalization(self):
        self.assertEqual(normalize_domain("HTTPS://WWW.Example.CO.UK/path?q=1"), "example.co.uk")
        self.assertEqual(registrable_domain("shop.eu.example.co.uk"), "example.co.uk")
        self.assertEqual(normalize_email("Sales <SALES@Exämple.com>"), "sales@xn--exmple-cua.com")
        self.assertEqual(normalize_email("%20info@example.com"), "")
        self.assertEqual(normalize_email("not an email"), "")
        self.assertEqual(normalize_domain("workflow：not a URL"), "")
        self.assertEqual(normalize_domain({"unexpected": "shape"}), "")

    def test_recipient_local_weekday_window(self):
        policies = {"IN": {"timezone": "Asia/Kolkata"}}
        friday_late = datetime(2026, 8, 14, 15, 0, tzinfo=UTC)  # 20:30 Friday in India
        next_time = next_business_window(friday_late, "IN", policies, 9, 17)
        self.assertEqual(next_time.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).weekday(), 0)
        self.assertEqual(next_time.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Kolkata")).hour, 9)


class LifecycleTests(TempDatabaseTest):
    def test_email_approval_requires_postal_address(self):
        mid = self.pending_message()
        self.db.set_setting("business_postal_address", "")
        with self.assertRaises(StateError):
            self.db.approve_message(mid)

    def test_approved_edit_invalidates_hash_and_release(self):
        mid = self.pending_message()
        self.assertEqual(validate_stored_message(self.db, mid), [])
        approved = self.db.approve_message(mid)
        row = self.db.row("SELECT * FROM messages WHERE id=?", (mid,))
        self.assertEqual(approved, row["approved_hash"])
        changed = self.db.edit_message(mid, GOOD_SUBJECT, GOOD_BODY.replace("useful?", "helpful?"))
        row = self.db.row("SELECT * FROM messages WHERE id=?", (mid,))
        self.assertEqual(row["status"], "pending_approval")
        self.assertIsNone(row["approved_hash"])
        self.assertNotEqual(changed, approved)
        with self.assertRaises(StateError):
            self.db.release_message(mid, iso())

    def test_gmail_scheduled_copy_requires_cancellation_before_edit(self):
        mid = self.pending_message()
        self.db.approve_message(mid)
        self.db.release_message(mid, iso())
        self.db.record_event(
            "email", "gmail_scheduled", message_id=mid,
            external_id="gmail-thread", details={"account": "wachas@wrrk.ai"},
        )
        with self.assertRaisesRegex(StateError, "cancel the Gmail scheduled send"):
            self.db.edit_message(mid, GOOD_SUBJECT, GOOD_BODY)
        self.db.record_event(
            "email", "gmail_schedule_cancelled", message_id=mid,
            external_id="gmail-thread", details={"account": "wachas@wrrk.ai"},
        )
        self.db.edit_message(mid, "A simpler customer handoff", GOOD_BODY)
        row = self.db.row(
            "SELECT status,approved_hash,scheduled_for FROM messages WHERE id=?", (mid,)
        )
        self.assertEqual((row["status"], row["approved_hash"], row["scheduled_for"]),
                         ("pending_approval", None, None))

    def test_deferred_gmail_draft_can_be_rewritten(self):
        mid = self.pending_message()
        self.db.approve_message(mid)
        self.db.release_message(mid, iso())
        self.db.record_event(
            "email", "gmail_schedule_deferred", message_id=mid,
            details={"reason": "scheduled-send capacity"},
        )
        with self.db.transaction(immediate=True) as conn:
            conn.execute("UPDATE messages SET last_error='old queue error' WHERE id=?", (mid,))
        self.db.edit_message(mid, "A simpler customer handoff", GOOD_BODY, [1])
        row = self.db.row("SELECT status,last_error FROM messages WHERE id=?", (mid,))
        self.assertEqual((row["status"], row["last_error"]), ("pending_approval", None))

    def test_release_and_attempt_caps_persist(self):
        self.db.set_setting("email_daily_cap", 1)
        first = self.pending_message()
        self.db.approve_message(first)
        result = release_messages(self.db, [first], "email")
        self.assertEqual(result["released"], [first])
        second = self.pending_message()
        self.db.approve_message(second)
        result = release_messages(self.db, [second], "email")
        self.assertFalse(result["released"])
        self.assertIn("cap", result["blocked"][second])
        self.assertEqual(self.db.reserve_daily_action("email", "smtp_attempt", iso()[:10], 1), 1)
        with self.assertRaises(StateError):
            self.db.reserve_daily_action("email", "smtp_attempt", iso()[:10], 1)

    def test_connector_preflight_reservation_is_idempotent(self):
        self.db.set_setting("business_hour_start", 0)
        self.db.set_setting("business_hour_end", 24)
        self.db.set_channel(
            "email", paused=True, reason="connector delivery",
            credential_status="gmail_connector",
        )
        mid = self.pending_message()
        self.db.approve_message(mid)
        self.db.release_message(mid, iso())
        with patch("prospecting.gmail_queue.in_business_window", return_value=True):
            first = preflight_connector_send(self.db, mid)
            second = preflight_connector_send(self.db, mid)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        counter = self.db.row(
            "SELECT count FROM daily_counters WHERE channel='email' AND action='gmail_send'"
        )
        self.assertEqual(counter["count"], 1)
        record_connector_send(self.db, mid, "gmail-id", "thread-id", "draft-id")
        row = self.db.row("SELECT status,external_id,thread_id FROM messages WHERE id=?", (mid,))
        self.assertEqual(
            (row["status"], row["external_id"], row["thread_id"]),
            ("sent", "gmail-id", "thread-id"),
        )

    def test_connector_preflight_respects_safety_pause(self):
        self.db.set_setting("business_hour_start", 0)
        self.db.set_setting("business_hour_end", 24)
        self.db.set_channel(
            "email", paused=True,
            reason="two bounces occurred in the rolling last 20 sent messages",
            credential_status="gmail_connector",
        )
        mid = self.pending_message()
        self.db.approve_message(mid)
        self.db.release_message(mid, iso())
        result = preflight_connector_send(self.db, mid)
        self.assertFalse(result["ok"])
        self.assertIn("two bounces", " ".join(result["errors"]))

    def _gmail_native_snapshot_row(self, message_id: int, serial: int) -> dict[str, str]:
        row = self.db.row(
            "SELECT to_address,subject,content_hash FROM messages WHERE id=?", (message_id,)
        )
        gmail_id = f"gmail-scheduled-{serial}"
        thread_id = f"gmail-thread-{serial}"
        self.db.set_thread_id(message_id, thread_id)
        self.db.record_event(
            "email", "gmail_scheduled", message_id=message_id, external_id=gmail_id,
            details={
                "content_hash": row["content_hash"], "gmail_thread_id": thread_id,
                "account": "wachas@wrrk.ai",
            },
        )
        return {
            "id": gmail_id, "thread_id": thread_id, "to": [row["to_address"]],
            "subject": row["subject"], "email_ts": iso(utcnow() - timedelta(minutes=serial)),
        }

    def test_gmail_native_reconciliation_is_exact_and_idempotent(self):
        mid = self.pending_message()
        self.db.approve_message(mid)
        self.db.release_message(mid, iso())
        sent = self._gmail_native_snapshot_row(mid, 1)
        snapshot = {"account": "wachas@wrrk.ai", "sent": [sent]}

        first = reconcile_gmail_snapshot(self.db, snapshot)
        second = reconcile_gmail_snapshot(self.db, snapshot)

        row = self.db.row(
            "SELECT status,external_id,thread_id,sent_at FROM messages WHERE id=?", (mid,)
        )
        self.assertEqual(first["sent"], [mid])
        self.assertEqual(second["sent"], [])
        self.assertEqual(second["errors"], [])
        self.assertEqual((row["status"], row["external_id"], row["thread_id"]),
                         ("sent", sent["id"], sent["thread_id"]))
        self.assertEqual(row["sent_at"], iso(parse_iso(sent["email_ts"])))
        self.assertEqual(
            self.db.row(
                "SELECT COUNT(*) AS n FROM delivery_events WHERE event_type='sent' "
                "AND external_id=?", (sent["id"],),
            )["n"],
            1,
        )

    def test_gmail_native_reconciliation_rejects_recipient_mismatch(self):
        mid = self.pending_message()
        self.db.approve_message(mid)
        self.db.release_message(mid, iso())
        sent = self._gmail_native_snapshot_row(mid, 1)
        sent["to"] = ["someone-else@example.com"]
        result = reconcile_gmail_snapshot(
            self.db, {"account": "wachas@wrrk.ai", "sent": [sent]},
        )
        self.assertIn("recipient mismatch", " ".join(result["errors"]))
        self.assertEqual(
            self.db.row("SELECT status FROM messages WHERE id=?", (mid,))["status"],
            "scheduled",
        )

    def test_gmail_snapshot_reconciles_bounces_and_pauses(self):
        sent_rows = []
        message_ids = []
        for serial in (1, 2):
            mid = self.pending_message()
            self.db.approve_message(mid)
            self.db.release_message(mid, iso())
            message_ids.append(mid)
            sent_rows.append(self._gmail_native_snapshot_row(mid, serial))
        bounces = [
            {
                "id": f"bounce-{serial}", "thread_id": sent["thread_id"],
                "failed_address": sent["to"][0], "detail": "Address not found",
                "email_ts": iso(utcnow() - timedelta(seconds=serial)),
            }
            for serial, sent in enumerate(sent_rows, 1)
        ]
        result = reconcile_gmail_snapshot(
            self.db,
            {"account": "wachas@wrrk.ai", "sent": sent_rows, "bounces": bounces},
        )
        self.assertEqual(result["sent"], message_ids)
        self.assertEqual(result["bounces"], message_ids)
        self.assertEqual(result["errors"], [])
        self.assertTrue(self.db.channel("email")["paused"])
        self.assertIn("two bounces", self.db.channel("email")["reason"])
        for sent in sent_rows:
            self.assertTrue(self.db.is_suppressed("email", sent["to"][0]))

    def test_gmail_snapshot_reply_cancels_followups_without_sending(self):
        mid = self.pending_message()
        self.db.approve_message(mid)
        self.db.release_message(mid, iso())
        sent = self._gmail_native_snapshot_row(mid, 1)
        initial = reconcile_gmail_snapshot(
            self.db, {"account": "wachas@wrrk.ai", "sent": [sent]},
        )
        self.assertEqual(initial["errors"], [])
        reply = {
            "id": "gmail-reply-1", "thread_id": sent["thread_id"],
            "from": sent["to"][0], "subject": f"Re: {sent['subject']}",
            "body": "Please send the proposal to our marketing team.",
            "email_ts": iso(), "details": {"classification": "referral"},
        }
        result = reconcile_gmail_snapshot(
            self.db, {"account": "wachas@wrrk.ai", "replies": [reply]},
        )
        self.assertEqual(result["replies"], [mid])
        self.assertEqual(result["errors"], [])
        self.assertEqual(
            self.db.row("SELECT status FROM messages WHERE id=?", (mid,))["status"],
            "replied",
        )
        self.assertEqual(
            self.db.row(
                "SELECT COUNT(*) AS n FROM delivery_events WHERE event_type='human_reply' "
                "AND external_id='gmail-reply-1'"
            )["n"],
            1,
        )

    def test_separate_release_calls_share_one_pacing_cursor(self):
        self.db.set_setting("email_daily_cap", 2)
        self.db.set_setting("email_pacing_min_minutes", 7)
        self.db.set_setting("email_pacing_max_minutes", 7)
        settings = self.db.settings()
        settings["market_policies"]["IN"]["timezone"] = "UTC"
        self.db.set_setting("market_policies", settings["market_policies"])
        self.db.set_setting("business_hour_start", 0)
        self.db.set_setting("business_hour_end", 24)
        first, second = self.pending_message(), self.pending_message()
        self.db.approve_message(first)
        self.db.approve_message(second)
        self.assertEqual(release_messages(self.db, [first], "email")["released"], [first])
        self.assertEqual(release_messages(self.db, [second], "email")["released"], [second])
        one = parse_iso(self.db.row("SELECT scheduled_for FROM messages WHERE id=?", (first,))[0])
        two = parse_iso(self.db.row("SELECT scheduled_for FROM messages WHERE id=?", (second,))[0])
        self.assertGreaterEqual(two - one, timedelta(minutes=7))

    def test_gmail_queue_rebuild_preserves_hashes_and_global_pacing(self):
        self.db.set_setting("email_daily_cap", 2)
        self.db.set_setting("email_pacing_min_minutes", 7)
        self.db.set_setting("email_pacing_max_minutes", 7)
        settings = self.db.settings()
        settings["market_policies"]["IN"]["timezone"] = "UTC"
        self.db.set_setting("market_policies", settings["market_policies"])
        self.db.set_setting("business_hour_start", 0)
        self.db.set_setting("business_hour_end", 24)
        first, second = self.pending_message(), self.pending_message()
        for message_id in (first, second):
            self.db.approve_message(message_id)
            self.db.release_message(message_id, iso())
        before = {
            row["id"]: (row["content_hash"], row["approved_hash"])
            for row in self.db.rows(
                "SELECT id,content_hash,approved_hash FROM messages WHERE id IN (?,?)",
                (first, second),
            )
        }
        plan = rebuild_gmail_schedule(self.db, start_delay_minutes=0)
        self.assertEqual(2, len(plan))
        self.assertGreaterEqual(
            parse_iso(plan[1]["scheduled_for"]) - parse_iso(plan[0]["scheduled_for"]),
            timedelta(minutes=7),
        )
        after = {
            row["id"]: (row["content_hash"], row["approved_hash"])
            for row in self.db.rows(
                "SELECT id,content_hash,approved_hash FROM messages WHERE id IN (?,?)",
                (first, second),
            )
        }
        self.assertEqual(before, after)
        self.assertEqual(
            2,
            self.db.row(
                "SELECT COUNT(*) n FROM delivery_events WHERE event_type='gmail_rescheduled'"
            )["n"],
        )

    def test_gmail_queue_rebuild_enforces_daily_cap(self):
        base = datetime(2026, 8, 20, 0, tzinfo=UTC)
        self.db.set_setting("email_daily_cap", 2)
        self.db.set_setting("email_pacing_min_minutes", 7)
        self.db.set_setting("email_pacing_max_minutes", 7)
        settings = self.db.settings()
        settings["market_policies"]["IN"]["timezone"] = "UTC"
        self.db.set_setting("market_policies", settings["market_policies"])
        self.db.set_setting("business_hour_start", 0)
        self.db.set_setting("business_hour_end", 24)
        message_ids = [self.pending_message() for _ in range(3)]
        for message_id in message_ids:
            self.db.approve_message(message_id)
            self.db.release_message(message_id, iso(base))

        with patch("prospecting.gmail_queue.utcnow", return_value=base):
            plan = rebuild_gmail_schedule(self.db, start_delay_minutes=0)

        account_tz = ZoneInfo("Asia/Kolkata")
        counts = {}
        for row in plan:
            day = parse_iso(row["scheduled_for"]).astimezone(account_tz).date()
            counts[day] = counts.get(day, 0) + 1
        self.assertEqual(3, len(plan))
        self.assertLessEqual(max(counts.values()), 2)
        self.assertEqual(2, len(counts))

    def test_gmail_queue_rebuild_uses_minute_boundaries(self):
        base = datetime(2026, 8, 20, 9, 0, 31, 123456, tzinfo=UTC)
        self.db.set_setting("email_pacing_min_minutes", 7)
        self.db.set_setting("email_pacing_max_minutes", 7)
        settings = self.db.settings()
        settings["market_policies"]["IN"]["timezone"] = "UTC"
        self.db.set_setting("market_policies", settings["market_policies"])
        self.db.set_setting("business_hour_start", 0)
        self.db.set_setting("business_hour_end", 24)
        message_id = self.pending_message()
        self.db.approve_message(message_id)
        self.db.release_message(message_id, iso(base))

        with patch("prospecting.gmail_queue.utcnow", return_value=base):
            plan = rebuild_gmail_schedule(self.db, start_delay_minutes=0)

        scheduled = parse_iso(plan[0]["scheduled_for"])
        self.assertEqual((scheduled.second, scheduled.microsecond), (0, 0))
        self.assertGreaterEqual(scheduled, base + timedelta(minutes=7))

    def test_gmail_gap_fill_keeps_existing_schedule_and_uses_earlier_space(self):
        base = datetime(2026, 8, 20, 9, tzinfo=UTC)
        self.db.set_setting("email_daily_cap", 100)
        self.db.set_setting("email_pacing_min_minutes", 7)
        self.db.set_setting("email_pacing_max_minutes", 7)
        settings = self.db.settings()
        settings["market_policies"]["IN"]["timezone"] = "UTC"
        self.db.set_setting("market_policies", settings["market_policies"])
        self.db.set_setting("business_hour_start", 0)
        self.db.set_setting("business_hour_end", 24)
        fixed = self.pending_message()
        additions = [self.pending_message(), self.pending_message()]
        for message_id in (fixed, *additions):
            self.db.approve_message(message_id)
            self.db.release_message(message_id, iso(base + timedelta(days=5)))
        self.db.reschedule(fixed, iso(base + timedelta(minutes=30)))

        with patch("prospecting.gmail_queue.utcnow", return_value=base):
            plan = fill_gmail_schedule(
                self.db, additions, start_delay_minutes=0, rng=random.Random(1)
            )

        self.assertEqual(iso(base + timedelta(minutes=30)), self.db.row(
            "SELECT scheduled_for FROM messages WHERE id=?", (fixed,)
        )["scheduled_for"])
        self.assertEqual(
            [iso(base + timedelta(minutes=7)), iso(base + timedelta(minutes=14))],
            [row["scheduled_for"] for row in plan],
        )
        combined = sorted([
            parse_iso(iso(base + timedelta(minutes=30))),
            *(parse_iso(row["scheduled_for"]) for row in plan),
        ])
        self.assertTrue(all(
            later - earlier >= timedelta(minutes=7)
            for earlier, later in zip(combined, combined[1:])
        ))

    def test_suppression_cancels_scheduled_copy(self):
        mid = self.pending_message()
        self.db.approve_message(mid)
        self.db.release_message(mid, iso())
        row = self.db.row("SELECT p.registrable_domain FROM messages m JOIN prospects p ON p.id=m.prospect_id WHERE m.id=?", (mid,))
        self.db.suppress("domain", row["registrable_domain"], "test opt-out")
        self.assertEqual(self.db.row("SELECT status FROM messages WHERE id=?", (mid,))["status"], "suppressed")

    def test_rejecting_initial_rejects_its_prospect(self):
        mid = self.pending_message()
        pid = self.db.row("SELECT prospect_id FROM messages WHERE id=?", (mid,))["prospect_id"]
        self.db.reject_message(mid, "not a fit")
        self.assertEqual(self.db.row("SELECT status FROM messages WHERE id=?", (mid,))["status"], "rejected")
        self.assertEqual(self.db.row("SELECT status FROM prospects WHERE id=?", (pid,))["status"], "rejected")

    def test_linkedin_suppression_cancels_pending_comment(self):
        campaign = self.db.ensure_campaign("fresh")
        author = "https://www.linkedin.com/in/suppressed-author"
        with self.db.transaction(immediate=True) as conn:
            post_id = conn.execute(
                "INSERT INTO posts(author_url,post_url,text,text_hash,published_at,market,role,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (author, "https://www.linkedin.com/posts/suppressed-author_test", "A test post",
                 "hash", iso(), "IN", "influencer", "discovered", iso()),
            ).lastrowid
        mid = self.db.create_message(campaign, "linkedin", "comment", "A specific test comment with enough detail.",
                                     post_id=post_id, to_address=author, evidence_ids=[-post_id])
        self.db.mark_pending(mid)
        self.db.suppress("linkedin", author, "manual suppression")
        self.assertEqual(self.db.row("SELECT status FROM messages WHERE id=?", (mid,))["status"], "suppressed")


class PolicyAndLintTests(unittest.TestCase):
    def test_regional_gates(self):
        settings = {"market_policies": {"GB": {"enabled": True}, "US": {"enabled": True}},
                    "business_postal_address": ""}
        self.assertTrue(any("incorporated" in x for x in regional_gate(
            settings, "GB", corporate_type="unknown", email="hello@example.co.uk")))
        self.assertTrue(any("freemail" in x for x in regional_gate(
            settings, "GB", corporate_type="limited_company", email="company@gmail.com")))
        self.assertTrue(any("postal" in x for x in regional_gate(
            settings, "US", corporate_type="corporation", email="hello@example.com")))

    def test_related_brand_domain_is_not_mistaken_for_site_credit(self):
        # Patch suffix normalization so reserved .test domains can exercise the
        # related-brand heuristic without adding real third-party addresses to tests.
        with patch("prospecting.policy.registrable_domain", side_effect=lambda value: value):
            self.assertTrue(published_address_matches_business(
                "atelier-studio.test", "contact@atelierstudio.test", "Atelier Studio Ltd"))
            self.assertTrue(published_address_matches_business(
                "studio-five-design.test", "info@studiofivedesign.test", "Studio Five"))
            self.assertFalse(published_address_matches_business(
                "pixel-services.test", "owner@unrelated-brand.test", "Pixel Services"))

    def test_email_lint(self):
        self.assertEqual(lint_email(GOOD_SUBJECT, GOOD_BODY, [7], {7}, "hello@example.com"), [])
        self.assertEqual(lint_email(f"Re: {GOOD_SUBJECT}", GOOD_BODY, [7], {7},
                                    "hello@example.com", "followup_1"), [])
        errors = lint_email(GOOD_SUBJECT, GOOD_BODY.replace("tailored demo", "quick chat") + " https://x.com",
                            [8], {7}, "careers@example.com")
        self.assertTrue(any("links" in x for x in errors))
        self.assertTrue(any("evidence" in x for x in errors))
        self.assertTrue(any("inbox" in x for x in errors))
        self.assertTrue(any("inbox" in x for x in lint_email(
            GOOD_SUBJECT, GOOD_BODY, [7], {7}, "support@example.com"
        )))
        self.assertTrue(any("promotional" in x for x in lint_email(
            "15% Limited-Time Offer", GOOD_BODY, [7], {7}, "hello@example.com"
        )))
        self.assertEqual(address_problem("support@example.com"), "wrong-audience inbox")
        self.assertEqual(address_problem("support-team@example.com"), "wrong-audience inbox")
        stale = GOOD_BODY.replace("Example Company publishes", "I noticed Example Company publishes")
        self.assertTrue(any("retired" in x for x in lint_email(
            "Customer inquiry workflow", stale, [7], {7}, "hello@example.com"
        )))
        no_founder_voice = GOOD_BODY.replace("I'm building wrrk.ai", "wrrk.ai helps")
        self.assertTrue(any("founding-engineer voice" in x for x in lint_email(
            GOOD_SUBJECT, no_founder_voice, [7], {7}, "hello@example.com"
        )))
        self.assertEqual(lint_email(
            GOOD_SUBJECT, GOOD_BOOKING_BODY, [7], {7}, "hello@example.com",
            copy_style=BOOKING_COPY_STYLE,
        ), [])
        self.assertTrue(any("approved booking URL" in x for x in lint_email(
            GOOD_SUBJECT, GOOD_BODY, [7], {7}, "hello@example.com",
            copy_style=BOOKING_COPY_STYLE,
        )))
        self.assertTrue(any("unapproved link" in x for x in lint_email(
            GOOD_SUBJECT, GOOD_BOOKING_BODY + "\nhttps://example.com", [7], {7},
            "hello@example.com", copy_style=BOOKING_COPY_STYLE,
        )))

    def test_linkedin_comment_lint(self):
        post = "We learned that customer support ownership matters more than adding another tool."
        comment = "Support ownership is the sharp point here. A clear owner often matters more than another dashboard."
        self.assertEqual(lint_comment(comment, [-1], {-1}, post), [])
        self.assertTrue(lint_comment("Try wrrk.ai https://wrrk.ai", [-1], {-1}, post))


class StructuredCopyTests(TempDatabaseTest):
    def test_mocked_codex_output_is_stored_pending(self):
        campaign, pid, cid, evidence_id = self.qualified()
        request = CopyRequest(
            "email:test", "email", "initial", pid, campaign, cid, None,
            self.db.row("SELECT email FROM contacts WHERE id=?", (cid,))["email"],
            [dict(self.db.row("SELECT * FROM evidence WHERE id=?", (evidence_id,)))],
            {"company": "Example"},
        )
        output = {"items": [{"request_id": "email:test", "subject": GOOD_SUBJECT,
                              "body": GOOD_BOOKING_BODY, "evidence_ids": [evidence_id]}]}
        with patch("prospecting.copy_engine.run_codex", return_value=output):
            result = generate_and_store(self.db, [request])
        self.assertEqual(result["pending_approval"], 1)
        self.assertEqual(self.db.row("SELECT status FROM messages")["status"], "pending_approval")

    def test_mocked_codex_rewrite_invalidates_released_copy(self):
        mid = self.pending_message()
        self.db.approve_message(mid)
        self.db.release_message(mid, iso())
        self.db.record_event(
            "email", "gmail_schedule_deferred", message_id=mid,
            details={"reason": "scheduled-send capacity"},
        )
        evidence_id = self.db.row(
            "SELECT id FROM evidence WHERE prospect_id=(SELECT prospect_id FROM messages WHERE id=?)",
            (mid,),
        )["id"]
        output = {"items": [{
            "request_id": f"rewrite:{mid}", "subject": GOOD_SUBJECT,
            "body": GOOD_BOOKING_BODY, "evidence_ids": [evidence_id],
        }]}
        with patch("prospecting.copy_engine.run_codex", return_value=output):
            result = rewrite_email_messages(self.db, [mid])
        self.assertEqual(result["rewritten"], [mid])
        row = self.db.row(
            "SELECT status,approved_hash,scheduled_for FROM messages WHERE id=?", (mid,)
        )
        self.assertEqual((row["status"], row["approved_hash"], row["scheduled_for"]),
                         ("pending_approval", None, None))

    def test_codex_failure_blocks_without_fallback(self):
        campaign, pid, cid, evidence_id = self.qualified()
        request = CopyRequest(
            "email:test", "email", "initial", pid, campaign, cid, None,
            self.db.row("SELECT email FROM contacts WHERE id=?", (cid,))["email"],
            [dict(self.db.row("SELECT * FROM evidence WHERE id=?", (evidence_id,)))], {},
        )
        with patch("prospecting.copy_engine.run_codex", side_effect=CodexCopyError("auth failed")):
            result = generate_and_store(self.db, [request])
        self.assertEqual(result["blocked"], 1)
        row = self.db.row("SELECT status,last_error FROM messages")
        self.assertEqual(row["status"], "blocked")
        self.assertIn("auth failed", row["last_error"])

    def test_explicit_rewrite_can_recover_transient_codex_generation_block(self):
        campaign, pid, cid, evidence_id = self.qualified()
        request = CopyRequest(
            "email:test", "email", "initial", pid, campaign, cid, None,
            self.db.row("SELECT email FROM contacts WHERE id=?", (cid,))["email"],
            [dict(self.db.row("SELECT * FROM evidence WHERE id=?", (evidence_id,)))], {},
        )
        with patch(
            "prospecting.copy_engine.run_codex",
            side_effect=CodexCopyError("Codex copy generation timed out"),
        ):
            generate_and_store(self.db, [request])
        mid = self.db.row("SELECT id FROM messages")["id"]
        output = {"items": [{
            "request_id": f"rewrite:{mid}", "subject": GOOD_SUBJECT,
            "body": GOOD_BOOKING_BODY, "evidence_ids": [evidence_id],
        }]}
        with patch("prospecting.copy_engine.run_codex", return_value=output):
            result = rewrite_email_messages(self.db, [mid], batch_size=1)
        self.assertEqual(result["rewritten"], [mid])
        row = self.db.row("SELECT status,copy_style,last_error FROM messages WHERE id=?", (mid,))
        self.assertEqual(
            (row["status"], row["copy_style"], row["last_error"]),
            ("pending_approval", BOOKING_COPY_STYLE, None),
        )

    def test_prepare_retries_transiently_blocked_followup_in_place(self):
        parent_id = self.pending_message()
        self.db.approve_message(parent_id)
        self.db.release_message(parent_id, iso())
        self.db.mark_delivered(parent_id, "gmail-parent", delivered_at=iso(
            utcnow() - timedelta(days=11)
        ))
        self.db.set_thread_id(parent_id, "gmail-thread")
        with patch(
            "prospecting.copy_engine.run_codex",
            side_effect=CodexCopyError("Codex copy generation timed out"),
        ):
            first = prepare(self.db, email_limit=1, comment_limit=0)
        self.assertEqual(first["email"]["blocked"], 1)
        blocked = self.db.row(
            "SELECT id,status FROM messages WHERE parent_message_id=? AND kind='followup_2'",
            (parent_id,),
        )
        evidence_id = self.db.row(
            "SELECT id FROM evidence WHERE prospect_id=(SELECT prospect_id FROM messages WHERE id=?)",
            (parent_id,),
        )["id"]
        output = {"items": [{
            "request_id": f"followup_2:{parent_id}", "subject": "ignored by thread rule",
            "body": GOOD_BODY, "evidence_ids": [evidence_id],
        }]}
        with patch("prospecting.copy_engine.run_codex", return_value=output):
            second = prepare(self.db, email_limit=1, comment_limit=0)
        restored = self.db.row(
            "SELECT id,status,last_error,thread_id FROM messages WHERE parent_message_id=? "
            "AND kind='followup_2'", (parent_id,),
        )
        self.assertEqual(second["email"]["pending_approval"], 1)
        self.assertEqual(
            (restored["id"], restored["status"], restored["last_error"], restored["thread_id"]),
            (blocked["id"], "pending_approval", None, "gmail-thread"),
        )
        self.assertEqual(
            self.db.row(
                "SELECT COUNT(*) AS n FROM messages WHERE parent_message_id=? AND kind='followup_2'",
                (parent_id,),
            )["n"],
            1,
        )


class SourceAndMigrationTests(TempDatabaseTest):
    def test_exa_expansion_is_disjoint_and_preserves_mix(self):
        standard = exa_queries(["IN"])
        expansion = exa_queries(["IN"], expansion=True)
        fresh = exa_queries(["IN"], fresh_wave=True)
        conversion = exa_queries(["IN"], conversion_wave=True)
        self.assertEqual(10, len(standard))
        self.assertEqual(10, len(expansion))
        self.assertEqual(10, len(fresh))
        self.assertEqual(10, len(conversion))
        self.assertEqual(
            {"service_smb": 5, "agency_directory": 3, "funded_startup": 2},
            {pool: sum(item[1] == pool for item in expansion)
             for pool in {item[1] for item in expansion}},
        )
        self.assertFalse({item[2] for item in standard} & {item[2] for item in expansion})
        self.assertFalse({item[2] for item in standard} & {item[2] for item in fresh})
        self.assertFalse({item[2] for item in expansion} & {item[2] for item in fresh})
        self.assertFalse({item[2] for item in standard} & {item[2] for item in conversion})
        self.assertFalse({item[2] for item in expansion} & {item[2] for item in conversion})
        self.assertFalse({item[2] for item in fresh} & {item[2] for item in conversion})

    def test_connector_safe_launchd_omits_smtp_worker_and_imap_poller(self):
        agents = launchd_definitions(include_delivery=False)
        labels = set(agents)
        self.assertIn("ai.wrrk.prospecting.dashboard", labels)
        self.assertIn("ai.wrrk.prospecting.discover", labels)
        self.assertIn("ai.wrrk.prospecting.prepare", labels)
        self.assertNotIn("ai.wrrk.prospecting.worker", labels)
        self.assertNotIn("ai.wrrk.prospecting.inbox", labels)
        dashboard = agents["ai.wrrk.prospecting.dashboard"]
        self.assertIn("launchd-runtime", dashboard["WorkingDirectory"])
        self.assertIn("launchd-runtime", dashboard["ProgramArguments"][0])
        node = shutil.which("node")
        if node:
            self.assertIn(str(Path(node).parent), dashboard["EnvironmentVariables"]["PATH"].split(":"))
        mcporter = shutil.which("mcporter")
        if mcporter:
            self.assertEqual(
                dashboard["EnvironmentVariables"]["WRRKHUNT_MCPORTER_BIN"], mcporter,
            )

    def test_launchd_runtime_is_initialized_as_a_git_worktree(self):
        if not shutil.which("git") and not Path("/usr/bin/git").exists():
            self.skipTest("git is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime"
            path.mkdir()
            _ensure_runtime_git_repository(path)
            self.assertTrue((path / ".git").is_dir())

    def test_meta_navigation_retries_transient_offline_state(self):
        class FakePage:
            def __init__(self):
                self.calls = 0
                self.waits = []

            def goto(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("net::ERR_INTERNET_DISCONNECTED")

            def wait_for_timeout(self, milliseconds):
                self.waits.append(milliseconds)

        page = FakePage()
        _goto_meta_with_retry(page, "https://example.test")
        self.assertEqual(page.calls, 2)
        self.assertEqual(page.waits, [5000])

    def test_meta_navigation_stops_after_bounded_retries(self):
        class OfflinePage:
            def goto(self, *args, **kwargs):
                raise RuntimeError("net::ERR_INTERNET_DISCONNECTED")

            def wait_for_timeout(self, milliseconds):
                pass

        with self.assertRaises(MetaNetworkUnavailable):
            _goto_meta_with_retry(OfflinePage(), "https://example.test", attempts=2)

    def test_private_email_export_is_deduplicated_and_gate_labeled(self):
        self.pending_message()
        output = Path(self.tmp.name) / "email-leads.csv"
        result = export_email_contacts(self.db, output)
        with output.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["recommended"], 1)
        self.assertEqual(rows[0]["recommended_for_outreach"], "YES")
        self.assertEqual(rows[0]["recommended_action"], "APPROVE_DRAFT")
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_exa_parser(self):
        payload = json.dumps({"content": [{"type": "text", "text": (
            "Title: Acme Services\nURL: https://acme.example/\nPublished: 2026-08-14\n"
            "Highlights:\nAcme asks customers to contact it on WhatsApp.\n\n---\n\n"
            "Title: Beta\nURL: https://beta.example/\nPublished: N/A\nHighlights:\nOfficial site")}]})
        rows = parse_exa_output(payload)
        self.assertEqual([x.url for x in rows], ["https://acme.example/", "https://beta.example/"])
        self.assertIn("WhatsApp", rows[0].excerpt)

    def test_real_apify_timestamp_shape_and_market_inference(self):
        parsed = parse_apify_date({"timestamp": 1786701614945,
                                   "date": "2026-08-14T10:00:14.945Z"})
        self.assertEqual(parsed.isoformat(), "2026-08-14T10:00:14.945000+00:00")
        self.assertEqual(infer_market("Founder in Singapore"), "SG")
        self.assertEqual(infer_market("", "https://company.ae"), "AE")
        self.assertEqual(infer_market("AI usage patterns in Indiana"), "")
        self.assertEqual(infer_post_market(
            "The founder grew up between Israel and the United States."), "")
        self.assertEqual(infer_post_market("📍 Location: Goregaon East, Mumbai"), "IN")
        self.assertEqual(infer_post_market("Join our team in Singapore!"), "SG")

    def test_zero_prepare_limits_disable_both_channels(self):
        self.qualified()
        with patch("prospecting.copy_engine.run_codex") as codex:
            result = prepare(self.db, email_limit=0, comment_limit=0)
        self.assertEqual(result["requested"], {"email": 0, "followups": 0, "linkedin": 0})
        codex.assert_not_called()

    def test_copy_prompt_context_excludes_internal_price_estimates(self):
        _, pid, _, _ = self.qualified()
        metadata = {"audit": {"fit_why": "four front doors", "gaps": ["CRM not detected"],
                               "channels": {"whatsapp": True},
                               "tools": [{"name": "Example CRM", "category": "crm",
                                          "usd_mo": 500, "per_seat": True}]}}
        with self.db.transaction(immediate=True) as conn:
            conn.execute("UPDATE prospects SET metadata_json=? WHERE id=?", (json.dumps(metadata), pid))
        payload = json.dumps(email_requests(self.db, 1)[0].context)
        self.assertNotIn("usd_mo", payload)
        self.assertNotIn("per_seat", payload)

    def test_script_only_mailto_is_not_publication_evidence(self):
        from enrich.find_contacts import _published_emails
        rows = _published_emails(
            "https://example.com/contact",
            '<script>const x="mailto:hidden@tracker.test"</script>'
            '<a href="mailto:visible@business.test">Email us</a>',
        )
        self.assertEqual([row["email"] for row in rows], ["visible@business.test"])

    def test_mailto_decodes_url_encoded_whitespace_before_validation(self):
        from enrich.find_contacts import _published_emails
        rows = _published_emails(
            "https://business.test/contact",
            '<a href="mailto:%20info@business.test">Email us</a>',
        )
        self.assertEqual([row["email"] for row in rows], ["info@business.test"])

    def test_legacy_migration_counts_and_source_files_unchanged(self):
        files = [REPO_ROOT / "data/stacks.json", REPO_ROOT / "data/contacts.json",
                 REPO_ROOT / "data/tracker.csv", REPO_ROOT / "outreach/batch1.json",
                 REPO_ROOT / "outreach/batch2.json"]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        counts = import_legacy(self.db)
        self.assertEqual(counts, {"stacks": 52, "contacts": 43, "tracker": 32,
                                  "batch1": 37, "batch2": 15})
        self.assertEqual(self.db.row("SELECT COUNT(*) AS n FROM messages")["n"], 52)
        campaign = self.db.row("SELECT status FROM campaigns WHERE name='legacy'")
        self.assertEqual(campaign["status"], "held")
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files})

    def test_public_auditors_do_not_disable_tls_verification(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, *args): return b"<html>contact us</html>"

        with patch("sources.stack_detect.urllib.request.urlopen", return_value=Response()) as opened:
            from sources.stack_detect import _fetch as stack_fetch
            self.assertTrue(stack_fetch("https://example.com"))
            self.assertNotIn("context", opened.call_args.kwargs)
        with patch("enrich.find_contacts.urllib.request.urlopen", return_value=Response()) as opened:
            from enrich.find_contacts import _fetch as contact_fetch
            self.assertTrue(contact_fetch("https://example.com"))
            self.assertNotIn("context", opened.call_args.kwargs)


class DeliveryIntegrationTests(TempDatabaseTest):
    class FakeSMTP:
        messages = []

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, user, password):
            self.user = user

        def send_message(self, message):
            self.messages.append(message)

    def _scheduled(self) -> int:
        mid = self.pending_message()
        self.db.approve_message(mid)
        self.db.release_message(mid, iso(utcnow() - timedelta(minutes=1)))
        settings = self.db.settings()
        policies = settings["market_policies"]
        policies["IN"]["timezone"] = "UTC"
        self.db.set_setting("market_policies", policies)
        self.db.set_setting("business_hour_start", 0)
        self.db.set_setting("business_hour_end", 24)
        self.db.set_setting("business_postal_address", "1 Test Street, Bengaluru 560001, India")
        self.db.set_channel("email", paused=False, reason="", credential_status="healthy")
        return mid

    def test_fake_smtp_sends_plain_text_with_unsubscribe(self):
        mid = self._scheduled()
        self.FakeSMTP.messages.clear()
        with patch("prospecting.email_delivery.in_business_window", return_value=True), \
             patch("prospecting.email_delivery.keychain_get", return_value="x" * 16), \
             patch("prospecting.email_delivery.smtplib.SMTP_SSL", self.FakeSMTP):
            result = send_due(self.db)
        self.assertEqual(result["sent"], 1)
        message = self.FakeSMTP.messages[0]
        self.assertEqual(message.get_content_type(), "text/plain")
        self.assertIn("mailto:wachas@wrrk.ai", message["List-Unsubscribe"])
        self.assertNotIn("<img", message.get_content())
        self.assertIn("1 Test Street, Bengaluru 560001, India", message.get_content())
        self.assertEqual(self.db.row("SELECT status FROM messages WHERE id=?", (mid,))["status"], "sent")

    def test_worker_dry_run_is_read_only(self):
        mid = self._scheduled()
        result = run_worker(self.db, "email", dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["email"]["would_attempt"], 1)
        row = self.db.row("SELECT status,attempt_count FROM messages WHERE id=?", (mid,))
        self.assertEqual((row["status"], row["attempt_count"]), ("scheduled", 0))

    def test_smtp_worker_never_duplicates_a_gmail_scheduled_message(self):
        mid = self._scheduled()
        self.db.record_event(
            "email", "gmail_scheduled", message_id=mid,
            external_id="gmail-thread-id", details={"account": "wachas@wrrk.ai"},
        )
        scheduled_for = self.db.row(
            "SELECT scheduled_for FROM messages WHERE id=?", (mid,)
        )["scheduled_for"]
        self.FakeSMTP.messages.clear()
        self.assertEqual(reschedule_overdue(self.db), 0)
        self.assertEqual(
            self.db.row("SELECT scheduled_for FROM messages WHERE id=?", (mid,))["scheduled_for"],
            scheduled_for,
        )
        self.assertEqual(run_worker(self.db, "email", dry_run=True)["email"]["would_attempt"], 0)
        with patch("prospecting.email_delivery.in_business_window", return_value=True), \
             patch("prospecting.email_delivery.keychain_get", return_value="x" * 16), \
             patch("prospecting.email_delivery.smtplib.SMTP_SSL", self.FakeSMTP):
            result = send_due(self.db)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["due"], 0)
        self.assertFalse(self.FakeSMTP.messages)
        self.assertEqual(
            self.db.row("SELECT status FROM messages WHERE id=?", (mid,))["status"],
            "scheduled",
        )

    def test_cancelled_gmail_schedule_returns_to_local_delivery_queue(self):
        mid = self._scheduled()
        self.db.record_event(
            "email", "gmail_scheduled", message_id=mid,
            external_id="old-gmail-id", details={"account": "wachas@wrrk.ai"},
        )
        self.db.record_event(
            "email", "gmail_schedule_cancelled", message_id=mid,
            external_id="old-gmail-id", details={"account": "wachas@wrrk.ai"},
        )
        self.assertEqual(
            run_worker(self.db, "email", dry_run=True)["email"]["would_attempt"], 1
        )
        self.FakeSMTP.messages.clear()
        with patch("prospecting.email_delivery.in_business_window", return_value=True), \
             patch("prospecting.email_delivery.keychain_get", return_value="x" * 16), \
             patch("prospecting.email_delivery.smtplib.SMTP_SSL", self.FakeSMTP):
            result = send_due(self.db)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(self.FakeSMTP.messages), 1)

    def test_cancelled_gmail_schedule_can_be_rescheduled_after_missed_window(self):
        mid = self._scheduled()
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE messages SET scheduled_for=? WHERE id=?",
                (iso(utcnow() - timedelta(minutes=20)), mid),
            )
        self.db.record_event(
            "email", "gmail_scheduled", message_id=mid,
            external_id="old-gmail-id", details={"account": "wachas@wrrk.ai"},
        )
        self.db.record_event(
            "email", "gmail_schedule_cancelled", message_id=mid,
            external_id="old-gmail-id", details={"account": "wachas@wrrk.ai"},
        )
        before = parse_iso(self.db.row(
            "SELECT scheduled_for FROM messages WHERE id=?", (mid,)
        )["scheduled_for"])
        self.assertEqual(reschedule_overdue(self.db), 1)
        after = parse_iso(self.db.row(
            "SELECT scheduled_for FROM messages WHERE id=?", (mid,)
        )["scheduled_for"])
        self.assertGreater(after, before)

    def test_multiple_due_emails_do_not_send_in_a_burst(self):
        first, second = self._scheduled(), self._scheduled()
        with self.db.transaction(immediate=True) as conn:
            conn.execute("UPDATE messages SET scheduled_for=? WHERE id IN (?,?)",
                         (iso(utcnow() - timedelta(minutes=1)), first, second))
        self.FakeSMTP.messages.clear()
        with patch("prospecting.email_delivery.in_business_window", return_value=True), \
             patch("prospecting.email_delivery.keychain_get", return_value="x" * 16), \
             patch("prospecting.email_delivery.smtplib.SMTP_SSL", self.FakeSMTP):
            result = send_due(self.db)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(self.FakeSMTP.messages), 1)
        states = self.db.rows("SELECT status,scheduled_for FROM messages WHERE id IN (?,?)", (first, second))
        self.assertEqual(sorted(row["status"] for row in states), ["scheduled", "sent"])
        remaining = next(row for row in states if row["status"] == "scheduled")
        self.assertGreater(parse_iso(remaining["scheduled_for"]), utcnow())

    def test_no_smtp_action_without_approved_hash(self):
        mid = self.pending_message()
        with self.db.transaction(immediate=True) as conn:
            conn.execute("UPDATE messages SET status='scheduled',scheduled_for=? WHERE id=?",
                         (iso(utcnow() - timedelta(minutes=1)), mid))
        settings = self.db.settings()
        settings["market_policies"]["IN"]["timezone"] = "UTC"
        self.db.set_setting("market_policies", settings["market_policies"])
        self.db.set_setting("business_hour_start", 0)
        self.db.set_setting("business_hour_end", 24)
        self.db.set_setting("business_postal_address", "1 Test Street, Bengaluru 560001, India")
        self.db.set_channel("email", paused=False, reason="", credential_status="healthy")
        self.FakeSMTP.messages.clear()
        with patch("prospecting.email_delivery.keychain_get", return_value="x" * 16), \
             patch("prospecting.email_delivery.smtplib.SMTP_SSL", self.FakeSMTP):
            result = send_due(self.db)
        self.assertEqual(result["sent"], 0)
        self.assertFalse(self.FakeSMTP.messages)
        self.assertEqual(self.db.row("SELECT status FROM messages WHERE id=?", (mid,))["status"], "failed")

    def test_footer_change_after_approval_invalidates_delivery_hash(self):
        mid = self._scheduled()
        self.db.set_setting("business_postal_address", "2 Changed Street, Bengaluru 560002, India")
        self.FakeSMTP.messages.clear()
        with patch("prospecting.email_delivery.keychain_get", return_value="x" * 16), \
             patch("prospecting.email_delivery.smtplib.SMTP_SSL", self.FakeSMTP):
            result = send_due(self.db)
        self.assertEqual(result["sent"], 0)
        self.assertFalse(self.FakeSMTP.messages)
        self.assertEqual(self.db.row("SELECT status FROM messages WHERE id=?", (mid,))["status"], "failed")

    def test_prior_unfinished_smtp_attempt_pauses_without_retry(self):
        mid = self._scheduled()
        self.db.reserve_message_attempt(mid)
        self.FakeSMTP.messages.clear()
        with patch("prospecting.email_delivery.keychain_get", return_value="x" * 16), \
             patch("prospecting.email_delivery.smtplib.SMTP_SSL", self.FakeSMTP):
            result = send_due(self.db)
        self.assertEqual(result["sent"], 0)
        self.assertFalse(self.FakeSMTP.messages)
        self.assertTrue(self.db.channel("email")["paused"])
        self.assertEqual(self.db.row("SELECT status FROM messages WHERE id=?", (mid,))["status"], "failed")

    def test_connector_sends_count_toward_rolling_bounce_pause(self):
        first, second = self._scheduled(), self._scheduled()
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE messages SET status='sent',sent_at=?,attempt_count=0 WHERE id=?",
                (iso(utcnow() - timedelta(minutes=2)), first),
            )
            conn.execute(
                "UPDATE messages SET status='sent',sent_at=?,attempt_count=0 WHERE id=?",
                (iso(utcnow() - timedelta(minutes=1)), second),
            )
        _record_bounce_and_maybe_pause(self.db, first, "connector hard bounce")
        self.assertFalse(self.db.channel("email")["paused"])
        _record_bounce_and_maybe_pause(self.db, second, "connector hard bounce")
        self.assertTrue(self.db.channel("email")["paused"])
        self.assertIn("two bounces", self.db.channel("email")["reason"])

    def test_fake_imap_optout_suppresses_email_and_domain(self):
        mid = self._scheduled()
        with self.db.transaction(immediate=True) as conn:
            conn.execute("UPDATE messages SET status='sent',sent_at=?,external_id=? WHERE id=?",
                         (iso(), "<original@wrrk.ai>", mid))
        inbound = EmailMessage()
        recipient = self.db.row("SELECT to_address FROM messages WHERE id=?", (mid,))["to_address"]
        inbound["From"] = recipient
        inbound["To"] = "wachas@wrrk.ai"
        inbound["Subject"] = "Re: Keeping customer enquiries together"
        inbound["In-Reply-To"] = "<original@wrrk.ai>"
        inbound["Message-ID"] = "<reply@example.com>"
        inbound.set_content("Please opt out and do not contact me again.")

        class FakeIMAP:
            def __init__(self, *args, **kwargs): pass
            def login(self, *args): return "OK", []
            def select(self, *args, **kwargs): return "OK", []
            def uid(self, command, *args):
                if command == "search": return "OK", [b"1"]
                return "OK", [(b"1 (RFC822)", bytes(inbound))]
            def logout(self): return "BYE", []

        with patch("prospecting.email_delivery.keychain_get", return_value="x" * 16), \
             patch("prospecting.email_delivery.imaplib.IMAP4_SSL", FakeIMAP):
            result = poll_inbox(self.db)
        self.assertEqual(result["optouts"], 1)
        self.assertTrue(self.db.is_suppressed("email", recipient))
        domain = self.db.row("SELECT p.registrable_domain FROM messages m JOIN prospects p ON p.id=m.prospect_id WHERE m.id=?", (mid,))["registrable_domain"]
        self.assertTrue(self.db.is_suppressed("domain", domain))
        self.assertEqual(
            self.db.row(
                "SELECT COUNT(*) AS n FROM delivery_events WHERE message_id=? AND event_type='opt_out'",
                (mid,),
            )["n"],
            1,
        )

    def test_linkedin_worker_never_posts_and_manual_confirmation_is_audited(self):
        campaign = self.db.ensure_campaign("fresh")
        post_text = "Support ownership matters more than adding another dashboard to a growing team."
        with self.db.transaction(immediate=True) as conn:
            post_id = conn.execute(
                "INSERT INTO posts(author_name,author_url,post_url,text,text_hash,published_at,market,role,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("Test Author", "https://www.linkedin.com/in/test-author",
                 "https://www.linkedin.com/posts/test-author_update-1", post_text,
                 __import__("prospecting.util", fromlist=["text_hash"]).text_hash(post_text),
                 iso(), "IN", "influencer", "discovered", iso()),
            ).lastrowid
        comment = "Support ownership is the sharp point here. A clear owner often matters more than another dashboard."
        mid = self.db.create_message(
            campaign, "linkedin", "comment", comment, post_id=post_id,
            to_address="https://www.linkedin.com/in/test-author", evidence_ids=[-post_id],
        )
        self.db.mark_pending(mid)
        self.db.approve_message(mid)
        self.db.release_message(mid, iso(utcnow() - timedelta(minutes=1)))
        settings = self.db.settings()
        settings["market_policies"]["IN"]["timezone"] = "UTC"
        self.db.set_setting("market_policies", settings["market_policies"])
        self.db.set_setting("business_hour_start", 0)
        self.db.set_setting("business_hour_end", 24)
        self.db.set_setting("linkedin_posting_mode", "manual")
        self.db.set_channel("linkedin", paused=False, reason="manual", credential_status="manual")

        with patch("prospecting.linkedin_delivery.webbrowser.open") as browser:
            result = post_due(self.db)
        self.assertEqual(result["posted"], 0)
        self.assertEqual(result["manual_action_required"], 1)
        browser.assert_not_called()
        self.assertEqual(self.db.row("SELECT status FROM messages WHERE id=?", (mid,))["status"], "scheduled")
        self.assertEqual(self.db.row("SELECT attempt_count FROM messages WHERE id=?", (mid,))[0], 0)

        external_id = confirm_manual_post(self.db, mid)
        self.assertIn("#manual-", external_id)
        self.assertEqual(self.db.row("SELECT status FROM messages WHERE id=?", (mid,))["status"], "posted")
        self.assertEqual(self.db.row("SELECT attempt_count FROM messages WHERE id=?", (mid,))[0], 0)
        event = self.db.row("SELECT * FROM delivery_events WHERE message_id=?", (mid,))
        self.assertEqual(event["event_type"], "manually_posted")

    def test_linkedin_setup_only_opens_the_normal_browser(self):
        with patch("prospecting.linkedin_delivery.webbrowser.open", return_value=True) as browser:
            result = setup_linkedin(self.db)
        browser.assert_called_once_with("https://www.linkedin.com/feed/", new=2)
        self.assertEqual(result["mode"], "manual")
        self.assertEqual(self.db.channel("linkedin")["credential_status"], "manual")
        self.assertFalse(self.db.setting("linkedin_identity_url", ""))

    def _scheduled_official_api_comment(self) -> tuple[int, str]:
        post_url = (
            "https://www.linkedin.com/posts/manual-author_"
            "support-ownership-activity-7401234567890123456-abcd"
        )
        post_id = add_manual_post(
            self.db,
            post_url=post_url,
            author_url="https://www.linkedin.com/in/manual-author",
            author_name="Manual Author",
            post_text=(
                "Clear support ownership helps a growing service team respond consistently "
                "without adding another dashboard."
            ),
            role="influencer", market="IN", published_at=iso(),
        )
        comment = (
            "Clear support ownership is the useful point here. Consistency often improves "
            "before another dashboard is needed."
        )
        campaign = self.db.ensure_campaign("fresh")
        mid = self.db.create_message(
            campaign, "linkedin", "comment", comment, post_id=post_id,
            to_address="https://www.linkedin.com/in/manual-author",
            evidence_ids=[-post_id],
        )
        self.db.mark_pending(mid)
        self.db.approve_message(mid)
        self.db.release_message(mid, iso(utcnow() - timedelta(minutes=1)))
        policies = self.db.setting("market_policies")
        policies["IN"]["timezone"] = "UTC"
        self.db.set_setting("market_policies", policies)
        self.db.set_setting("business_hour_start", 0)
        self.db.set_setting("business_hour_end", 24)
        self.db.set_setting("linkedin_posting_mode", "official_api")
        self.db.set_setting("linkedin_api_client_id", "test-client")
        self.db.set_setting("linkedin_api_actor_urn", "urn:li:person:123456")
        self.db.set_setting("linkedin_api_authorized_member_urn", "urn:li:person:123456")
        self.db.set_setting("linkedin_api_scope", "w_member_social_feed")
        self.db.set_setting("linkedin_api_version", "202607")
        self.db.set_setting(
            "linkedin_api_token_expires_at", iso(utcnow() + timedelta(hours=1)),
        )
        self.db.set_channel("linkedin", paused=False, reason="", credential_status="healthy")
        return mid, comment

    def test_official_comments_api_posts_exact_approved_comment(self):
        mid, comment = self._scheduled_official_api_comment()
        external_id = "urn:li:comment:(urn:li:activity:7401234567890123456,987654)"

        def fake_keychain(service, account):
            self.assertEqual(account, "test-client")
            return "access-token" if service == LINKEDIN_ACCESS_TOKEN_SERVICE else ""

        with patch("prospecting.linkedin_api.in_business_window", return_value=True), \
             patch("prospecting.linkedin_api.keychain_get", side_effect=fake_keychain), \
             patch("prospecting.linkedin_api._create_comment", return_value=external_id) as create:
            result = post_due(self.db)

        self.assertEqual(result["posted"], 1)
        create.assert_called_once_with(
            "access-token", "202607", "urn:li:person:123456",
            "urn:li:activity:7401234567890123456", comment,
        )
        row = self.db.row(
            "SELECT status,attempt_count,external_id FROM messages WHERE id=?", (mid,),
        )
        self.assertEqual((row["status"], row["attempt_count"], row["external_id"]),
                         ("posted", 1, external_id))
        event = self.db.row("SELECT * FROM delivery_events WHERE message_id=?", (mid,))
        self.assertEqual(event["event_type"], "official_api_posted")

    def test_official_api_missing_token_pauses_without_browser_fallback(self):
        mid, _ = self._scheduled_official_api_comment()
        with patch("prospecting.linkedin_api.keychain_get", return_value=""), \
             patch("prospecting.linkedin_api._create_comment") as create, \
             patch("prospecting.linkedin_delivery.webbrowser.open") as browser:
            result = post_due(self.db)
        self.assertEqual(result["posted"], 0)
        self.assertIn("token", result["blocked"].lower())
        self.assertTrue(self.db.channel("linkedin")["paused"])
        row = self.db.row("SELECT status,attempt_count FROM messages WHERE id=?", (mid,))
        self.assertEqual((row["status"], row["attempt_count"]), ("scheduled", 0))
        create.assert_not_called()
        browser.assert_not_called()

    def test_official_api_quota_error_is_counted_once_and_pauses(self):
        mid, _ = self._scheduled_official_api_comment()
        with patch("prospecting.linkedin_api.in_business_window", return_value=True), \
             patch("prospecting.linkedin_api.keychain_get", return_value="access-token"), \
             patch(
                 "prospecting.linkedin_api._create_comment",
                 side_effect=LinkedInHTTPError("LinkedIn Comments API HTTP 429", status=429),
             ):
            result = post_due(self.db)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(self.db.channel("linkedin")["paused"])
        self.assertEqual(self.db.channel("linkedin")["credential_status"], "quota")
        row = self.db.row("SELECT status,attempt_count FROM messages WHERE id=?", (mid,))
        self.assertEqual((row["status"], row["attempt_count"]), ("failed", 1))

    def test_linkedin_official_oauth_setup_stores_only_keychain_secrets(self):
        payload = {
            "access_token": "access-token", "expires_in": 3600,
            "scope": "r_basicprofile w_member_social_feed",
        }
        with patch("prospecting.linkedin_api.keychain_get", return_value="client-secret"), \
             patch("prospecting.linkedin_api._receive_authorization_code", return_value="code"), \
             patch("prospecting.linkedin_api._token_request", return_value=payload), \
             patch(
                 "prospecting.linkedin_api._current_member",
                 return_value=("urn:li:person:123456", "Test Member"),
             ), \
             patch("prospecting.linkedin_api.keychain_set") as store:
            result = setup_linkedin_api(
                self.db, client_id="test-client", actor_urn="urn:li:person:123456",
            )
        self.assertEqual(result["mode"], "official_api")
        self.assertFalse(self.db.channel("linkedin")["paused"])
        store.assert_called_once_with(
            "access-token", LINKEDIN_ACCESS_TOKEN_SERVICE, "test-client",
        )
        stored_settings = json.dumps(self.db.settings())
        self.assertNotIn("access-token", stored_settings)
        self.assertNotIn("client-secret", stored_settings)

    def test_linkedin_permalink_target_urn_parsing(self):
        self.assertEqual(
            target_urn_from_url(
                "https://www.linkedin.com/feed/update/urn%3Ali%3AugcPost%3A7401234567890123456/"
            ),
            "urn:li:ugcPost:7401234567890123456",
        )

    def test_comments_api_request_and_response_are_identity_bound(self):
        actor = "urn:li:person:123456"
        target = "urn:li:activity:7401234567890123456"
        comment = "Clear support ownership makes this observation especially useful."

        class Response:
            status = 201
            headers = {"x-restli-id": "987654"}

            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self):
                return json.dumps({
                    "actor": actor, "object": target, "message": {"text": comment},
                    "commentUrn": f"urn:li:comment:({target},987654)",
                }).encode()

        with patch("prospecting.linkedin_api.urllib.request.urlopen", return_value=Response()) as opened:
            external_id = _create_comment("token", "202607", actor, target, comment)
        request = opened.call_args.args[0]
        self.assertEqual(json.loads(request.data), {
            "actor": actor, "object": target, "message": {"text": comment},
        })
        self.assertEqual(request.headers["Linkedin-version"], "202607")
        self.assertEqual(external_id, f"urn:li:comment:({target},987654)")

    def test_only_user_supplied_linkedin_posts_are_eligible_in_safe_mode(self):
        campaign = self.db.ensure_campaign("fresh")
        with self.db.transaction(immediate=True) as conn:
            automatic_id = conn.execute(
                "INSERT INTO posts(author_name,author_url,post_url,text,text_hash,published_at,"
                "market,role,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("Indexed Author", "https://www.linkedin.com/in/indexed-author",
                 "https://www.linkedin.com/posts/indexed-author_automatic-1",
                 "This indexed post is long enough to look usable but was not supplied by the user.",
                 "automatic-hash", iso(), "IN", "influencer", "discovered", iso()),
            ).lastrowid
        manual_id = add_manual_post(
            self.db,
            post_url="https://www.linkedin.com/posts/manual-author_manual-1",
            author_url="https://www.linkedin.com/in/manual-author",
            author_name="Manual Author",
            post_text="A specific, user-verified post about making customer support ownership clearer.",
            role="influencer", market="IN", published_at=iso(),
        )
        requests = comment_requests(self.db, 5)
        self.assertIn(manual_id, [item.post_id for item in requests])
        self.assertNotIn(automatic_id, [item.post_id for item in requests])
        source = self.db.row(
            "SELECT sr.source FROM posts p JOIN source_runs sr ON sr.id=p.source_run_id WHERE p.id=?",
            (manual_id,),
        )
        self.assertEqual(source["source"], "manual_linkedin")


class DashboardTests(TempDatabaseTest):
    def test_dashboard_is_loopback_and_csrf_gated(self):
        app = create_app(self.db)
        app.testing = True
        client = app.test_client()
        self.assertEqual(client.get("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code, 200)
        self.assertEqual(client.get("/", environ_base={"REMOTE_ADDR": "10.0.0.2"}).status_code, 403)
        self.assertEqual(client.post("/emergency/stop", environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code, 403)

    def test_dashboard_explains_email_outcomes_without_open_tracking(self):
        message_id = self.pending_message()
        self.db.record_event("email", "sent", message_id=message_id)
        self.db.record_event("email", "bounce", message_id=message_id)
        self.db.record_event("email", "human_reply", message_id=message_id)
        self.db.record_event("email", "opt_out", message_id=message_id)
        app = create_app(self.db)
        app.testing = True
        response = app.test_client().get(
            "/", environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertIn(b"Gmail submitted 1", response.data)
        self.assertIn(b"Bounced 1", response.data)
        self.assertIn(b"Human replies 1", response.data)
        self.assertIn(b"Opt-outs 1", response.data)
        self.assertIn(b"Opens are not tracked", response.data)
        self.assertIn(b"No bounce observed 0", response.data)

    def test_linkedin_worker_never_opens_browser(self):
        with patch("prospecting.linkedin_delivery.webbrowser.open") as browser:
            result = post_due(self.db)
        self.assertEqual(result["posted"], 0)
        browser.assert_not_called()

    def test_manual_dashboard_has_user_triggered_open_and_copy_handoff_only(self):
        post_id = add_manual_post(
            self.db,
            post_url="https://www.linkedin.com/posts/manual-author_manual-handoff-1",
            author_url="https://www.linkedin.com/in/manual-author",
            author_name="Manual Author",
            post_text=(
                "Clear support ownership helps a growing service team respond consistently "
                "without adding another dashboard."
            ),
            role="influencer", market="IN", published_at=iso(),
        )
        comment = (
            "Clear support ownership is the useful point here. Consistency often improves "
            "before another dashboard is needed."
        )
        campaign = self.db.ensure_campaign("fresh")
        message_id = self.db.create_message(
            campaign, "linkedin", "comment", comment, post_id=post_id,
            to_address="https://www.linkedin.com/in/manual-author",
            evidence_ids=[-post_id],
        )
        self.db.mark_pending(message_id)
        self.db.approve_message(message_id)
        self.db.release_message(message_id, iso())
        self.db.set_setting("linkedin_posting_mode", "manual")

        app = create_app(self.db)
        app.testing = True
        client = app.test_client()
        response = client.get("/", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertIn(b"Open post + copy approved comment", response.data)
        self.assertIn(b"navigator.clipboard.writeText", response.data)
        self.assertNotIn(b"playwright", response.data.lower())
        self.assertNotIn(b"selenium", response.data.lower())

        self.db.set_setting("linkedin_posting_mode", "official_api")
        response = client.get("/", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertNotIn(b"Open post + copy approved comment", response.data)

    def test_dashboard_labels_official_api_without_manual_confirmation(self):
        self.db.set_setting("linkedin_posting_mode", "official_api")
        app = create_app(self.db)
        app.testing = True
        response = app.test_client().get(
            "/", environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertIn(b"Official Comments API", response.data)
        self.assertNotIn(b"I posted this exact comment", response.data)


if __name__ == "__main__":
    unittest.main()
