from __future__ import annotations

import csv
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prospecting.db import Database
from prospecting.discovery import resolve_mcporter_binary, resolve_mcporter_config
from prospecting.phone_prospecting import (
    CSV_COLUMNS,
    _action_and_suppression_state,
    _audit_candidates_supervised,
    _audit_candidate,
    _formula_safe,
    apply_manual_browser_review,
    broker_phone_is_corroborated,
    canonical_snapshot,
    discover_uae_candidates,
    open_canonical_readonly,
    quota_rank,
    write_private_csv,
)
from prospecting.phones import extract_phone_contacts, extract_uae_locations, normalize_phone
from sources.stack_detect import detect


VALID_MOBILE = "+971 58 869 6981"
VALID_E164 = "+971588696981"


def phone_contact(**overrides):
    value = normalize_phone(VALID_MOBILE, "AE")
    assert value
    value.update({
        "contact_type": "whatsapp", "is_whatsapp": True, "business_use": True,
        "source_type": "company_website", "source_url": "https://acme.ae/contact",
        "source_urls": ["https://acme.ae/contact"],
        "evidence_excerpt": "WhatsApp our Dubai sales team on +971 58 869 6981",
        "evidence_excerpts": ["WhatsApp our Dubai sales team on +971 58 869 6981"],
        "observed_at": "2026-08-28T08:00:00+00:00", "confidence": "high",
        "source_count": 1,
    })
    value.update(overrides)
    return value


def audit_record(confidence="high"):
    return {
        "domain": "acme.ae", "reachable": True, "resolved": "https://acme.ae",
        "confidence": confidence, "phone_contacts": [phone_contact()],
        "locations": [{
            "city": "Dubai", "emirate": "Dubai", "source_url": "https://acme.ae/contact",
            "evidence_excerpt": "Dubai, United Arab Emirates",
            "observed_at": "2026-08-28T08:00:00+00:00",
        }],
        "site_summary": {"title": "Acme Property Services Dubai", "description": "Property enquiries"},
        "channels": {
            "whatsapp": True, "phone": True, "emails": ["hello@acme.ae"],
            "instagram": ["acme"], "facebook": [], "linkedin": ["acme-ae"],
            "contact_form": True, "live_chat": False,
        },
        "channel_count": 5, "runs_ads": True,
        "tools": [{"name": "Meta Pixel", "category": "ads"}],
    }


class PhoneNormalizationTests(unittest.TestCase):
    def test_uae_local_e164_landline_and_extension(self):
        self.assertEqual(VALID_E164, normalize_phone("058 869 6981", "AE")["e164"])
        self.assertEqual("+97142935333", normalize_phone("04 293 5333", "AE")["e164"])
        extended = normalize_phone("tel:+971588696981;ext=12", "AE")
        self.assertEqual((VALID_E164, "12"), (extended["e164"], extended["extension"]))

    def test_rejects_placeholders_and_non_uae_numbers(self):
        for value in ("+971 50 000 0000", "050 123 4567", "1234567", "+1 212 555 0188"):
            self.assertIsNone(normalize_phone(value, "AE"), value)

    def test_whatsapp_jsonld_and_labelled_visible_numbers_are_deduplicated(self):
        markup = f"""
        <script type="application/ld+json">{{"@type":"Organization","telephone":"{VALID_MOBILE}"}}</script>
        <a href="https://wa.me/971588696981">WhatsApp</a>
        <p>Sales: {VALID_MOBILE}</p><p>Dubai, UAE</p>
        """
        contacts = extract_phone_contacts([("https://acme.ae/contact", markup)], "AE")
        self.assertEqual(1, len(contacts))
        self.assertEqual((VALID_E164, True, "whatsapp"), (
            contacts[0]["e164"], contacts[0]["is_whatsapp"], contacts[0]["contact_type"],
        ))
        self.assertIn("Sales", " ".join(contacts[0]["evidence_excerpts"]))
        self.assertIn("971588696981", "".join(ch for ch in contacts[0]["evidence_excerpt"] if ch.isdigit()))
        self.assertEqual("Dubai", extract_uae_locations([("https://acme.ae", markup)])[0]["city"])

    def test_stack_detector_emits_structured_phone_evidence(self):
        markup = f'<title>Acme Dubai Clinic</title><a href="https://wa.me/971588696981">WhatsApp</a><p>Dubai</p>'
        with patch("sources.stack_detect._gather_html", return_value=(markup, "https://acme.ae", [("https://acme.ae", markup)])):
            result = detect("acme.ae", region="AE")
        self.assertEqual(VALID_E164, result["phone_contacts"][0]["e164"])
        self.assertTrue(any(item["kind"] == "business_phone" for item in result["evidence"]))
        self.assertEqual("Dubai", result["locations"][0]["city"])


