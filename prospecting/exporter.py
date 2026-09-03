"""Privacy-conscious CSV exports from the canonical prospecting database."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .config import APP_HOME
from .db import Database
from .policy import choose_contact, initial_outreach_gate
from .util import iso, is_freemail


EMAIL_EXPORT_COLUMNS = [
    "send_priority",
    "recommended_for_outreach",
    "recommended_action",
    "company",
    "email",
    "contact_kind",
    "market",
    "pool",
    "fit_score",
    "audit_confidence",
    "campaign",
    "campaign_status",
    "prospect_status",
    "queue_status",
    "draft_subject",
    "website",
    "company_domain",
    "company_linkedin",
    "published_contact_url",
    "publication_evidence",
    "published_by_business",
    "mx_available",
    "freemail",
    "eligibility_notes",
    "prospect_discovered_at",
    "prospect_audited_at",
    "contact_recorded_at",
    "exported_at",
]


def default_email_export_path() -> Path:
    return APP_HOME / "exports" / f"wrrkhunt_email_leads_{iso()[:10]}.csv"


def _csv_safe(value: Any) -> Any:
    """Prevent spreadsheet formula execution while preserving readable CSV values."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if isinstance(value, str):
        value = value.replace("\x00", "").strip()
        if value.startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


def _message_action(status: str) -> str:
    return {
        "": "DRAFT_EMAIL",
        "drafted": "REVIEW_DRAFT",
        "pending_approval": "APPROVE_DRAFT",
        "approved": "RELEASE_APPROVED_DRAFT",
        "scheduled": "ALREADY_SCHEDULED",
        "sent": "ALREADY_SENT",
        "replied": "REPLIED_NO_OUTREACH",
        "blocked": "BLOCKED",
        "rejected": "REJECTED",
        "suppressed": "SUPPRESSED",
        "failed": "FAILED",
    }.get(status, "REVIEW")


def export_email_contacts(db: Database, output: Path | None = None,
                          *, recommended_only: bool = False) -> dict[str, Any]:
    """Export all found addresses with deterministic outreach recommendations."""
    db.initialize()
    output = (output or default_email_export_path()).expanduser().resolve()
    settings = db.settings()
    prospects = db.rows(
        "SELECT p.*,c.name AS campaign,c.status AS campaign_status "
        "FROM prospects p JOIN campaigns c ON c.id=p.campaign_id "
        "ORDER BY c.name,p.company,p.id"
    )
    exported_at = iso()
    records: list[dict[str, Any]] = []
    for prospect in prospects:
        contacts = [dict(row) for row in db.rows(
            "SELECT * FROM contacts WHERE prospect_id=? "
            "ORDER BY is_primary DESC,kind,email", (prospect["id"],),
        )]
        if not contacts:
            continue
        message = db.row(
            "SELECT * FROM messages WHERE prospect_id=? AND channel='email' "
            "AND kind='initial' ORDER BY id DESC LIMIT 1", (prospect["id"],),
        )
        message_contact_id = int(message["contact_id"]) if message and message["contact_id"] else 0
        selected = next((row for row in contacts if int(row["id"]) == message_contact_id), None)
        if selected is None and not message:
            selected = choose_contact(contacts, prospect["registrable_domain"])
        selected_id = int(selected["id"]) if selected else 0

        for contact in contacts:
            gate_errors = initial_outreach_gate(
                db, int(prospect["id"]), contact, settings,
                exclude_message_id=int(message["id"]) if message else 0,
            )
            reasons: list[str] = []
            if prospect["campaign"] != "fresh" or prospect["campaign_status"] != "active":
                reasons.append("legacy or inactive campaign is held")
            if prospect["status"] not in {"qualified", "drafted", "pending_approval", "approved"}:
                reasons.append(f"prospect lifecycle is {prospect['status']}")
            if int(contact["id"]) != selected_id:
                reasons.append("alternate contact; use only one initial recipient per company")
            reasons.extend(gate_errors)
            queue_status = str(message["status"] if message else "")
            if queue_status in {"blocked", "rejected", "suppressed", "failed", "sent", "replied", "scheduled"}:
                reasons.append(f"initial email queue status is {queue_status}")
            if message and message["last_error"]:
                reasons.append(str(message["last_error"]))
            reasons = list(dict.fromkeys(reason for reason in reasons if reason))
            recommended = not reasons
            action = _message_action(queue_status)
            records.append({
                "send_priority": 0,
                "recommended_for_outreach": "YES" if recommended else "NO",
                "recommended_action": action if recommended else "HOLD",
                "company": prospect["company"],
                "email": contact["normalized_email"],
                "contact_kind": contact["kind"],
                "market": prospect["market"],
                "pool": prospect["pool"],
                "fit_score": int(prospect["fit_score"]),
                "audit_confidence": prospect["confidence"],
                "campaign": prospect["campaign"],
                "campaign_status": prospect["campaign_status"],
                "prospect_status": prospect["status"],
                "queue_status": queue_status or "not_drafted",
                "draft_subject": message["subject"] if message else "",
                "website": prospect["website"],
                "company_domain": prospect["registrable_domain"],
                "company_linkedin": prospect["linkedin_url"],
                "published_contact_url": contact["published_url"],
                "publication_evidence": contact["evidence_excerpt"],
                "published_by_business": bool(contact["is_published"]),
                "mx_available": bool(contact["mx_available"]),
                "freemail": is_freemail(contact["normalized_email"]),
                "eligibility_notes": "; ".join(reasons) if reasons else "passes current deterministic gates",
                "prospect_discovered_at": prospect["discovered_at"],
                "prospect_audited_at": prospect["audited_at"],
                "contact_recorded_at": contact["created_at"],
                "exported_at": exported_at,
            })

    records.sort(key=lambda row: (
        row["recommended_for_outreach"] != "YES",
        {"APPROVE_DRAFT": 0, "RELEASE_APPROVED_DRAFT": 1,
         "REVIEW_DRAFT": 2, "DRAFT_EMAIL": 3}.get(row["recommended_action"], 9),
        -int(row["fit_score"]), str(row["company"]).lower(), str(row["email"]).lower(),
    ))
    priority = 0
    for row in records:
        if row["recommended_for_outreach"] == "YES":
            priority += 1
            row["send_priority"] = priority
    if recommended_only:
        records = [row for row in records if row["recommended_for_outreach"] == "YES"]

    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EMAIL_EXPORT_COLUMNS, extrasaction="raise")
        writer.writeheader()
        writer.writerows({key: _csv_safe(row.get(key, "")) for key in EMAIL_EXPORT_COLUMNS}
                         for row in records)
    output.chmod(0o600)
    return {
        "path": str(output),
        "rows": len(records),
        "recommended": sum(row["recommended_for_outreach"] == "YES" for row in records),
        "held_or_blocked": sum(row["recommended_for_outreach"] != "YES" for row in records),
    }
