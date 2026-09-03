#!/usr/bin/env python3
"""Decision-maker + email finder for wrrk.ai prospects.

Order matters, and it is deliberate: cheapest and most reliable evidence first.

  1. published inboxes  — mailto: on the site (already ground truth, no guessing)
  2. team/about pages   — candidate names for human review, never email synthesis
  3. MX sanity check    — proves each visibly published address domain can receive mail

It deliberately does NOT invent addresses from a name alone. An unverified guess that
bounces costs sender reputation, and reputation is the scarce resource here: see
new/docs/WRRK_AI_WARMUP_RUNBOOK.md for what that already cost this domain once.

Usage:
    python3 wrrkhunt/enrich/find_contacts.py montdorinterior.com musedesign.ae
    python3 wrrkhunt/enrich/find_contacts.py --from-stacks      # every audited domain
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STACKS = os.path.join(ROOT, "data", "stacks.json")
OUT = os.path.join(ROOT, "data", "contacts.json")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from prospecting.phones import extract_phone_contacts

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PATHS = ["", "/about", "/about-us", "/team", "/our-team", "/contact", "/contact-us",
         "/leadership", "/management"]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "rediffmail.com",
             "icloud.com", "protonmail.com", "aol.com", "live.com", "ymail.com"}

# A capitalised human name sitting next to a decision-maker title.
# \b matters: without it "coo" matched inside product copy and invented people such as
# "Ultima Memory Foam (Coo)" on sleepycat.in.
TITLES = (r"\b(?:founder|co-?founder|ceo|managing director|director|owner|proprietor|"
          r"partner|principal|head of (?:sales|marketing|growth|operations)|"
          r"chief executive|cmo|coo|business head)\b")
NAME = r"[A-Z][a-z]+(?:\s+[A-Z][a-z'.]+){1,2}"
NAME_THEN_TITLE = re.compile(rf"({NAME})\s*[,\-|–—:]{{0,3}}\s*(?:is\s+)?(?:the\s+)?({TITLES})", re.I)
TITLE_THEN_NAME = re.compile(rf"({TITLES})\s*[,\-|–—:]{{0,3}}\s*({NAME})", re.I)

# Prose words that the capitalised-name pattern happily swallows. Without this filter
# the extractor produced "At our (Partner)", "is affected by (Owner)" and
# "Floor Office Project (Ceo)" from ordinary marketing copy. A wrong name in a cold
# email is worse than no name, so any candidate containing one of these is discarded.
STOPWORDS = {
    "a", "about", "affected", "all", "an", "and", "any", "are", "as", "at", "based", "be",
    "been", "best", "book", "build", "building", "business", "by", "call", "can", "centre",
    "center", "clients", "come", "company", "consultation", "contact", "create", "custom",
    "design", "designs", "do", "dream", "each", "every", "expert", "experts", "first",
    "floor", "for", "free", "from", "get", "give", "global", "has", "have", "help", "here",
    "home", "homes", "how", "in", "india", "interior", "interiors", "is", "it", "its",
    "just", "know", "let", "like", "make", "management", "many", "medical", "more", "most",
    "much", "need", "new", "no", "not", "now", "of", "office", "on", "one", "only", "or",
    "other", "our", "out", "over", "own", "policy", "privacy", "project", "projects",
    "quality", "quote", "read", "rights", "same", "see", "service", "services", "should",
    "site", "solutions", "some", "study", "such", "team", "terms", "than", "that", "the",
    "their", "them", "there", "these", "they", "this", "those", "through", "time", "to",
    "top", "trusted", "universities", "us", "use", "very", "we", "well", "what", "when",
    "where", "which", "who", "why", "will", "with", "work", "works", "would", "you",
    "your", "yours",
}


def _looks_like_person(name: str) -> bool:
    parts = name.split()
    if not 2 <= len(parts) <= 3:
        return False
    return all(p.strip(".'").lower() not in STOPWORDS and len(p.strip(".'")) >= 2
               for p in parts)


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        # Keep normal certificate and hostname verification enabled.
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.read(900_000).decode("utf-8", "replace")
    except Exception:
        return ""


def _strip(html: str) -> str:
    html = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _mx(domain: str) -> bool:
    try:
        r = subprocess.run(["dig", "+short", "MX", domain],
                           capture_output=True, text=True, timeout=20)
        return bool(r.stdout.strip())
    except Exception:
        return False


def _fetch_all(base: str) -> list[tuple[str, str]]:
    """Fetch the candidate pages one at a time.

    Deliberately sequential. An inner ThreadPoolExecutor here nested inside main()'s
    pool put ~36 sockets plus dig subprocesses in flight at once, and the resulting
    timeouts were swallowed by the bare excepts. That silently reported mx=n and zero
    emails for montdorinterior.com, a domain that in fact publishes five addresses and
    has Google + Hostinger MX. Parallelism belongs at one level only, in main().
    """
    pages = []
    for path in PATHS:
        url = base + path
        body = _fetch(url)
        if body:
            pages.append((url, body))
    return pages


def _published_emails(url: str, html: str) -> list[dict]:
    """Extract only addresses the business visibly publishes on its own page.

    Script/style payloads are excluded. A mailto link is treated as visible contact
    evidence even when its anchor text says only "Email us".
    """
    visible = _strip(html)
    public_markup = re.sub(
        r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    mailtos = re.findall(
        r"<a\b[^>]*\bhref\s*=\s*(?:[\"']\s*)?mailto:([^?\"'<>\s]+)",
        public_markup, re.I,
    )
    candidates = set(EMAIL_RE.findall(visible)) | set(mailtos)
    out = []
    for candidate in sorted(candidates):
        # Some CMS editors accidentally leave URL-encoded whitespace in a
        # mailto href (for example, mailto:%20info@example.com). Decode the
        # href value before validation; never pass the encoded bytes to SMTP.
        email = urllib.parse.unquote(candidate).strip().lower()
        if not EMAIL_RE.fullmatch(email):
            continue
        if email.endswith((".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp")):
            continue
        if "sentry" in email or "example." in email or "@2x" in email:
            continue
        at = visible.lower().find(email)
        if at >= 0:
            excerpt = visible[max(0, at - 90):at + len(email) + 100]
        else:
            excerpt = f"The business publishes a mailto contact for {email}."
        out.append({"email": email, "source_url": url,
                    "excerpt": re.sub(r"\s+", " ", excerpt).strip()[:320]})
    return out


def _people(text: str) -> list[dict]:
    """Candidate decision-makers. Deliberately conservative and still only CANDIDATES:
    nothing here is safe to paste into a greeting without a human confirming it against
    LinkedIn or the site. draft.py treats an unverified name as absent."""
    found, seen = [], set()
    for rx, flip in ((NAME_THEN_TITLE, False), (TITLE_THEN_NAME, True)):
        for m in rx.finditer(text):
            name = (m.group(2) if flip else m.group(1)).strip()
            title = (m.group(1) if flip else m.group(2)).strip().title()
            if not _looks_like_person(name) or name.lower() in seen:
                continue
            seen.add(name.lower())
            found.append({"name": name, "title": title, "verified": False})
    return found[:6]


def _pattern(emails: list[str], domain: str) -> str:
    """Infer the org's address pattern from addresses it already publishes.
    Only reported when the site itself demonstrates it."""
    for e in emails:
        local = e.split("@")[0]
        if "." in local and local.count(".") == 1:
            a, b = local.split(".")
            if a.isalpha() and b.isalpha() and len(a) > 1 and len(b) > 1:
                return "first.last@" + domain
    for e in emails:
        local = e.split("@")[0]
        if local.isalpha() and local not in ("info", "hello", "contact", "sales",
                                             "admin", "support", "enquiry", "office"):
            return "first@" + domain
    return ""


def enrich(domain: str, region: str | None = None) -> dict:
    domain = re.sub(r"^https?://|/.*$", "", domain.strip().lower())
    rec = {"domain": domain, "emails": [], "role_emails": [], "personal_emails": [],
           "people": [], "pattern": "", "mx": False, "mx_by_domain": {},
           "free_mail_business": False, "email_evidence": [], "phone_contacts": [],
           "detected_at": datetime.now(UTC).replace(microsecond=0).isoformat()}

    base = ""
    for b in (f"https://{domain}", f"https://www.{domain}", f"http://{domain}"):
        if _fetch(b):
            base = b
            break
    if not base:
        return rec

    pages = _fetch_all(base)
    rec["phone_contacts"] = extract_phone_contacts(pages, region=region)
    html = "\n".join(body for _, body in pages)
    text = _strip(html)

    evidence_by_email = {}
    for url, body in pages:
        for item in _published_emails(url, body):
            evidence_by_email.setdefault(item["email"], item)
    emails = sorted(evidence_by_email)
    own = [e for e in emails if e.split("@")[-1].endswith(domain.replace("www.", ""))]
    free = [e for e in emails if e.split("@")[-1] in FREE_MAIL]

    generic = ("info", "hello", "contact", "sales", "admin", "support", "enquiry",
               "enquiries", "office", "care", "help", "team", "marketing")
    rec["emails"] = emails[:12]
    rec["email_evidence"] = [evidence_by_email[e] for e in rec["emails"]]
    rec["role_emails"] = [e for e in own if e.split("@")[0] in generic]
    rec["personal_emails"] = [e for e in own if e.split("@")[0] not in generic]
    rec["free_mail_business"] = bool(free and not own)
    rec["people"] = _people(text)
    rec["pattern"] = _pattern(own, domain)
    mail_domains = {e.rsplit("@", 1)[1] for e in emails} | {domain}
    rec["mx_by_domain"] = {mail_domain: _mx(mail_domain) for mail_domain in sorted(mail_domains)}
    rec["mx"] = rec["mx_by_domain"].get(domain, False)
    return rec


def main(domains: list[str]) -> None:
    out = []
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(enrich, d): d for d in domains}
        for f in cf.as_completed(futs):
            try:
                out.append(f.result())
            except Exception as e:
                out.append({"domain": futs[f], "error": str(e)})

    existing = {}
    if os.path.exists(OUT):
        try:
            existing = {r["domain"]: r for r in json.load(open(OUT))}
        except Exception:
            pass
    for r in out:
        existing[r["domain"]] = r
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(list(existing.values()), open(OUT, "w"), indent=1, ensure_ascii=False)

    for r in sorted(out, key=lambda x: x["domain"]):
        if r.get("error"):
            print(f"{r['domain']:<32} ERROR {r['error'][:40]}")
            continue
        who = "; ".join(f"{p['name']} ({p['title']})" for p in r["people"][:2]) or "-"
        best = (r["personal_emails"] or r["role_emails"] or r["emails"] or ["-"])[0]
        flag = " [free-mail business]" if r["free_mail_business"] else ""
        print(f"{r['domain']:<32} mx={'y' if r['mx'] else 'n'} {best:<34} {who}{flag}")
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--from-stacks" in args:
        doms = [r["domain"] for r in json.load(open(STACKS)) if r.get("reachable")]
    else:
        doms = [a for a in args if not a.startswith("--")]
    if not doms:
        sys.exit(__doc__)
    main(doms)
