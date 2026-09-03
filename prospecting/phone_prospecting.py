"""Isolated UAE business-phone prospecting with no canonical database writes."""
from __future__ import annotations

import concurrent.futures as futures
import csv
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from enrich.find_contacts import FREE_MAIL
from sources.stack_detect import detect

from .config import APP_HOME, DB_PATH, REPO_ROOT
from .discovery import BLOCKED_HOSTS, SearchResult, exa_search
from .phones import normalize_phone
from .policy import PRODUCT_VENDOR_RE
from .util import evidence_excerpt, normalize_domain, registrable_domain


CSV_COLUMNS = [
    "priority_rank", "tier", "company", "domain", "website", "vertical", "city",
    "emirate", "primary_phone", "normalized_e164", "number_type", "whatsapp_status",
    "alternate_phone_1", "alternate_phone_2", "phone_source_url", "phone_source_type",
    "phone_evidence_excerpt", "observation_date", "corroborating_source",
    "published_business_email", "company_linkedin_url", "fit_score",
    "recommended_ai_employee_role", "fit_reason", "visible_channels", "detected_tools",
    "demand_signal", "audit_confidence", "eligibility_notes", "lead_origin",
]
REJECTED_COLUMNS = CSV_COLUMNS + ["rejection_reasons"]
SNAPSHOT_TABLES = (
    "prospects", "contacts", "evidence", "messages", "suppressions", "delivery_events",
    "source_runs", "campaigns",
)

VERTICAL_TARGETS = {
    "real_estate": 25,
    "healthcare": 20,
    "education": 15,
    "ecommerce": 15,
    "finance": 15,
    "field_services": 10,
}
CITY_TARGETS = {"Dubai": 65, "Abu Dhabi": 20, "Sharjah": 10, "Elsewhere": 5}

VERTICAL_LABELS = {
    "real_estate": "Real estate / property / relocation",
    "healthcare": "Healthcare / clinic / diagnostics",
    "education": "Education / training / cohort",
    "ecommerce": "Ecommerce / retail / marketplace",
    "finance": "Finance / insurance / accounting / business setup",
    "field_services": "Fit-out / facilities / maintenance / rental services",
    "other": "Other enquiry-heavy service",
}

VERTICAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("field_services", re.compile(
        r"\b(?:fit[ -]?out|interior(?: design| designing| fit[ -]?out)?|facilit(?:y|ies)|"
        r"maintenance|cleaning|rental|repair|technical services|contractor|moving|movers|"
        r"logistics|car hire|rent(?:al)?[- ]?(?:a )?car|landscaping|pest control)\b", re.I,
    )),
    ("real_estate", re.compile(
        r"\breal\s*estate|property|properties|brokerage|realtor|relocation|holiday homes?\b",
        re.I,
    )),
    ("healthcare", re.compile(
        r"\b(?:clinic|medical|healthcare|health care|hospital|dental|dentist|diagnostic|"
        r"physio|physiotherapy|dermatology|dermatologist|pharmacy)\b|"
        r"\b(?:aesthetic|wellness)\s+(?:clinic|center|centre|medicine|treatment|dental)\b",
        re.I,
    )),
    ("education", re.compile(
        r"\beducation|school|academy|training|institute|college|university|tuition|"
        r"course|cohort|learning|nursery\b", re.I,
    )),
    ("ecommerce", re.compile(
        r"\be-?commerce|online store|shopify|retail|marketplace|fashion|jewellery|"
        r"jewelry|furniture|florist|consumer brand|boutique|cookware|electronics|"
        r"(?:online )?(?:shop|store)\b", re.I,
    )),
    ("finance", re.compile(
        r"\bfinance|financial|insurance|accounting|accountants?|tax|audit|bookkeeping|"
        r"business setup|company formation|corporate services|mortgage\b", re.I,
    )),
)

ROLE_BY_VERTICAL = {
    "real_estate": "AI lead-qualification and viewing coordinator",
    "healthcare": "AI patient-enquiry and appointment coordinator",
    "education": "AI admissions and learner-support coordinator",
    "ecommerce": "AI sales and customer-support coordinator",
    "finance": "AI client-intake and document-follow-up coordinator",
    "field_services": "AI enquiry, quoting, and dispatch coordinator",
    "other": "AI customer-enquiry and follow-up coordinator",
}

DIRECTORY_DOMAINS = {
    "2gis.ae", "bayut.com", "businesslist.ae", "clutch.co", "dubaipulse.gov.ae",
    "facebook.com", "foursquare.com", "google.com", "instagram.com", "linkedin.com",
    "maps.google.com", "propertyfinder.ae", "reddit.com", "tripadvisor.com",
    "uaeresults.com", "yellowpages.ae", "yelp.com", "youtube.com",
}
DIRECTORY_TERMS = re.compile(
    r"\b(directory|list of|top \d+|best \d+|companies in dubai|business listing|review portal)\b",
    re.I,
)
GIANT_TERMS = re.compile(
    r"\b(emaar|damac|majid al futtaim|al futtaim|mediclinic|nmc healthcare|"
    r"aster dm|gems education|lulu group|landmark group|careem|noon\.com|amazon|"
    r"american hospital|alef group|gant|tramontina|tiger properties|tiger group|"
    r"al masaood)\b|\bhospital\b",
    re.I,
)
COMPANY_JUNK = re.compile(
    r"^(home|contact(?: us)?|about(?: us)?|services?|welcome|team|faq|apply now|"
    r"request an appointment|admission requirements|frequently asked questions|"
    r"best (?:dental|training|it training|accounting)|leading real estate|lab tests|"
    r"pathology laboratory|uae business setup cost calculator|online fashion boutique|"
    r"online form)\b",
    re.I,
)
NON_TARGET_AGENCY_RE = re.compile(
    r"\b(?:seo|digital marketing|web design|advertising|branding|software development|it services)\b|"
    r"\b(?:digital|growth|marketing|creative|web|seo)\s+(?:transformation\s+)?agency\b|"
    r"\bagency\b",
    re.I,
)
PRODUCT_VENDOR_WEBSITE_RE = re.compile(
    r"\b(?:whatsapp (?:business )?(?:api|automation|platform|for e-?commerce)|"
    r"crm (?:software|platform)|shared inbox|helpdesk software|ai chatbot platform)\b",
    re.I,
)
NON_TARGET_SERVICE_RE = re.compile(
    r"\b(?:travel agency|tour operator|holiday packages?|recruitment|staffing|manpower|immigration|"
    r"digital human|creative technology)\b", re.I,
)

