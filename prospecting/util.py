"""Pure helpers used by policy, scheduling, and deduplication."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

try:
    from publicsuffix2 import get_sld
except ImportError:  # A conservative fallback keeps the CLI usable before setup.
    get_sld = None

FREE_MAIL_DOMAINS = {
    "aol.com", "gmail.com", "hotmail.com", "icloud.com", "live.com",
    "outlook.com", "proton.me", "protonmail.com", "rediffmail.com",
    "yahoo.co.in", "yahoo.com", "ymail.com",
}
MULTIPART_SUFFIXES = {
    "co.in", "co.uk", "com.au", "com.br", "com.sg", "com.tr", "com.mx",
    "co.nz", "co.za", "org.uk", "net.au", "org.au", "ae.org",
}
EMAIL_RE = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,63}$", re.I)


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(UTC).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def normalize_domain(value: str) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    if not value:
        return ""
    if "://" not in value:
        value = "//" + value
    try:
        host = (urlsplit(value).hostname or "").rstrip(".")
    except (TypeError, ValueError):
        return ""
    if host.startswith("www."):
        host = host[4:]
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def registrable_domain(value: str) -> str:
    domain = normalize_domain(value)
    if not domain:
        return ""
    if get_sld:
        result = get_sld(domain, strict=False)
        if result:
            return result.lower()
    labels = domain.split(".")
    if len(labels) <= 2:
        return domain
    suffix2 = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if suffix2 in MULTIPART_SUFFIXES else suffix2


def normalize_email(value: str) -> str:
    raw = (value or "").strip()
    # Percent-encoded control/whitespace bytes belong to a URL, not an SMTP
    # mailbox. Contact-page extraction decodes valid mailto hrefs first; other
    # callers fail closed instead of treating "%20info" as a deliverable local part.
    if re.search(r"%(?:0[09ad]|20)", raw, re.I):
        return ""
    address = parseaddr(raw)[1].lower()
    if "@" not in address:
        return ""
    local, domain = address.rsplit("@", 1)
    domain = normalize_domain(domain)
    normalized = f"{local}@{domain}" if domain else ""
    return normalized if EMAIL_RE.match(normalized) else ""


def is_freemail(email: str) -> bool:
    normalized = normalize_email(email)
    return bool(normalized and normalized.rsplit("@", 1)[1] in FREE_MAIL_DOMAINS)


def normalize_linkedin(value: str) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if not value:
        return ""
    if value.startswith("http://"):
        value = "https://" + value[7:]
    if value.startswith("www."):
        value = "https://" + value
    return value.lower()


def content_hash(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compliance_email_body(body: str, settings: dict[str, Any]) -> str:
    address = str(settings.get("business_postal_address") or "").strip()
    legal = str(settings.get("business_legal_name") or "wrrk.ai").strip()
    footer = ["This is a business development message from wrrk.ai.", "", legal]
    if address:
        footer.append(address)
    footer.extend(["", "If this is not relevant, reply opt out and I will not contact you again."])
    return body.rstrip() + "\n\n" + "\n".join(footer) + "\n"


def delivery_content_hash(channel: str, kind: str, to_address: str, subject: str,
                          body: str, evidence_ids: list[int],
                          settings: dict[str, Any]) -> str:
    """Hash the exact user-controlled delivery content frozen by approval."""
    if channel != "email":
        return content_hash(channel, kind, to_address, subject, body, evidence_ids)
    final_body = compliance_email_body(body, settings)
    identity = {
        "sender_name": str(settings.get("sender_name") or "Wachas"),
        "sender_email": str(settings.get("sender_email") or "wachas@wrrk.ai"),
    }
    return content_hash(channel, kind, to_address, subject, final_body, evidence_ids, identity)


def text_hash(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def evidence_excerpt(value: str, limit: int = 420) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def mx_available(domain_or_email: str) -> bool:
    domain = domain_or_email.rsplit("@", 1)[-1]
    domain = normalize_domain(domain)
    if not domain:
        return False
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=10)
        return bool(list(answers))
    except ImportError:
        pass
    except Exception:
        return False
    try:
        result = subprocess.run(["dig", "+short", "MX", domain], capture_output=True,
                                text=True, timeout=15, check=False)
        return bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def market_timezone(market: str, policies: dict[str, Any]) -> ZoneInfo:
    policy = policies.get((market or "").upper()) or {}
    return ZoneInfo(policy.get("timezone", "UTC"))


def in_business_window(moment: datetime, market: str, policies: dict[str, Any],
                       start_hour: int = 9, end_hour: int = 17) -> bool:
    local = moment.astimezone(market_timezone(market, policies))
    return local.weekday() < 5 and start_hour <= local.hour < end_hour


def next_business_window(moment: datetime, market: str, policies: dict[str, Any],
                         start_hour: int = 9, end_hour: int = 17) -> datetime:
    """Return the same instant if valid, otherwise the next local weekday opening."""
    tz = market_timezone(market, policies)
    local = moment.astimezone(tz)
    if local.weekday() < 5 and start_hour <= local.hour < end_hour:
        return moment.astimezone(UTC)
    if local.weekday() < 5 and local.hour < start_hour:
        candidate = datetime.combine(local.date(), time(start_hour), tzinfo=tz)
    else:
        candidate = datetime.combine(local.date() + timedelta(days=1), time(start_hour), tzinfo=tz)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def local_day(moment: datetime, market: str, policies: dict[str, Any]) -> date:
    return moment.astimezone(market_timezone(market, policies)).date()
