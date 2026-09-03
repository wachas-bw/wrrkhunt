"""Public business-phone extraction and conservative normalization.

The helpers in this module never guess a number and never test a number by calling or
messaging it.  A contact is emitted only when a public page contains an explicit phone,
WhatsApp, JSON-LD, or labelled visible-number signal.
"""
from __future__ import annotations

import html as html_module
import json
import re
import urllib.parse
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Iterable

try:
    import phonenumbers
    from phonenumbers import PhoneNumberFormat, PhoneNumberType
except ImportError:  # Legacy scripts remain usable before the automation venv is set up.
    phonenumbers = None
    PhoneNumberFormat = PhoneNumberType = None


TEL_HREF_RE = re.compile(r"\bhref\s*=\s*['\"]\s*tel:([^'\"<>]+)", re.I)
WHATSAPP_RE = re.compile(
    r"(?:wa\.me/|api\.whatsapp\.com/send\?[^'\"<>]*?phone=|"
    r"web\.whatsapp\.com/send\?[^'\"<>]*?phone=|whatsapp://send\?[^'\"<>]*?phone=)"
    r"(\+?[0-9][0-9\s().-]{6,20}[0-9])",
    re.I,
)
JSON_PHONE_RE = re.compile(
    r"['\"](?:telephone|phone|mobile)['\"]\s*:\s*['\"]([^'\"<>]{7,30})['\"]",
    re.I,
)
LABELLED_PHONE_RE = re.compile(
    r"\b(WhatsApp|Whats App|Mobile|Phone|Telephone|Tel|Call(?: us)?|Sales|Office)"
    r"\s*(?:number|line)?\s*[:|\-–]?\s*"
    r"(\+?[0-9][0-9\s().-]{5,22}[0-9])",
    re.I,
)
TAG_RE = re.compile(r"<[^>]+>")
VENDOR_CREDIT_RE = re.compile(
    r"\b(?:designed|developed|powered|maintained|website)\s+by\b|"
    r"\b(?:web|digital|marketing)\s+agency\b",
    re.I,
)

UAE_LOCATIONS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("Dubai", "Dubai", re.compile(r"\bdubai\b", re.I)),
    ("Abu Dhabi", "Abu Dhabi", re.compile(r"\babu\s+dhabi\b", re.I)),
    ("Al Ain", "Abu Dhabi", re.compile(r"\bal\s+ain\b", re.I)),
    ("Sharjah", "Sharjah", re.compile(r"\bsharjah\b", re.I)),
    ("Ajman", "Ajman", re.compile(r"\bajman\b", re.I)),
    ("Ras Al Khaimah", "Ras Al Khaimah", re.compile(r"\bras\s+al\s+khaimah\b|\brak\b", re.I)),
    ("Fujairah", "Fujairah", re.compile(r"\bfujairah\b", re.I)),
    ("Umm Al Quwain", "Umm Al Quwain", re.compile(r"\bumm\s+al\s+quwain\b|\buaq\b", re.I)),
)

REGION_COUNTRY_CODES = {"AE": "971", "IN": "91", "SG": "65", "GB": "44", "US": "1"}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth and data.strip():
            self.parts.append(data)


