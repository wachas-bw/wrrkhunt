"""Read-only import of the existing JSON/CSV artifacts into a held legacy campaign."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from .config import REPO_ROOT
from .db import Database
from .util import iso, normalize_email, registrable_domain

LEGACY_FILES = (
    "data/stacks.json", "data/contacts.json", "data/tracker.csv",
    "outreach/batch1.json", "outreach/batch2.json", "data/suppression.json",
)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_legacy(root: Path = REPO_ROOT) -> dict[str, int]:
    def load_json(name: str) -> Any:
        return json.loads((root / name).read_text())
    with (root / "data/tracker.csv").open(newline="") as handle:
        tracker = list(csv.DictReader(handle))
    return {
        "stacks": len(load_json("data/stacks.json")),
        "contacts": len(load_json("data/contacts.json")),
        "tracker": len(tracker),
        "batch1": len(load_json("outreach/batch1.json")),
        "batch2": len(load_json("outreach/batch2.json")),
    }


def import_legacy(db: Database, root: Path = REPO_ROOT, dry_run: bool = False) -> dict[str, int]:
    counts = inspect_legacy(root)
    if dry_run:
        return counts
    db.initialize()
    campaign_id = db.ensure_campaign("legacy", "legacy", "held")
    stacks = json.loads((root / "data/stacks.json").read_text())
    contacts = json.loads((root / "data/contacts.json").read_text())
    with (root / "data/tracker.csv").open(newline="") as handle:
        tracker = list(csv.DictReader(handle))
    batches = []
    for filename in ("outreach/batch1.json", "outreach/batch2.json"):
        batch_name = Path(filename).stem
        batches.extend({**item, "_legacy_batch": batch_name}
                       for item in json.loads((root / filename).read_text()))

    stack_by_domain = {registrable_domain(x.get("domain", "")): x for x in stacks}
    contact_by_domain = {registrable_domain(x.get("domain", "")): x for x in contacts}
    tracker_by_domain = {registrable_domain(x.get("domain", "")): x for x in tracker}
    batch_by_domain = {registrable_domain(x.get("domain", "")): x for x in batches}
    domains = sorted((set(stack_by_domain) | set(contact_by_domain) |
                      set(tracker_by_domain) | set(batch_by_domain)) - {""})
    prospect_ids: dict[str, int] = {}
    for domain in domains:
        tr = tracker_by_domain.get(domain, {})
        batch = batch_by_domain.get(domain, {})
        stack = stack_by_domain.get(domain, {})
        pid, _ = db.upsert_prospect(
            campaign_id, domain=domain, company=tr.get("company") or batch.get("company") or domain,
            market=tr.get("market") or batch.get("market") or "",
            pool=tr.get("pool") or "legacy", vertical=batch.get("vertical") or "",
            website=stack.get("resolved") or f"https://{domain}",
            linkedin_url=tr.get("linkedin_person") or tr.get("linkedin_company") or "",
            metadata={"legacy": True, "tracker_status": tr.get("status", ""), "original": tr},
        )
        prospect_ids[domain] = pid
        hook = stack.get("hook") or stack.get("fit_why")
        if hook:
            existing = db.row("SELECT 1 FROM evidence WHERE prospect_id=? AND kind='legacy_audit'", (pid,))
            if not existing:
                db.add_evidence(pid, "legacy_audit", stack.get("resolved") or f"https://{domain}",
                                hook, stack.get("confidence", "legacy"),
                                observed_value=str(stack.get("fit", "")))

    # Legacy addresses are intentionally non-actionable until a fresh visible-page audit.
    with db.transaction(immediate=True) as conn:
        for domain, record in contact_by_domain.items():
            pid = prospect_ids.get(domain)
            if not pid:
                continue
            for address in record.get("emails", []):
                email = normalize_email(address)
                if not email:
                    continue
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO contacts(prospect_id,email,normalized_email,kind,published_url,"
                        "evidence_excerpt,is_published,mx_available,is_primary,created_at) "
                        "VALUES (?,?,?,?,?,?,0,?,0,?)",
                        (pid, email, email, "legacy", f"https://{domain}",
                         "Legacy import; visible publication must be re-audited before use.",
                         int(bool(record.get("mx"))), iso()),
                    )
                except Exception:
                    continue

    for item in batches:
        domain = registrable_domain(item.get("domain", ""))
        pid = prospect_ids.get(domain)
        if not pid:
            continue
        kind = f"legacy_{item['_legacy_batch']}"
        duplicate = db.row(
            "SELECT 1 FROM messages WHERE campaign_id=? AND prospect_id=? AND channel='email' AND kind=?",
            (campaign_id, pid, kind),
        )
        if duplicate:
            continue
        evidence = db.rows("SELECT id FROM evidence WHERE prospect_id=? ORDER BY id", (pid,))
        message_id = db.create_message(
            campaign_id, "email", kind, item.get("body", ""), prospect_id=pid,
            to_address=item.get("to", ""), subject=item.get("subject", ""),
            evidence_ids=[r["id"] for r in evidence], status="blocked",
        )
        with db.transaction(immediate=True) as conn:
            conn.execute("UPDATE messages SET last_error='held legacy backlog' WHERE id=?", (message_id,))

    suppression = json.loads((root / "data/suppression.json").read_text())
    for key in ("customers", "own", "vendors_not_prospects"):
        for domain in suppression.get(key, []):
            db.suppress("domain", domain, f"legacy suppression: {key}", "legacy-import")

    with db.transaction(immediate=True) as conn:
        for rel in LEGACY_FILES:
            path = root / rel
            if path.exists():
                count = counts.get(path.stem.replace("batch", "batch"), 0)
                conn.execute(
                    "INSERT OR IGNORE INTO imports(source_path,source_hash,record_count,imported_at) VALUES (?,?,?,?)",
                    (str(path), _hash(path), count, iso()),
                )
    return counts