DISCOVERY_QUERIES: tuple[tuple[str, str, str], ...] = (
    ("Dubai", "real_estate", 'Dubai boutique real estate agency WhatsApp official website'),
    ("Dubai", "real_estate", 'Dubai property management company WhatsApp official website'),
    ("Abu Dhabi", "real_estate", 'Abu Dhabi real estate agency WhatsApp official website'),
    ("Sharjah", "real_estate", 'Sharjah property company WhatsApp official website'),
    ("Dubai", "healthcare", 'Dubai clinic WhatsApp appointments official website'),
    ("Dubai", "healthcare", 'Dubai diagnostics healthcare WhatsApp official website'),
    ("Abu Dhabi", "healthcare", 'Abu Dhabi clinic WhatsApp official website'),
    ("Sharjah", "healthcare", 'Sharjah dental clinic WhatsApp official website'),
    ("Dubai", "education", 'Dubai training institute WhatsApp admissions official website'),
    ("Abu Dhabi", "education", 'Abu Dhabi academy training WhatsApp official website'),
    ("Dubai", "ecommerce", 'Dubai ecommerce retail brand WhatsApp official website'),
    ("UAE", "ecommerce", 'UAE online store WhatsApp support official website'),
    ("Dubai", "finance", 'Dubai business setup accounting WhatsApp official website'),
    ("Abu Dhabi", "finance", 'Abu Dhabi accounting insurance WhatsApp official website'),
    ("Dubai", "field_services", 'Dubai fit out maintenance company WhatsApp official website'),
    ("Dubai", "field_services", 'Dubai car rental cleaning services WhatsApp official website'),
    ("Sharjah", "field_services", 'Sharjah maintenance company WhatsApp official website'),
    ("UAE", "finance", 'UAE corporate services company formation WhatsApp official website'),
)


@dataclass(frozen=True)
class CanonicalSnapshot:
    sha256: str
    row_counts: dict[str, int]


