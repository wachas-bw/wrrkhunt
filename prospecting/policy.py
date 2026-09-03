"""Deterministic qualification and compliance gates."""
from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from .db import Database
from .util import is_freemail, iso, normalize_email, parse_iso, registrable_domain, utcnow

UNREPLYABLE = re.compile(
    r"^(?:no[._-]?reply|donotreply|do[._-]?not[._-]?reply|bounce|mailer[-_]?daemon|"
    r"postmaster|abuse|webmaster)$", re.I,
)
WRONG_AUDIENCE = re.compile(
    r"^(?:careers?|jobs?|hr|recruit(?:ment|ing)?|hiring|resume|cv|internships?|"
    r"support|help|privacy|legal|billing|accounts?|security)(?:$|[._+-])", re.I,
)
CORPORATE_TYPES = {"limited_company", "llp", "plc", "corporation"}
PRODUCT_VENDOR_RE = re.compile(
    r"\b(?:crm (?:software|platform|system|for)|customer relationship management (?:software|platform)|"
    r"shared inbox (?:software|platform)|helpdesk (?:software|platform)|"
    r"whatsapp (?:bsp|api|automation platform)|ai receptionist|virtual receptionist|"
    r"call automation platform)\b", re.I,
)


def address_problem(email: str) -> str:
    normalized = normalize_email(email)
    if not normalized:
        return "invalid email"
    local = normalized.split("@", 1)[0]
    if UNREPLYABLE.match(local):
        return "unreplyable inbox"
    if WRONG_AUDIENCE.match(local):
        return "wrong-audience inbox"
    return ""


def published_address_matches_business(prospect_domain: str, email: str,
                                       company: str = "") -> bool:
    """Accept the company domain, published freemail, or a related brand domain."""
    email_domain = registrable_domain(email.rsplit("@", 1)[-1]) if "@" in email else ""
    prospect_domain = registrable_domain(prospect_domain)
    if email_domain == prospect_domain or is_freemail(email):
        return True
    email_brand = re.sub(r"[^a-z0-9]", "", email_domain.split(".", 1)[0])
    prospect_brand = re.sub(r"[^a-z0-9]", "", prospect_domain.split(".", 1)[0])
    common = 0
    for left, right in zip(email_brand, prospect_brand):
        if left != right:
            break
        common += 1
    if common >= 5:
        return True
    company_tokens = [re.sub(r"[^a-z0-9]", "", token.lower())
                      for token in re.findall(r"[A-Za-z0-9]+", company)]
    return any(len(token) >= 5 and token in email_brand and token in prospect_brand
               for token in company_tokens)


def regional_gate(settings: dict[str, Any], market: str, *, corporate_type: str = "unknown",
                  email: str = "") -> list[str]:
    market = (market or "").upper()
    policies = settings.get("market_policies") or {}
    policy = policies.get(market)
    errors: list[str] = []
    if not policy or not policy.get("enabled", False):
        errors.append(f"regional policy for {market or 'unknown'} is disabled")
        return errors
    if market == "US" and not str(settings.get("business_postal_address") or "").strip():
        errors.append("US sending requires a configured business postal address")
    if market == "GB":
        if corporate_type not in CORPORATE_TYPES:
            errors.append("UK prospect is not verified as an incorporated corporate business")
        if email and is_freemail(email):
            errors.append("UK freemail contacts are blocked")
    return errors


def initial_outreach_gate(db: Database, prospect_id: int, contact: dict[str, Any],
                          settings: dict[str, Any], exclude_message_id: int = 0) -> list[str]:
    prospect = db.row("SELECT * FROM prospects WHERE id=?", (prospect_id,))
    if not prospect:
        return ["prospect not found"]
    email = contact.get("normalized_email") or contact.get("email") or ""
    errors = regional_gate(settings, prospect["market"],
                           corporate_type=prospect["corporate_type"], email=email)
    if int(prospect["fit_score"]) < int(settings.get("fit_threshold", 75)):
        errors.append("prospect no longer meets the fit threshold")
    if prospect["confidence"] in {"none", "low"}:
        errors.append("low-confidence site audit requires browser verification")
    if PRODUCT_VENDOR_RE.search(prospect["company"] or ""):
        errors.append("prospect appears to sell a competing CRM, inbox, or helpdesk product")
    problem = address_problem(email)
    if problem:
        errors.append(problem)
    if not contact.get("is_published"):
        errors.append("address is not backed by visible publication evidence")
    if not contact.get("published_url") or not contact.get("evidence_excerpt"):
        errors.append("contact publication evidence is incomplete")
    if not contact.get("mx_available"):
        errors.append("recipient mail domain has no MX")
    domain = prospect["registrable_domain"]
    if not published_address_matches_business(domain, email, prospect["company"]):
        errors.append("published address belongs to an unrelated custom domain")
    if db.is_suppressed("domain", domain) or db.is_suppressed("email", email):
        errors.append("recipient or domain is suppressed")
    cutoff = iso(utcnow() - timedelta(days=int(settings.get("domain_cooldown_days", 90))))
    prior = db.row(
        "SELECT 1 FROM messages m JOIN prospects p ON p.id=m.prospect_id "
        "WHERE p.registrable_domain=? AND m.channel='email' AND m.kind='initial' AND m.id!=? "
        "AND m.status IN ('scheduled','sent','replied') AND COALESCE(m.sent_at,m.scheduled_for,m.created_at)>=? "
        "LIMIT 1", (domain, exclude_message_id, cutoff),
    )
    if prior:
        errors.append("domain has received initial outreach within 90 days")
    return errors


def choose_contact(rows: list[dict[str, Any]], prospect_domain: str = "") -> dict[str, Any] | None:
    eligible = []
    for row in rows:
        problem = address_problem(row.get("normalized_email") or row.get("email") or "")
        if problem or not row.get("is_published") or not row.get("mx_available"):
            continue
        email = row.get("normalized_email") or row.get("email")
        local = email.split("@", 1)[0]
        role = row.get("kind") == "role"
        email_domain = registrable_domain(email.rsplit("@", 1)[-1])
        target_domain = registrable_domain(prospect_domain)
        domain_rank = 0 if target_domain and email_domain == target_domain else (
            2 if is_freemail(email) else 1)
        # Exact company domain first, related brand domain second, freemail last.
        rank = (domain_rank, 1 if role else 0, len(local), email)
        eligible.append((rank, row))
    return min(eligible, default=(None, None))[1]