def _visible_text(markup: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(markup or "")
        parser.close()
        value = " ".join(parser.parts)
    except Exception:
        value = TAG_RE.sub(" ", markup or "")
    return re.sub(r"\s+", " ", html_module.unescape(value)).strip()


def _excerpt(markup: str, start: int, end: int, limit: int = 320) -> str:
    raw = markup[max(0, start - 130):min(len(markup), end + 170)]
    clean = re.sub(r"\s+", " ", html_module.unescape(TAG_RE.sub(" ", raw))).strip()
    return clean[:limit]


def _placeholder(digits: str) -> bool:
    if len(digits) < 7:
        return True
    national = digits[3:] if digits.startswith("971") else digits
    if len(set(national)) <= 2:
        return True
    if re.search(r"(?:0000000|1111111|1234567|7654321)", national):
        return True
    return national in {"500000000", "501234567", "5012345678", "00000000"}


def _fallback_uae(raw: str) -> dict[str, Any] | None:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00971"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "971" + digits[1:]
    elif not digits.startswith("971"):
        return None
    national = digits[3:]
    if _placeholder(digits) or len(national) not in {8, 9}:
        return None
    if len(national) == 9 and national.startswith("5"):
        number_type = "mobile"
    elif len(national) == 8 and national[:1] in {"2", "3", "4", "6", "7", "9"}:
        number_type = "landline"
    else:
        return None
    e164 = "+" + digits
    return {
        "raw": raw.strip(), "e164": e164, "display": e164,
        "national": national, "region": "AE", "number_type": number_type,
        "extension": "", "valid": True,
    }


def normalize_phone(raw: str, region: str | None = "AE") -> dict[str, Any] | None:
    """Return a normalized, structurally valid public phone or ``None``.

    Local numbers are accepted only when a region is explicitly supplied.  This avoids
    turning an arbitrary digit sequence into an international number by assumption.
    """
    value = urllib.parse.unquote(str(raw or "")).strip()
    if not value or len(value) > 80:
        return None
    value = re.sub(r"^(?:tel:|phone:)", "", value, flags=re.I).strip()
    digits = re.sub(r"\D", "", value)
    if _placeholder(digits):
        return None
    region = (region or "").upper() or None
    expected = REGION_COUNTRY_CODES.get(region or "")
    if expected and digits.startswith(expected) and not value.lstrip().startswith("+"):
        value = "+" + digits
    if phonenumbers is None:
        return _fallback_uae(value) if region == "AE" else None
    try:
        parsed = phonenumbers.parse(value, region)
    except Exception:
        return None
    if not phonenumbers.is_possible_number(parsed) or not phonenumbers.is_valid_number(parsed):
        return None
    if expected and str(parsed.country_code) != expected:
        return None
    number_type = phonenumbers.number_type(parsed)
    type_names = {
        PhoneNumberType.MOBILE: "mobile",
        PhoneNumberType.FIXED_LINE: "landline",
        PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_or_mobile",
        PhoneNumberType.TOLL_FREE: "toll_free",
        PhoneNumberType.PREMIUM_RATE: "premium_rate",
        PhoneNumberType.VOIP: "voip",
    }
    label = type_names.get(number_type, "other")
    if label in {"premium_rate", "other"}:
        return None
    e164 = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
    if _placeholder(re.sub(r"\D", "", e164)):
        return None
    return {
        "raw": str(raw).strip(),
        "e164": e164,
        "display": phonenumbers.format_number(parsed, PhoneNumberFormat.INTERNATIONAL),
        "national": str(parsed.national_number),
        "region": phonenumbers.region_code_for_number(parsed) or region or "",
        "number_type": label,
        "extension": parsed.extension or "",
        "valid": True,
    }


def _contact_type(label: str, normalized_type: str, is_whatsapp: bool) -> str:
    lowered = (label or "").lower()
    if is_whatsapp or "whats" in lowered:
        return "whatsapp"
    if "sales" in lowered:
        return "sales"
    if "office" in lowered:
        return "office"
    if "mobile" in lowered:
        return "business_mobile"
    return normalized_type


def _json_candidates(markup: str) -> Iterable[tuple[str, int, int]]:
    for match in JSON_PHONE_RE.finditer(markup):
        yield match.group(1), match.start(1), match.end(1)
    for script in re.finditer(
        r"<script[^>]+type=['\"]application/ld\+json['\"][^>]*>(.*?)</script>",
        markup, re.I | re.S,
    ):
        try:
            payload = json.loads(html_module.unescape(script.group(1)))
        except (TypeError, ValueError):
            continue

        def walk(value: Any) -> Iterable[str]:
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).lower() in {"telephone", "phone", "mobile"} and isinstance(item, str):
                        yield item
                    else:
                        yield from walk(item)
            elif isinstance(value, list):
                for item in value:
                    yield from walk(item)

        for value in walk(payload):
            yield value, script.start(1), script.end(1)