class _ReadOnlyConnection(sqlite3.Connection):
    """SQLite connection whose context manager also closes the file handle."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _secure(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_canonical_readonly(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open the canonical database without initialization or writable pragmas."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"canonical database does not exist: {resolved}")
    conn = sqlite3.connect(
        f"file:{resolved}?mode=ro", uri=True, timeout=30, factory=_ReadOnlyConnection,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        conn.close()
        raise RuntimeError("could not enable SQLite query-only mode")
    return conn


def canonical_snapshot(path: Path | str = DB_PATH) -> CanonicalSnapshot:
    resolved = Path(path).expanduser().resolve()
    with open_canonical_readonly(resolved) as conn:
        counts = {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in SNAPSHOT_TABLES
        }
    return CanonicalSnapshot(file_sha256(resolved), counts)


def _normalized_company(value: str) -> str:
    value = re.sub(r"\b(?:llc|l\.l\.c|fze|fzco|limited|ltd|inc|pjsc)\b", " ", value or "", flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _candidate_company(company: str, domain: str, summary: dict[str, str] | None = None) -> str:
    value = re.split(r"\s+[|—–]\s+", (company or "").strip(), maxsplit=1)[0].strip()
    site_name = (summary or {}).get("site_name", "").strip()
    if not value or COMPANY_JUNK.search(value) or len(value) > 100:
        if site_name and not COMPANY_JUNK.search(site_name) and len(site_name) <= 100:
            value = site_name
        title = value or (summary or {}).get("title", "")
        value = re.split(r"\s+[|—–]\s+", title, maxsplit=1)[0].strip()
    if not value or COMPANY_JUNK.search(value) or len(value) > 100:
        value = domain.split(".", 1)[0].replace("-", " ").title()
    return value[:160]


def _action_and_suppression_state(conn: sqlite3.Connection) -> dict[str, set[str]]:
    suppressed_domains = {
        registrable_domain(row[0]) for row in conn.execute(
            "SELECT value FROM suppressions WHERE kind='domain'"
        ) if registrable_domain(row[0])
    }
    suppressed_emails = {
        str(row[0]).strip().lower() for row in conn.execute(
            "SELECT value FROM suppressions WHERE kind='email'"
        )
    }
    actioned_domains = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT p.registrable_domain FROM prospects p "
            "JOIN messages m ON m.prospect_id=p.id "
            "WHERE m.status IN ('scheduled','sent','replied') OR m.sent_at IS NOT NULL"
        ) if row[0]
    }
    customer_domains = set()
    for row in conn.execute(
        "SELECT p.registrable_domain,p.metadata_json,c.name,c.kind FROM prospects p "
        "JOIN campaigns c ON c.id=p.campaign_id"
    ):
        metadata = _json(row[1], {})
        if row[2].lower() in {"customers", "customer"} or row[3].lower() == "customer" or any(
            bool(metadata.get(key)) for key in ("existing_customer", "is_customer", "customer")
        ):
            customer_domains.add(row[0])
    return {
        "suppressed_domains": suppressed_domains,
        "suppressed_emails": suppressed_emails,
        "actioned_domains": actioned_domains,
        "customer_domains": customer_domains,
    }


def _existing_candidates(
    conn: sqlite3.Connection, state: dict[str, set[str]], limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT p.*,c.name AS campaign_name FROM prospects p "
        "JOIN campaigns c ON c.id=p.campaign_id WHERE p.market='AE' "
        "ORDER BY CASE WHEN json_extract(p.metadata_json,'$.audit.channels.whatsapp')=1 THEN 0 ELSE 1 END,"
        "p.fit_score DESC,CASE p.confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,p.id"
    ).fetchall()
    for row in rows:
        item = dict(row)
        domain = registrable_domain(item["registrable_domain"] or item["domain"])
        reason = ""
        if domain in state["suppressed_domains"]:
            reason = "domain is suppressed"
        elif domain in state["actioned_domains"]:
            reason = "domain already has scheduled, sent, or replied outreach"
        elif domain in state["customer_domains"]:
            reason = "existing customer"
        if reason:
            excluded.append(_rejected_stub(item, reason, "existing_database"))
            continue
        metadata = _json(item.get("metadata_json"), {})
        candidates.append({
            "company": item.get("company", ""), "domain": domain,
            "website": item.get("website") or f"https://{domain}",
            "vertical_hint": item.get("vertical", ""), "city_hint": "",
            "source_url": item.get("website") or f"https://{domain}",
            "source_excerpt": "Existing UAE prospect; all phone and location facts are re-audited.",
            "source_type": "canonical_read_only", "origin": "existing_database",
            "metadata": metadata, "stored_fit": int(item.get("fit_score") or 0),
            "stored_confidence": item.get("confidence", "none"),
            "linkedin_url": item.get("linkedin_url", ""),
        })
        if len(candidates) >= limit:
            break
    return candidates, excluded


def _rejected_stub(candidate: dict[str, Any], reason: str, origin: str) -> dict[str, Any]:
    domain = registrable_domain(candidate.get("domain") or candidate.get("registrable_domain") or "")
    row = {column: "" for column in REJECTED_COLUMNS}
    row.update({
        "company": candidate.get("company", ""), "domain": domain,
        "website": candidate.get("website", ""), "lead_origin": origin,
        "rejection_reasons": reason, "fit_score": int(candidate.get("fit_score") or 0),
        "audit_confidence": candidate.get("confidence", "none"),
    })
    return row


def _result_candidate(result: SearchResult, city: str, vertical: str, query: str) -> dict[str, Any] | None:
    domain = registrable_domain(result.url)
    if not domain or domain in BLOCKED_HOSTS or domain in DIRECTORY_DOMAINS:
        return None
    if any(domain.endswith("." + blocked) for blocked in BLOCKED_HOSTS | DIRECTORY_DOMAINS):
        return None
    if DIRECTORY_TERMS.search(result.title or ""):
        return None
    return {
        "company": re.split(r"\s+[|—–]\s+", result.title, maxsplit=1)[0][:160],
        "domain": domain, "website": f"https://{domain}", "vertical_hint": vertical,
        "city_hint": city if city != "UAE" else "", "source_url": result.url,
        "source_excerpt": result.excerpt, "source_type": "exa",
        "origin": "fresh_discovery", "metadata": {
            "query": query, "published": result.published,
        }, "stored_fit": 0, "stored_confidence": "none", "linkedin_url": "",
    }


def discover_uae_candidates(
    limit: int, progress: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Use Exa only as discovery/context; first-party pages remain authoritative."""
    progress = progress or (lambda _: None)
    found: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    def search(spec: tuple[str, str, str]):
        city, vertical, query = spec
        return spec, exa_search(query, num_results=10, timeout=75)

    with futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_map = {executor.submit(search, spec): spec for spec in DISCOVERY_QUERIES}
        completed = 0
        for future in futures.as_completed(future_map):
            spec = future_map[future]
            completed += 1
            try:
                (_, _, query), results = future.result()
                for result in results:
                    item = _result_candidate(result, spec[0], spec[1], query)
                    if item and item["domain"] not in found:
                        found[item["domain"]] = item
            except Exception as exc:
                errors.append(f"{spec[2]}: {str(exc)[:500]}")
            progress(f"Exa discovery {completed}/{len(DISCOVERY_QUERIES)}; {len(found)} unique domains")
    return list(found.values())[:limit], errors


