"""Public-site audit, evidence ingestion, contact validation, and qualification."""
from __future__ import annotations

import json
import re
import urllib.request
import concurrent.futures as futures
from datetime import UTC, datetime
from typing import Any

from enrich.find_contacts import FREE_MAIL, enrich
from sources.stack_detect import UA, detect

from .db import Database
from .policy import PRODUCT_VENDOR_RE, choose_contact, published_address_matches_business, regional_gate
from .util import evidence_excerpt, is_freemail, iso, normalize_email, registrable_domain

UK_CORPORATE_RE = re.compile(
    r"\b(?:limited|ltd\.?|plc|limited liability partnership|llp)\b|"
    r"(?:company|registration)\s+(?:number|no\.?)\s*[:#]?\s*[A-Z0-9]{6,10}", re.I,
)


def verify_uk_corporate(domain: str) -> tuple[str, dict[str, str] | None]:
    for path in ("/privacy", "/privacy-policy", "/terms", "/terms-and-conditions", "/about"):
        url = f"https://{domain}{path}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=12) as response:
                html = response.read(800_000).decode("utf-8", "replace")
        except Exception:
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
        match = UK_CORPORATE_RE.search(text)
        if not match:
            continue
        excerpt = text[max(0, match.start() - 100):match.end() + 150].strip()
        token = match.group(0).lower()
        corporate_type = "llp" if "llp" in token or "partnership" in token else (
            "plc" if "plc" in token else "limited_company")
        return corporate_type, {"source_url": url, "excerpt": evidence_excerpt(excerpt)}
    return "unknown", None


def _contact_kind(email: str, role_addresses: list[str], personal: list[str]) -> str:
    if email in personal:
        return "person"
    if email in role_addresses:
        return "role"
    if is_freemail(email):
        return "published_freemail"
    return "published"


def audit_prospect(db: Database, prospect_id: int, source_run_id: int | None = None) -> dict[str, Any]:
    prospect = db.row("SELECT * FROM prospects WHERE id=?", (prospect_id,))
    if not prospect:
        raise ValueError("prospect not found")
    if prospect["status"] != "discovered":
        return {"prospect_id": prospect_id, "status": prospect["status"], "skipped": True}

    stack = detect(prospect["domain"], region=prospect["market"] or None)
    contacts = enrich(prospect["domain"], region=prospect["market"] or None)
    confidence = stack.get("confidence", "none")
    for item in stack.get("evidence", []):
        db.add_evidence(
            prospect_id, item.get("kind", "site_audit"), item.get("source_url") or stack.get("resolved"),
            item.get("excerpt") or item.get("observed_value", ""), item.get("confidence", confidence),
            item.get("observed_value", ""), source_run_id=source_run_id,
            detected_at=item.get("detected_at"),
        )

    corporate_type = prospect["corporate_type"]
    if prospect["market"] == "GB":
        corporate_type, corp_evidence = verify_uk_corporate(prospect["domain"])
        if corp_evidence:
            db.add_evidence(prospect_id, "corporate_status", corp_evidence["source_url"],
                            corp_evidence["excerpt"], "medium", corporate_type,
                            source_run_id=source_run_id)

    evidence_by_email = {x["email"]: x for x in contacts.get("email_evidence", [])}
    candidates = []
    for email in contacts.get("emails", []):
        normalized = normalize_email(email)
        evidence = evidence_by_email.get(normalized)
        if not normalized or not evidence:
            continue
        mail_domain = normalized.rsplit("@", 1)[1]
        if not published_address_matches_business(prospect["registrable_domain"], normalized,
                                                  prospect["company"]):
            # Third-party custom domains are often web-designer credits, not business contacts.
            continue
        mx = bool(contacts.get("mx_by_domain", {}).get(mail_domain, False))
        candidates.append({
            "email": normalized, "normalized_email": normalized,
            "kind": _contact_kind(normalized, contacts.get("role_emails", []),
                                  contacts.get("personal_emails", [])),
            "published_url": evidence["source_url"], "evidence_excerpt": evidence["excerpt"],
            "is_published": True, "mx_available": mx,
        })
    chosen = choose_contact(candidates, prospect["registrable_domain"])
    for candidate in candidates:
        contact_id = db.add_contact(
            prospect_id, candidate["email"], kind=candidate["kind"],
            published_url=candidate["published_url"], excerpt=candidate["evidence_excerpt"],
            mx=candidate["mx_available"], primary=bool(chosen and candidate["email"] == chosen["email"]),
        )
        if contact_id:
            db.add_evidence(prospect_id, "published_contact", candidate["published_url"],
                            candidate["evidence_excerpt"], "high", candidate["email"],
                            source_run_id=source_run_id, detected_at=contacts.get("detected_at"))

    fit = int(stack.get("fit", 0))
    metadata = json.loads(prospect["metadata_json"] or "{}")
    metadata["audit"] = {
        "fit_why": stack.get("fit_why", ""), "module_fit": stack.get("module_fit", {}),
        "gaps": stack.get("gaps", []), "channels": stack.get("channels", {}),
        "tools": stack.get("tools", []), "resolved": stack.get("resolved", ""),
    }
    db.transition_prospect(prospect_id, "audited", fit_score=fit, confidence=confidence,
                           corporate_type=corporate_type, audited_at=iso(),
                           metadata_json=json.dumps(metadata, ensure_ascii=False))

    stored_contacts = [dict(r) for r in db.rows(
        "SELECT * FROM contacts WHERE prospect_id=? ORDER BY is_primary DESC,id", (prospect_id,))]
    chosen = choose_contact(stored_contacts, prospect["registrable_domain"])
    if chosen:
        with db.transaction(immediate=True) as conn:
            conn.execute("UPDATE contacts SET is_primary=0 WHERE prospect_id=?", (prospect_id,))
            conn.execute("UPDATE contacts SET is_primary=1 WHERE id=?", (chosen["id"],))
    evidence_count = db.row(
        "SELECT COUNT(*) AS n FROM evidence WHERE prospect_id=? AND kind IN "
        "('channel_summary','stack_detection','corporate_status')", (prospect_id,)
    )["n"]
    errors = []
    threshold = int(db.setting("fit_threshold", 75))
    if fit < threshold:
        errors.append(f"fit score {fit} is below {threshold}")
    if confidence in {"none", "low"}:
        errors.append("low-confidence JS-heavy audit requires browser verification")
    if PRODUCT_VENDOR_RE.search(prospect["company"] or ""):
        errors.append("company appears to sell a competing CRM, inbox, or helpdesk product")
    if not evidence_count:
        errors.append("usable website evidence was not detected")
    if not chosen:
        errors.append("no visibly published, deliverable business contact")
    if chosen:
        errors.extend(regional_gate(db.settings(), prospect["market"],
                                    corporate_type=corporate_type, email=chosen["email"]))
    if not stack.get("reachable"):
        errors.append("website audit failed")
    if errors:
        metadata["qualification_errors"] = errors
        with db.transaction(immediate=True) as conn:
            conn.execute("UPDATE prospects SET metadata_json=?,updated_at=? WHERE id=?",
                         (json.dumps(metadata, ensure_ascii=False), iso(), prospect_id))
        return {"prospect_id": prospect_id, "status": "audited", "fit": fit, "errors": errors}

    db.transition_prospect(prospect_id, "qualified", qualified_at=iso())
    return {"prospect_id": prospect_id, "status": "qualified", "fit": fit,
            "contact": chosen["email"]}