def extract_phone_contacts(
    pages: Iterable[tuple[str, str]], region: str | None = "AE",
    source_type: str = "company_website",
) -> list[dict[str, Any]]:
    """Extract deduplicated phone evidence from public pages."""
    found: dict[str, dict[str, Any]] = {}
    observed_at = _now()
    for source_url, markup in pages:
        candidates: list[tuple[str, str, bool, int, int, str, str]] = []
        for match in TEL_HREF_RE.finditer(markup):
            candidates.append((match.group(1), "telephone", False, match.start(), match.end(), "high", ""))
        for match in WHATSAPP_RE.finditer(markup):
            candidates.append((match.group(1), "WhatsApp", True, match.start(), match.end(), "high", ""))
        for raw, start, end in _json_candidates(markup):
            candidates.append((raw, "telephone", False, start, end, "medium", ""))
        visible = _visible_text(markup)
        for match in LABELLED_PHONE_RE.finditer(visible):
            visible_excerpt = visible[max(0, match.start() - 120):match.end() + 160].strip()[:320]
            candidates.append((match.group(2), match.group(1), "whats" in match.group(1).lower(),
                               match.start(), match.end(), "medium", visible_excerpt))

        for raw, label, is_whatsapp, start, end, confidence, explicit_excerpt in candidates:
            normalized = normalize_phone(raw, region)
            if not normalized:
                continue
            excerpt = explicit_excerpt or _excerpt(markup, start, end)
            if VENDOR_CREDIT_RE.search(excerpt):
                continue
            e164 = normalized["e164"]
            if re.sub(r"\D", "", e164)[-7:] not in re.sub(r"\D", "", excerpt):
                excerpt = (
                    excerpt.rstrip(" .") + ". " if excerpt else ""
                ) + f"The business page publishes {e164} via its {label} contact."
                excerpt = excerpt[:320]
            item = found.get(e164)
            if item is None:
                item = {
                    **normalized,
                    "contact_type": _contact_type(label, normalized["number_type"], is_whatsapp),
                    "is_whatsapp": bool(is_whatsapp),
                    "business_use": True,
                    "source_type": source_type,
                    "source_url": source_url,
                    "source_urls": [source_url],
                    "evidence_excerpt": excerpt or f"The business publishes {e164} as a contact.",
                    "evidence_excerpts": [excerpt] if excerpt else [],
                    "observed_at": observed_at,
                    "confidence": confidence,
                }
                found[e164] = item
            else:
                item["is_whatsapp"] = bool(item["is_whatsapp"] or is_whatsapp)
                if is_whatsapp:
                    item["contact_type"] = "whatsapp"
                if source_url not in item["source_urls"]:
                    item["source_urls"].append(source_url)
                if excerpt and excerpt not in item["evidence_excerpts"]:
                    item["evidence_excerpts"].append(excerpt)
                if confidence == "high":
                    item["confidence"] = "high"
    for item in found.values():
        item["source_count"] = len(item["source_urls"])
    return sorted(
        found.values(),
        key=lambda item: (
            not item["is_whatsapp"],
            item["contact_type"] not in {"sales", "office", "business_mobile"},
            -int(item["source_count"]),
            item["e164"],
        ),
    )


def extract_uae_locations(pages: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    """Return explicit UAE city/emirate mentions with first-party evidence."""
    found: dict[tuple[str, str], dict[str, str]] = {}
    observed_at = _now()
    for source_url, markup in pages:
        text = _visible_text(markup)
        for city, emirate, pattern in UAE_LOCATIONS:
            match = pattern.search(text)
            if not match:
                continue
            excerpt = text[max(0, match.start() - 110):match.end() + 150].strip()[:320]
            found.setdefault((city, emirate), {
                "city": city, "emirate": emirate, "source_url": source_url,
                "evidence_excerpt": excerpt, "observed_at": observed_at,
            })
    return list(found.values())