def load_broker_evidence(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    """Load optional Apollo/verified-profile evidence without trusting broker phones alone."""
    if not path:
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    with path.expanduser().open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            domain = registrable_domain(row.get("domain") or row.get("website") or "")
            phone = normalize_phone(row.get("phone", ""), "AE")
            source_type = str(row.get("source_type", "apollo")).strip().lower()
            if not domain or not phone or source_type not in {
                "apollo", "verified_google_profile", "verified_meta_profile",
            }:
                continue
            result.setdefault(domain, []).append({
                **phone, "source_url": row.get("source_url", ""),
                "source_type": source_type,
                "evidence_excerpt": evidence_excerpt(row.get("evidence_excerpt", "")),
            })
    return result


def load_verifications(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    with path.expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    items = payload if isinstance(payload, list) else payload.get("items", [])
    return {
        registrable_domain(item.get("domain", "")): item for item in items
        if isinstance(item, dict) and registrable_domain(item.get("domain", ""))
    }


def _classify_vertical(candidate: dict[str, Any], audit: dict[str, Any]) -> str:
    metadata = candidate.get("metadata", {})
    summary = audit.get("site_summary", {})
    identity_text = " ".join([
        candidate.get("company", ""), candidate.get("domain", ""), summary.get("title", ""),
        summary.get("site_name", ""),
    ])
    for vertical, pattern in VERTICAL_PATTERNS:
        if pattern.search(identity_text):
            return vertical
    if NON_TARGET_AGENCY_RE.search(identity_text):
        return "other"
    if NON_TARGET_SERVICE_RE.search(identity_text):
        return "other"
    official_text = f"{identity_text} {summary.get('description', '')}"
    for vertical, pattern in VERTICAL_PATTERNS:
        if pattern.search(official_text):
            return vertical
    if NON_TARGET_AGENCY_RE.search(official_text) or NON_TARGET_SERVICE_RE.search(official_text):
        return "other"
    hint_text = " ".join([
        candidate.get("vertical_hint", ""), json.dumps(metadata, ensure_ascii=False),
    ])
    for vertical, pattern in VERTICAL_PATTERNS:
        if pattern.search(hint_text):
            return vertical
    return "other"


def _infer_location(audit: dict[str, Any], phone: dict[str, Any], hint: str) -> tuple[str, str, str]:
    locations = audit.get("locations", [])
    if locations:
        preferred = next((x for x in locations if x.get("city") == hint), locations[0])
        return preferred["city"], preferred["emirate"], preferred.get("source_url", "")
    national = str(phone.get("national", ""))
    if phone.get("number_type") == "landline" or phone.get("number_type") == "fixed_or_mobile":
        if national.startswith("4"):
            return "Dubai", "Dubai", phone.get("source_url", "")
        if national.startswith("2"):
            return "Abu Dhabi", "Abu Dhabi", phone.get("source_url", "")
        if national.startswith("6"):
            return "Sharjah", "Sharjah", phone.get("source_url", "")
    if hint in {"Dubai", "Abu Dhabi", "Sharjah"}:
        return hint, hint, candidate_location_source(audit)
    return "Elsewhere", "UAE", phone.get("source_url", "")


def candidate_location_source(audit: dict[str, Any]) -> str:
    return audit.get("resolved", "")


def _select_email(audit: dict[str, Any], domain: str, suppressed: set[str]) -> str:
    emails = [str(value).strip().lower() for value in audit.get("channels", {}).get("emails", [])]
    own = [email for email in emails if email.endswith("@" + domain)]
    freemail = [email for email in emails if email.rsplit("@", 1)[-1] in FREE_MAIL]
    for email in own + freemail:
        if email not in suppressed:
            return email
    return ""


def _company_linkedin(candidate: dict[str, Any], audit: dict[str, Any]) -> str:
    if candidate.get("linkedin_url"):
        return candidate["linkedin_url"]
    handles = audit.get("channels", {}).get("linkedin", [])
    return f"https://www.linkedin.com/company/{handles[0]}" if handles else ""


def _visible_channels(audit: dict[str, Any]) -> list[str]:
    channels = audit.get("channels", {})
    values = []
    for key, label in (
        ("whatsapp", "WhatsApp"), ("phone", "Phone"), ("emails", "Email"),
        ("instagram", "Instagram"), ("facebook", "Facebook"),
        ("contact_form", "Contact form"), ("live_chat", "Live chat"),
    ):
        if channels.get(key):
            values.append(label)
    return values


def _score(
    audit: dict[str, Any], phone: dict[str, Any], vertical: str, city: str,
    broker_match: dict[str, Any] | None,
) -> tuple[int, dict[str, int], str]:
    channels = audit.get("channels", {})
    tools = audit.get("tools", [])
    categories = {tool.get("category") for tool in tools}
    visible = _visible_channels(audit)

    workflow = 12 if vertical != "other" else 6
    workflow += 14 if phone.get("is_whatsapp") else 0
    workflow += min(8, max(0, len(visible) - 1) * 2)
    workflow += 4
    workflow += 6 if channels.get("contact_form") or categories & {"booking", "forms", "chat"} else 0
    workflow = min(40, workflow)

    demand = 0
    if audit.get("runs_ads"):
        demand += 10
    if phone.get("is_whatsapp"):
        demand += 6
    if categories & {"booking", "forms", "platform", "email"}:
        demand += 5
    if len(visible) >= 4:
        demand += 4
    if audit.get("site_summary", {}).get("description"):
        demand += 2
    demand = min(25, demand)

    contactability = 11 if phone.get("source_type") == "company_website" else 9
    contactability += 3
    contactability += 4 if phone.get("is_whatsapp") else 0
    if int(phone.get("source_count") or 1) >= 2 or broker_match:
        contactability += 2
    contactability = min(20, contactability)

    legitimacy = 0
    if audit.get("locations"):
        legitimacy += 7
    elif audit.get("domain", "").endswith(".ae") or phone.get("region") == "AE":
        legitimacy += 5
    if audit.get("reachable"):
        legitimacy += 4
    if audit.get("site_summary", {}).get("title") or audit.get("site_summary", {}).get("description"):
        legitimacy += 2
    if not GIANT_TERMS.search(" ".join([
        audit.get("domain", ""), audit.get("site_summary", {}).get("title", ""),
    ])):
        legitimacy += 2
    legitimacy = min(15, legitimacy)

    parts = {
        "product_workflow_fit": workflow, "demand_intent": demand,
        "contactability_provenance": contactability, "uae_legitimacy_activity_size": legitimacy,
    }
    score = sum(parts.values())
    explanation = "; ".join(f"{key.replace('_', ' ')} {value}" for key, value in parts.items())
    return score, parts, explanation


def broker_phone_is_corroborated(
    broker: dict[str, Any], first_party: Iterable[dict[str, Any]],
) -> bool:
    """Apollo never establishes a phone by itself; exact E.164 corroboration is required."""
    trusted = {
        item.get("e164") for item in first_party
        if item.get("source_type") in {
            "company_website", "verified_google_profile", "verified_meta_profile",
        }
    }
    return bool(broker.get("e164") and broker.get("e164") in trusted)


def _demand_signal(audit: dict[str, Any], phone: dict[str, Any]) -> str:
    signals = []
    if phone.get("is_whatsapp"):
        signals.append("publishes a business WhatsApp route")
    if audit.get("runs_ads"):
        signals.append("current paid-ad tracking detected")
    categories = {tool.get("category") for tool in audit.get("tools", [])}
    if categories & {"booking", "forms"}:
        signals.append("booking/form workflow detected")
    if audit.get("channels", {}).get("contact_form"):
        signals.append("active enquiry form")
    if not signals:
        signals.append("public business phone and active service website")
    return "; ".join(signals)


def _audit_candidate(
    candidate: dict[str, Any], state: dict[str, set[str]],
    broker_index: dict[str, list[dict[str, Any]]],
    verifications: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    domain = candidate["domain"]
    try:
        audit = detect(domain, region="AE")
    except Exception as exc:
        return None, _rejected_stub(candidate, f"website audit failed: {exc}", candidate["origin"])
    summary = audit.get("site_summary", {})
    company = _candidate_company(candidate.get("company", ""), domain, summary)
    base_reasons: list[str] = []
    if not audit.get("reachable"):
        base_reasons.append("website audit failed or site is dormant")
    if domain in DIRECTORY_DOMAINS or DIRECTORY_TERMS.search(company):
        base_reasons.append("directory or listing site")
    official_identity = " ".join([
        company, summary.get("title", ""), summary.get("description", ""),
        summary.get("site_name", ""),
    ])
    if GIANT_TERMS.search(official_identity):
        base_reasons.append("giant enterprise outside target team size")
    if PRODUCT_VENDOR_RE.search(official_identity) or PRODUCT_VENDOR_WEBSITE_RE.search(official_identity):
        base_reasons.append("company appears to sell a competing CRM, inbox, helpdesk, or WhatsApp platform")

    phones = [item for item in audit.get("phone_contacts", []) if item.get("region") == "AE"]
    verification = verifications.get(domain, {})
    for item in verification.get("phone_contacts", []) if isinstance(verification, dict) else []:
        normalized = normalize_phone(item.get("phone", item.get("e164", "")), "AE")
        if not normalized:
            continue
        source_type = item.get("source_type", "")
        if source_type not in {"verified_google_profile", "verified_meta_profile"}:
            continue
        phones.append({
            **normalized, "contact_type": item.get("contact_type", normalized["number_type"]),
            "is_whatsapp": bool(item.get("is_whatsapp")), "business_use": True,
            "source_type": source_type, "source_url": item.get("source_url", ""),
            "source_urls": [item.get("source_url", "")],
            "evidence_excerpt": evidence_excerpt(item.get("evidence_excerpt", "")),
            "evidence_excerpts": [evidence_excerpt(item.get("evidence_excerpt", ""))],
            "observed_at": item.get("observed_at") or datetime.now(UTC).isoformat(),
            "confidence": "high", "source_count": 1,
        })
    deduped = {item["e164"]: item for item in phones if item.get("business_use")}
    phones = sorted(
        deduped.values(),
        key=lambda item: (
            not item.get("is_whatsapp"), item.get("confidence") != "high",
            item.get("number_type") not in {"mobile", "fixed_or_mobile"}, item["e164"],
        ),
    )
    if not phones:
        base_reasons.append("no public, structurally valid UAE business-use phone")
    if base_reasons:
        rejected = _rejected_stub(candidate, "; ".join(base_reasons), candidate["origin"])
        rejected.update({"company": company, "audit_confidence": audit.get("confidence", "none")})
        return None, rejected

    phone = phones[0]
    broker_match = next((
        item for item in broker_index.get(domain, [])
        if broker_phone_is_corroborated(item, phones)
    ), None)
    city, emirate, location_source = _infer_location(audit, phone, candidate.get("city_hint", ""))
    vertical = _classify_vertical(candidate, audit)
    score, score_parts, fit_reason = _score(audit, phone, vertical, city, broker_match)
    reasons = []
    confidence = audit.get("confidence", "none")
    if confidence in {"none", "low"} and not verification.get("browser_verified"):
        reasons.append("low-confidence or JavaScript-heavy audit requires browser verification")
    if score < 75:
        reasons.append(f"fit score {score} is below 75")
    if not audit.get("locations") and not (domain.endswith(".ae") and phone.get("region") == "AE"):
        reasons.append("UAE business-location evidence not detected")
    published_email = _select_email(audit, domain, state["suppressed_emails"])
    if published_email in state["suppressed_emails"]:
        reasons.append("published business email is suppressed")

    source_urls = [url for url in phone.get("source_urls", []) if url]
    corroborating = ""
    if len(source_urls) > 1:
        corroborating = source_urls[1]
    elif broker_match:
        corroborating = broker_match.get("source_url", "")
    elif location_source and location_source != phone.get("source_url"):
        corroborating = location_source

    visible = _visible_channels(audit)
    tools = [tool.get("name", "") for tool in audit.get("tools", [])]
    eligibility = [
        "Public business-use UAE number verified",
        "Canonical database read-only exclusion checks passed",
        "Tooling described only as detected/not detected",
    ]
    if not any(tool.get("category") in {"crm", "support", "chat", "wa_bsp"}
               for tool in audit.get("tools", [])):
        eligibility.append("CRM/shared-inbox tooling not detected")

    row = {column: "" for column in REJECTED_COLUMNS}
    row.update({
        "company": company, "domain": domain, "website": audit.get("resolved") or candidate["website"],
        "vertical": VERTICAL_LABELS[vertical], "vertical_key": vertical, "city": city,
        "emirate": emirate, "primary_phone": phone.get("display") or phone["e164"],
        "normalized_e164": phone["e164"], "number_type": phone.get("number_type", ""),
        "whatsapp_status": "confirmed on first-party page" if phone.get("is_whatsapp") else "not detected",
        "alternate_phone_1": phones[1]["e164"] if len(phones) > 1 else "",
        "alternate_phone_2": phones[2]["e164"] if len(phones) > 2 else "",
        "phone_source_url": phone.get("source_url", ""),
        "phone_source_type": phone.get("source_type", "company_website"),
        "phone_evidence_excerpt": phone.get("evidence_excerpt", ""),
        "observation_date": str(phone.get("observed_at", ""))[:10],
        "corroborating_source": corroborating, "published_business_email": published_email,
        "company_linkedin_url": _company_linkedin(candidate, audit), "fit_score": score,
        "score_parts": score_parts, "recommended_ai_employee_role": ROLE_BY_VERTICAL[vertical],
        "fit_reason": fit_reason, "visible_channels": ", ".join(visible),
        "detected_tools": ", ".join(tools) if tools else "not detected",
        "demand_signal": _demand_signal(audit, phone), "audit_confidence": confidence,
        "eligibility_notes": "; ".join(eligibility), "lead_origin": candidate["origin"],
        "rejection_reasons": "; ".join(reasons),
        "source_discovery_url": candidate.get("source_url", ""),
    })
    if reasons:
        return None, row
    row["tier"] = "A" if score >= 85 and confidence in {"medium", "high"} else "B"
    return row, None


def _quota_city(value: str) -> str:
    return value if value in {"Dubai", "Abu Dhabi", "Sharjah"} else "Elsewhere"


def quota_rank(rows: list[dict[str, Any]], target: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Greedy soft-quota ranking; only already-qualified rows participate."""
    remaining = list(rows)
    selected: list[dict[str, Any]] = []
    vertical_counts = {key: 0 for key in VERTICAL_TARGETS}
    city_counts = {key: 0 for key in CITY_TARGETS}
    while remaining and len(selected) < target:
        def priority(row: dict[str, Any]) -> tuple[int, int, int, str]:
            vertical = row.get("vertical_key", "other")
            city = _quota_city(row.get("city", ""))
            vertical_bonus = 4 if vertical in VERTICAL_TARGETS and vertical_counts[vertical] < VERTICAL_TARGETS[vertical] else 0
            city_bonus = 3 if city_counts[city] < CITY_TARGETS[city] else 0
            tier_bonus = 1 if row.get("tier") == "A" else 0
            return (int(row["fit_score"]) + vertical_bonus + city_bonus + tier_bonus,
                    int(row["fit_score"]), 1 if row.get("audit_confidence") == "high" else 0,
                    row.get("domain", ""))
        chosen = max(remaining, key=priority)
        remaining.remove(chosen)
        selected.append(chosen)
        vertical = chosen.get("vertical_key", "other")
        if vertical in vertical_counts:
            vertical_counts[vertical] += 1
        city_counts[_quota_city(chosen.get("city", ""))] += 1
    for rank, row in enumerate(selected, 1):
        row["priority_rank"] = rank
    return selected, remaining


def _formula_safe(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = str(value if value is not None else "").replace("\x00", "")
    if re.match(r"^[\t\r\n ]*[=+\-@]", text):
        return "'" + text
    return text


def write_private_csv(path: Path, rows: Iterable[dict[str, Any]], columns: list[str]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _secure(path.parent, 0o700)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8-sig", newline="", dir=path.parent, delete=False,
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _formula_safe(row.get(column, "")) for column in columns})
        temporary = Path(handle.name)
    _secure(temporary, 0o600)
    os.replace(temporary, path)
    _secure(path, 0o600)


def _write_private_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _secure(path, 0o600)


def _audit_candidates_supervised(
    candidates: list[dict[str, Any]], state: dict[str, set[str]],
    broker_index: dict[str, list[dict[str, Any]]],
    verifications: dict[str, dict[str, Any]], run_dir: Path,
    progress: Callable[[str], None], timeout_seconds: int = 90,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Audit each domain in a killable process with no canonical database access.

    Library-level DNS/TLS timeouts can fail to interrupt some macOS resolver or SSL
    states. A process boundary makes the wall-clock limit enforceable and prevents one
    hostile or broken site from blocking the full run.
    """
    context_path = run_dir / "audit_context.json"
    _write_private_json(context_path, {
        "state": {key: sorted(value) for key, value in state.items()},
        "broker_index": broker_index, "verifications": verifications,
    })
    work_dir = run_dir / "candidate-audits"
    work_dir.mkdir(mode=0o700)
    pending = deque(enumerate(candidates))
    active: dict[int, dict[str, Any]] = {}
    results: dict[int, tuple[dict[str, Any] | None, dict[str, Any] | None]] = {}
    completed = 0

    def launch(index: int, candidate: dict[str, Any]) -> None:
        input_path = work_dir / f"{index:04d}.input.json"
        output_path = work_dir / f"{index:04d}.output.json"
        _write_private_json(input_path, candidate)
        process = subprocess.Popen(
            [
                sys.executable, "-m", "prospecting.phone_audit_worker",
                "--candidate", str(input_path), "--context", str(context_path),
                "--output", str(output_path),
            ],
            cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        active[process.pid] = {
            "process": process, "index": index, "candidate": candidate,
            "output": output_path, "started": time.monotonic(),
        }

    while pending or active:
        while pending and len(active) < 4:
            launch(*pending.popleft())
        progressed = False
        for pid, item in list(active.items()):
            process: subprocess.Popen[Any] = item["process"]
            returncode = process.poll()
            timed_out = time.monotonic() - item["started"] > timeout_seconds
            if returncode is None and not timed_out:
                continue
            progressed = True
            if timed_out and returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                results[item["index"]] = (
                    None,
                    _rejected_stub(
                        item["candidate"],
                        f"website audit exceeded the {timeout_seconds}-second wall-clock limit",
                        item["candidate"]["origin"],
                    ),
                )
            elif returncode == 0 and item["output"].is_file():
                try:
                    payload = json.loads(item["output"].read_text(encoding="utf-8"))
                    results[item["index"]] = (payload.get("accepted"), payload.get("rejected"))
                except (OSError, json.JSONDecodeError) as exc:
                    results[item["index"]] = (
                        None,
                        _rejected_stub(
                            item["candidate"], f"isolated audit output invalid: {exc}",
                            item["candidate"]["origin"],
                        ),
                    )
            else:
                results[item["index"]] = (
                    None,
                    _rejected_stub(
                        item["candidate"], f"isolated audit worker failed with status {returncode}",
                        item["candidate"]["origin"],
                    ),
                )
            del active[pid]
            completed += 1
            if completed % 10 == 0 or completed == len(candidates):
                qualified = sum(1 for good, _ in results.values() if good)
                progress(
                    f"Website audit {completed}/{len(candidates)}; "
                    f"{qualified} currently qualified"
                )
        if not progressed:
            time.sleep(0.20)

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index in range(len(candidates)):
        good, bad = results[index]
        if good:
            accepted.append(good)
        if bad:
            rejected.append(bad)
    return accepted, rejected


def _serialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items()} for row in rows]


def load_candidate_seed(path: Path) -> list[dict[str, Any]]:
    path = path.expanduser().resolve()
    if path.is_dir():
        files = sorted(path.glob("*.input.json"))
        return [json.loads(item.read_text(encoding="utf-8")) for item in files]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("candidates", [])
    if not isinstance(payload, list):
        raise ValueError("candidate seed must be a JSON list or a candidate-audits directory")
    return [item for item in payload if isinstance(item, dict)]


def collect_phone_prospects(
    *, canonical_db: Path = DB_PATH, target: int = 100, max_candidates: int = 250,
    use_exa: bool = True, broker_input: Path | None = None,
    verification_input: Path | None = None, candidate_input: Path | None = None,
    run_home: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if target < 1 or target > 100:
        raise ValueError("target must be between 1 and 100")
    if max_candidates < target:
        raise ValueError("max_candidates cannot be lower than target")
    progress = progress or (lambda message: print(message, flush=True))
    run_root = (run_home or (APP_HOME / "phone-prospecting-runs")).expanduser().resolve()
    run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _secure(run_root, 0o700)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
    run_dir = run_root / run_id
    run_dir.mkdir(mode=0o700)

    before = canonical_snapshot(canonical_db)
    progress(f"Canonical snapshot {before.sha256[:12]} captured in query-only mode")
    with open_canonical_readonly(canonical_db) as conn:
        state = _action_and_suppression_state(conn)
        if candidate_input:
            existing, preexcluded = [], []
        else:
            existing_goal = min(max_candidates, max(target, round(max_candidates * 0.60)))
            existing, preexcluded = _existing_candidates(conn, state, existing_goal)

    source_errors: list[str] = []
    fresh: list[dict[str, Any]] = []
    if candidate_input:
        seeded = load_candidate_seed(candidate_input)
        excluded = (
            state["suppressed_domains"] | state["actioned_domains"] | state["customer_domains"]
        )
        by_domain = {}
        for item in seeded:
            domain = registrable_domain(item.get("domain", ""))
            if domain and domain not in excluded:
                item["domain"] = domain
                by_domain.setdefault(domain, item)
        candidates = list(by_domain.values())[:max_candidates]
        progress(f"Reused {len(candidates)} privately staged candidates; Exa was not called")
    elif use_exa:
        fresh, source_errors = discover_uae_candidates(max_candidates - len(existing), progress)
    if not candidate_input:
        seen_domains = {item["domain"] for item in existing}
        fresh = [item for item in fresh if item["domain"] not in seen_domains]
        for item in fresh:
            domain = item["domain"]
            if domain in state["suppressed_domains"] or domain in state["actioned_domains"] or domain in state["customer_domains"]:
                preexcluded.append(_rejected_stub(item, "canonical exclusion conflict", "fresh_discovery"))
        fresh = [item for item in fresh if item["domain"] not in (
            state["suppressed_domains"] | state["actioned_domains"] | state["customer_domains"]
        )]
        candidates = (existing + fresh)[:max_candidates]
        if len(candidates) < max_candidates:
            with open_canonical_readonly(canonical_db) as conn:
                more, _ = _existing_candidates(conn, state, max_candidates)
            by_domain = {item["domain"]: item for item in candidates}
            for item in more:
                by_domain.setdefault(item["domain"], item)
            candidates = list(by_domain.values())[:max_candidates]
    existing_count = sum(item.get("origin") == "existing_database" for item in candidates)
    fresh_count = sum(item.get("origin") == "fresh_discovery" for item in candidates)
    progress(
        f"Auditing {len(candidates)} isolated candidates "
        f"({existing_count} existing, {fresh_count} fresh)"
    )

    broker_index = load_broker_evidence(broker_input)
    verifications = load_verifications(verification_input)
    accepted, audited_rejections = _audit_candidates_supervised(
        candidates, state, broker_index, verifications, run_dir, progress,
    )
    rejected = list(preexcluded) + audited_rejections

    # Resolve duplicate public business numbers in favour of the higher-scoring company.
    unique: list[dict[str, Any]] = []
    phones_seen: set[str] = set()
    companies_seen: set[str] = set()
    for row in sorted(
        accepted,
        key=lambda item: (-int(item["fit_score"]), item["audit_confidence"] != "high", item["domain"]),
    ):
        phone = row["normalized_e164"]
        company_key = _normalized_company(row["company"])
        if phone in phones_seen:
            row["rejection_reasons"] = "duplicate E.164 business number"
            rejected.append(row)
            continue
        if company_key and company_key in companies_seen:
            row["rejection_reasons"] = "duplicate normalized company name"
            rejected.append(row)
            continue
        phones_seen.add(phone)
        if company_key:
            companies_seen.add(company_key)
        unique.append(row)

    selected, overflow = quota_rank(unique, target)
    for row in overflow:
        row["rejection_reasons"] = "qualified but outside requested target cap"
        rejected.append(row)
    for row in rejected:
        row.pop("vertical_key", None)
        row.pop("score_parts", None)
    for row in selected:
        row.pop("vertical_key", None)
        row.pop("score_parts", None)

    after = canonical_snapshot(canonical_db)
    unchanged = before == after
    stage = {
        "run_id": run_id, "run_dir": str(run_dir), "created_at": datetime.now(UTC).isoformat(),
        "canonical_db": str(Path(canonical_db).expanduser().resolve()),
        "canonical_before": asdict(before), "canonical_after": asdict(after),
        "canonical_db_unchanged": unchanged, "target": target,
        "candidates_audited": len(candidates), "qualified": len(selected),
        "rejected": len(rejected), "source_errors": source_errors,
        "broker_records_loaded": sum(map(len, broker_index.values())),
        "browser_verifications_loaded": len(verifications),
        "rows": _serialize_rows(selected), "rejected_rows": _serialize_rows(rejected),
    }
    stage_path = run_dir / "stage.json"
    _write_private_json(stage_path, stage)
    preview_path = run_dir / "top_20_review.csv"
    write_private_csv(preview_path, selected[:20], CSV_COLUMNS)
    if not unchanged:
        raise RuntimeError(
            f"canonical database changed during the run; staged evidence retained at {stage_path}"
        )
    return {**stage, "stage_path": str(stage_path), "preview_path": str(preview_path)}


def finalize_phone_stage(stage_path: Path, output: Path) -> dict[str, Any]:
    stage_path = stage_path.expanduser().resolve()
    payload = json.loads(stage_path.read_text(encoding="utf-8"))
    expected = CanonicalSnapshot(**payload["canonical_after"])
    current = canonical_snapshot(Path(payload["canonical_db"]))
    if current != expected or not payload.get("canonical_db_unchanged"):
        raise RuntimeError("canonical database no longer matches the reviewed stage; refusing export")
    output = output.expanduser().resolve()
    rejected_path = output.with_name(output.stem + "_rejected" + output.suffix)
    write_private_csv(output, payload.get("rows", []), CSV_COLUMNS)
    write_private_csv(rejected_path, payload.get("rejected_rows", []), REJECTED_COLUMNS)
    return {
        "output": str(output), "rejected_output": str(rejected_path),
        "qualified": len(payload.get("rows", [])),
        "rejected": len(payload.get("rejected_rows", [])),
        "canonical_db_unchanged": True, "canonical_sha256": current.sha256,
        "canonical_row_counts": current.row_counts, "stage_path": str(stage_path),
    }


def apply_manual_browser_review(
    stage_path: Path,
    outcomes: dict[str, dict[str, str]],
    *,
    reviewer: str = "Chrome read-only first-party verification",
) -> dict[str, Any]:
    """Create a separate reviewed stage without mutating the source stage or database.

    ``outcomes`` is keyed by registrable domain. Each value must contain a ``status``
    of ``confirmed`` or ``excluded``. Exclusions also require a concrete ``reason``.
    The resulting top 20 must all have explicit confirmations, which makes the manual
    acceptance gate mechanically enforceable before CSV finalization.
    """
    stage_path = stage_path.expanduser().resolve()
    payload = json.loads(stage_path.read_text(encoding="utf-8"))
    expected = CanonicalSnapshot(**payload["canonical_after"])
    current = canonical_snapshot(Path(payload["canonical_db"]))
    if current != expected or not payload.get("canonical_db_unchanged"):
        raise RuntimeError("canonical database no longer matches the staged run")

    normalized_outcomes: dict[str, dict[str, str]] = {}
    for raw_domain, outcome in outcomes.items():
        domain = registrable_domain(raw_domain)
        status = str(outcome.get("status", "")).strip().lower()
        reason = str(outcome.get("reason", "")).strip()
        if not domain or status not in {"confirmed", "excluded"}:
            raise ValueError(f"invalid browser-review outcome for {raw_domain!r}")
        if status == "excluded" and not reason:
            raise ValueError(f"excluded browser-review outcome needs a reason: {domain}")
        normalized_outcomes[domain] = {"status": status, "reason": reason}

    retained: list[dict[str, Any]] = []
    newly_rejected: list[dict[str, Any]] = []
    staged_domains = {registrable_domain(row.get("domain", "")) for row in payload.get("rows", [])}
    unknown = sorted(set(normalized_outcomes) - staged_domains)
    if unknown:
        raise ValueError(f"browser review contains domains outside the staged finalists: {unknown}")

    for original in payload.get("rows", []):
        row = dict(original)
        domain = registrable_domain(row.get("domain", ""))
        outcome = normalized_outcomes.get(domain)
        if outcome and outcome["status"] == "excluded":
            row["rejection_reasons"] = f"manual browser review: {outcome['reason']}"
            newly_rejected.append(row)
        else:
            retained.append(row)

    for rank, row in enumerate(retained, start=1):
        row["priority_rank"] = rank

    unconfirmed_top = [
        row.get("domain", "") for row in retained[:20]
        if normalized_outcomes.get(registrable_domain(row.get("domain", "")), {}).get("status")
        != "confirmed"
    ]
    if unconfirmed_top:
        raise ValueError(
            "the final top 20 are not all explicitly browser-confirmed: "
            + ", ".join(unconfirmed_top)
        )

    reviewed_at = datetime.now(UTC).isoformat()
    payload["rows"] = retained
    payload["rejected_rows"] = list(payload.get("rejected_rows", [])) + newly_rejected
    payload["qualified"] = len(retained)
    payload["rejected"] = len(payload["rejected_rows"])
    payload["manual_review"] = {
        "reviewed_at": reviewed_at,
        "reviewer": reviewer,
        "reviewed_count": len(normalized_outcomes),
        "confirmed_count": sum(
            item["status"] == "confirmed" for item in normalized_outcomes.values()
        ),
        "excluded_count": len(newly_rejected),
        "final_top_20_confirmed": len(retained) >= 20,
        "outcomes": normalized_outcomes,
    }
    payload["canonical_after_review"] = asdict(current)

    reviewed_stage_path = stage_path.with_name("reviewed-stage.json")
    _write_private_json(reviewed_stage_path, payload)
    reviewed_preview_path = stage_path.with_name("reviewed_top_20.csv")
    write_private_csv(reviewed_preview_path, retained[:20], CSV_COLUMNS)
    return {
        "stage_path": str(reviewed_stage_path),
        "preview_path": str(reviewed_preview_path),
        "qualified": len(retained),
        "rejected": len(payload["rejected_rows"]),
        "manual_review": payload["manual_review"],
        "canonical_db_unchanged": True,
        "canonical_sha256": current.sha256,
    }


def prospect_phones(
    *, output: Path, canonical_db: Path = DB_PATH, target: int = 100,
    max_candidates: int = 250, use_exa: bool = True, broker_input: Path | None = None,
    verification_input: Path | None = None, candidate_input: Path | None = None,
    stage_only: bool = False,
    finalize_stage: Path | None = None,
) -> dict[str, Any]:
    if finalize_stage:
        return finalize_phone_stage(finalize_stage, output)
    stage = collect_phone_prospects(
        canonical_db=canonical_db, target=target, max_candidates=max_candidates,
        use_exa=use_exa, broker_input=broker_input,
        verification_input=verification_input, candidate_input=candidate_input,
    )
    if stage_only:
        return {
            key: stage[key] for key in (
                "run_id", "stage_path", "preview_path", "candidates_audited", "qualified",
                "rejected", "canonical_db_unchanged", "source_errors",
            )
        }
    return finalize_phone_stage(Path(stage["stage_path"]), output)