def audit_pending(db: Database, campaign: str = "fresh", limit: int = 60,
                  source_run_id: int | None = None) -> list[dict[str, Any]]:
    rows = db.rows(
        "SELECT p.id,p.market,p.pool FROM prospects p JOIN campaigns c ON c.id=p.campaign_id "
        "WHERE c.name=? AND p.status='discovered' ORDER BY p.discovered_at,p.id",
        (campaign,),
    )
    # Select a balanced 50/30/20 audited cohort, round-robin across markets. If a
    # pool is short, fill the remainder without weakening the qualification score.
    quotas = {
        "service_smb": round(limit * 0.50),
        "agency_directory": round(limit * 0.30),
    }
    quotas["funded_startup"] = limit - sum(quotas.values())
    selected: list[Any] = []
    selected_ids: set[int] = set()
    for pool, quota in quotas.items():
        by_market: dict[str, list[Any]] = {}
        for row in rows:
            if row["pool"] == pool:
                by_market.setdefault(row["market"], []).append(row)
        while len([x for x in selected if x["pool"] == pool]) < quota and any(by_market.values()):
            for market in sorted(by_market):
                if by_market[market] and len([x for x in selected if x["pool"] == pool]) < quota:
                    item = by_market[market].pop(0)
                    selected.append(item)
                    selected_ids.add(item["id"])
    for row in rows:
        if len(selected) >= limit:
            break
        if row["id"] not in selected_ids:
            selected.append(row)
            selected_ids.add(row["id"])

    def run(row):
        try:
            return audit_prospect(db, row["id"], source_run_id)
        except Exception as exc:
            try:
                db.transition_prospect(row["id"], "failed")
            except Exception:
                pass
            return {"prospect_id": row["id"], "status": "failed", "errors": [str(exc)]}

    results = []
    with futures.ThreadPoolExecutor(max_workers=4) as executor:
        for result in executor.map(run, selected):
            results.append(result)
    return results