class ProvenanceAndScoringTests(unittest.TestCase):
    def candidate(self):
        return {
            "company": "Acme Property Services", "domain": "acme.ae",
            "website": "https://acme.ae", "vertical_hint": "real estate",
            "city_hint": "Dubai", "source_url": "https://acme.ae",
            "source_excerpt": "Dubai property services", "source_type": "exa",
            "origin": "fresh_discovery", "metadata": {}, "stored_fit": 0,
            "stored_confidence": "none", "linkedin_url": "",
        }

    def state(self):
        return {"suppressed_domains": set(), "suppressed_emails": set(),
                "actioned_domains": set(), "customer_domains": set()}

    def test_first_party_phone_qualifies_with_source_provenance(self):
        with patch("prospecting.phone_prospecting.detect", return_value=audit_record()):
            accepted, rejected = _audit_candidate(self.candidate(), self.state(), {}, {})
        self.assertIsNone(rejected)
        self.assertGreaterEqual(accepted["fit_score"], 75)
        self.assertEqual("https://acme.ae/contact", accepted["phone_source_url"])
        self.assertEqual("A", accepted["tier"])

    def test_low_confidence_fails_closed_without_browser_verification(self):
        with patch("prospecting.phone_prospecting.detect", return_value=audit_record("low")):
            accepted, rejected = _audit_candidate(self.candidate(), self.state(), {}, {})
        self.assertIsNone(accepted)
        self.assertIn("browser verification", rejected["rejection_reasons"])

    def test_competing_whatsapp_platform_and_giant_hospital_are_rejected(self):
        vendor = audit_record()
        vendor["site_summary"]["title"] = "WhatsApp automation platform for ecommerce"
        with patch("prospecting.phone_prospecting.detect", return_value=vendor):
            accepted, rejected = _audit_candidate(self.candidate(), self.state(), {}, {})
        self.assertIsNone(accepted)
        self.assertIn("competing", rejected["rejection_reasons"])

        hospital = audit_record()
        hospital["site_summary"]["title"] = "American Hospital Dubai"
        with patch("prospecting.phone_prospecting.detect", return_value=hospital):
            accepted, rejected = _audit_candidate(self.candidate(), self.state(), {}, {})
        self.assertIsNone(accepted)
        self.assertIn("giant enterprise", rejected["rejection_reasons"])

    def test_apollo_phone_requires_exact_trusted_corroboration(self):
        apollo = {**normalize_phone(VALID_MOBILE, "AE"), "source_type": "apollo"}
        self.assertTrue(broker_phone_is_corroborated(apollo, [phone_contact()]))
        other = {**normalize_phone("+971 4 293 5333", "AE"), "source_type": "company_website"}
        self.assertFalse(broker_phone_is_corroborated(apollo, [other]))

    def test_soft_quotas_never_include_unqualified_rows(self):
        rows = []
        for index, (vertical, city, score) in enumerate([
            ("healthcare", "Abu Dhabi", 90), ("real_estate", "Dubai", 88),
            ("finance", "Dubai", 87), ("other", "Elsewhere", 86),
        ]):
            rows.append({"domain": f"{index}.ae", "vertical_key": vertical, "city": city,
                         "fit_score": score, "tier": "A", "audit_confidence": "high"})
        selected, overflow = quota_rank(rows, 3)
        self.assertEqual(3, len(selected))
        self.assertEqual([1, 2, 3], [row["priority_rank"] for row in selected])
        self.assertEqual(1, len(overflow))


class ReadOnlyAndExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "canonical.sqlite3"
        Database(self.db_path).initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_canonical_connection_is_query_only_and_checksum_stays_identical(self):
        before = canonical_snapshot(self.db_path)
        with open_canonical_readonly(self.db_path) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO settings(key,value_json,updated_at) VALUES ('x','1','x')")
        self.assertEqual(before, canonical_snapshot(self.db_path))

    def test_suppression_and_action_history_are_loaded(self):
        db = Database(self.db_path)
        campaign = db.ensure_campaign("test")
        now = "2026-08-28T00:00:00+00:00"
        with db.transaction(immediate=True) as conn:
            pid = conn.execute(
                "INSERT INTO prospects(campaign_id,company,domain,registrable_domain,market,status,"
                "discovered_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (campaign, "Actioned", "actioned.ae", "actioned.ae", "AE", "audited", now, now),
            ).lastrowid
            conn.execute(
                "INSERT INTO messages(campaign_id,prospect_id,channel,kind,to_address,body,status,"
                "content_hash,scheduled_for,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (campaign, pid, "email", "initial", "hello@actioned.ae", "body", "scheduled",
                 "hash", now, now, now),
            )
        db.suppress("domain", "blocked.ae", "test")
        with open_canonical_readonly(self.db_path) as conn:
            state = _action_and_suppression_state(conn)
        self.assertIn("actioned.ae", state["actioned_domains"])
        self.assertIn("blocked.ae", state["suppressed_domains"])

    def test_csv_formula_safety_utf8_and_permissions(self):
        output = self.root / "leads.csv"
        row = {column: "" for column in CSV_COLUMNS}
        row.update({"company": "=HYPERLINK(\"bad\")", "normalized_e164": VALID_E164,
                    "fit_score": 88, "domain": "acme.ae"})
        write_private_csv(output, [row], CSV_COLUMNS)
        self.assertEqual(0o600, stat.S_IMODE(output.stat().st_mode))
        with output.open("r", encoding="utf-8-sig", newline="") as handle:
            loaded = next(csv.DictReader(handle))
        self.assertTrue(loaded["company"].startswith("'="))
        self.assertEqual("'" + VALID_E164, loaded["normalized_e164"])
        self.assertEqual("' @formula", _formula_safe(" @formula"))

    def test_manual_review_excludes_uncertain_rows_and_requires_confirmed_top_20(self):
        snapshot = canonical_snapshot(self.db_path)
        rows = []
        for index in range(21):
            row = {column: "" for column in CSV_COLUMNS}
            row.update({
                "priority_rank": index + 1, "company": f"Company {index}",
                "domain": f"company{index}.ae", "fit_score": 85,
            })
            rows.append(row)
        stage_path = self.root / "stage.json"
        stage_path.write_text(json.dumps({
            "canonical_db": str(self.db_path),
            "canonical_after": snapshot.__dict__,
            "canonical_db_unchanged": True,
            "rows": rows,
            "rejected_rows": [],
        }), encoding="utf-8")
        outcomes = {
            f"company{index}.ae": {
                "status": "excluded" if index == 0 else "confirmed",
                "reason": "page could not be verified" if index == 0 else "",
            }
            for index in range(21)
        }
        result = apply_manual_browser_review(stage_path, outcomes)
        reviewed = json.loads(Path(result["stage_path"]).read_text(encoding="utf-8"))
        self.assertEqual(20, reviewed["qualified"])
        self.assertEqual("company1.ae", reviewed["rows"][0]["domain"])
        self.assertEqual(1, reviewed["rows"][0]["priority_rank"])
        self.assertIn("manual browser review", reviewed["rejected_rows"][0]["rejection_reasons"])
        self.assertEqual(0o600, stat.S_IMODE(Path(result["stage_path"]).stat().st_mode))


class SourceFailureTests(unittest.TestCase):
    def test_exa_timeout_is_recorded_and_run_continues(self):
        one_query = (("Dubai", "real_estate", "test query"),)
        with patch("prospecting.phone_prospecting.DISCOVERY_QUERIES", one_query), patch(
            "prospecting.phone_prospecting.exa_search", side_effect=TimeoutError("timeout")
        ):
            candidates, errors = discover_uae_candidates(10)
        self.assertEqual([], candidates)
        self.assertIn("timeout", errors[0])

    def test_mcporter_environment_override_and_nvm_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "mcporter"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o700)
            with patch.dict(os.environ, {"WRRKHUNT_MCPORTER_BIN": str(binary)}, clear=False):
                self.assertEqual(str(binary.resolve()), resolve_mcporter_binary())
            nvm_binary = Path(temp) / "versions" / "node" / "v99" / "bin" / "mcporter"
            nvm_binary.parent.mkdir(parents=True)
            nvm_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            nvm_binary.chmod(0o700)
            with patch.dict(os.environ, {"WRRKHUNT_MCPORTER_BIN": "", "NVM_DIR": temp}, clear=False), patch(
                "prospecting.discovery.shutil.which", return_value=None
            ):
                self.assertEqual(str(nvm_binary.resolve()), resolve_mcporter_binary())

    def test_mcporter_config_is_self_contained_and_supports_override(self):
        with patch.dict(os.environ, {"WRRKHUNT_MCPORTER_CONFIG": ""}, clear=False):
            self.assertEqual(
                Path(__file__).resolve().parents[1] / "config" / "mcporter.json",
                resolve_mcporter_config(),
            )
        with tempfile.TemporaryDirectory() as temp:
            configured = Path(temp) / "custom.json"
            configured.write_text('{"mcpServers": {}}', encoding="utf-8")
            with patch.dict(
                os.environ, {"WRRKHUNT_MCPORTER_CONFIG": str(configured)}, clear=False
            ):
                self.assertEqual(configured.resolve(), resolve_mcporter_config())

    def test_supervisor_kills_overdue_candidate_process(self):
        candidate = {
            "company": "Timeout Test", "domain": "example.com", "website": "https://example.com",
            "vertical_hint": "", "city_hint": "Dubai", "source_url": "https://example.com",
            "source_excerpt": "", "source_type": "test", "origin": "fresh_discovery",
            "metadata": {}, "stored_fit": 0, "stored_confidence": "none", "linkedin_url": "",
        }
        state = {"suppressed_domains": set(), "suppressed_emails": set(),
                 "actioned_domains": set(), "customer_domains": set()}
        with tempfile.TemporaryDirectory() as temp:
            accepted, rejected = _audit_candidates_supervised(
                [candidate], state, {}, {}, Path(temp), lambda _: None, timeout_seconds=0,
            )
        self.assertEqual([], accepted)
        self.assertIn("wall-clock limit", rejected[0]["rejection_reasons"])


if __name__ == "__main__":
    unittest.main()
