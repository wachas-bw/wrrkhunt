#!/usr/bin/env python3
"""Stack detector — audit check #1 and Pool 3 sourcing for wrrk.ai outreach.

Fetches a company's public pages and reports which customer-facing tools they run:
CRM, chat widget, WhatsApp BSP, booking, email marketing, ads pixels. From that it
derives the two numbers every cold email needs:

  channel_count   how many separate front doors a customer can knock on
  stack_usd_mo    what they are plausibly paying for those tools each month

Pure stdlib, no API keys, no cost. Detection reads script tags and links in the
served HTML, which is where every one of these vendors injects itself.

Usage:
    python3 wrrkhunt/sources/stack_detect.py acme.com other.in
    python3 wrrkhunt/sources/stack_detect.py --file domains.txt
    python3 wrrkhunt/sources/stack_detect.py --file domains.txt --seats 6
    python3 wrrkhunt/sources/stack_detect.py --json-only acme.com

Writes wrrkhunt/data/stacks.json (merged, keyed by domain) and prints a table.

Limitation: sites that inject widgets only after hydration (some SPAs) can read as
clean. `confidence` is "low" when we saw a JS-heavy shell with no vendor hits, and
those domains should be re-checked in a real browser before being quoted in an email.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "data", "stacks.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PATHS = ["", "/contact", "/contact-us", "/about", "/pricing"]
TIMEOUT = 12

# ── vendor signatures ─────────────────────────────────────────────────────────
# (label, category, [regex fragments], usd_per_month, per_seat)
# Prices are public list prices for the entry paid tier, deliberately conservative.
# per_seat=True means the figure is multiplied by the team size when we know it.
SIGNATURES: list[tuple[str, str, list[str], float, bool]] = [
    # CRM / marketing automation
    ("HubSpot",        "crm",     [r"js\.hs-scripts\.com", r"js\.hs-analytics\.net",
                                   r"js\.hsforms\.net", r"hs-banner\.com"],        20, True),
    ("Salesforce/Pardot", "crm",  [r"pardot\.com", r"pi\.pardot", r"salesforce\.com/embeddedservice"], 25, True),
    ("Zoho",           "crm",     [r"salesiq\.zoho", r"zohopublic", r"crm\.zoho",
                                   r"zohostatic\.com"],                             10, True),
    ("Freshworks",     "crm",     [r"wchat\.freshchat\.com", r"freshsales",
                                   r"widget\.freshworks\.com"],                     19, True),
    ("Pipedrive",      "crm",     [r"pipedrive\.com", r"pipedriveassets"],          19, True),
    ("Zendesk",        "support", [r"static\.zdassets\.com", r"zendesk\.com/embeddable",
                                   r"zdassets"],                                    19, True),
    ("Gorgias",        "support", [r"gorgias\.chat", r"gorgias\.com"],              60, False),

    # live chat widgets
    ("Intercom",       "chat",    [r"widget\.intercom\.io", r"js\.intercomcdn\.com"], 39, True),
    ("Crisp",          "chat",    [r"client\.crisp\.chat"],                         25, False),
    ("Tawk.to",        "chat",    [r"embed\.tawk\.to"],                              0, False),
    ("Tidio",          "chat",    [r"code\.tidio\.co"],                             29, False),
    ("Drift",          "chat",    [r"js\.driftt\.com", r"drift\.com"],              50, False),
    ("LiveChat",       "chat",    [r"cdn\.livechatinc\.com"],                       24, True),
    ("Chatwoot",       "chat",    [r"chatwoot"],                                     0, False),
    ("Chatra",         "chat",    [r"call\.chatra\.io"],                            21, False),

    # WhatsApp BSPs — the Pool 3 trigger
    ("WATI",           "wa_bsp",  [r"wati\.io", r"app\.wati\.io"],                  23, False),
    ("AiSensy",        "wa_bsp",  [r"aisensy\.com", r"aisensy"],                    17, False),
    ("Interakt",       "wa_bsp",  [r"interakt\.ai", r"interakt\.shop"],             20, False),
    ("DoubleTick",     "wa_bsp",  [r"doubletick\.io"],                              20, False),
    ("Gallabox",       "wa_bsp",  [r"gallabox\.com"],                               25, False),
    ("Zoko",           "wa_bsp",  [r"zoko\.io"],                                    34, False),
    ("BusinessOnBot",  "wa_bsp",  [r"businessonbot"],                               30, False),
    ("LimeChat",       "wa_bsp",  [r"limechat\.ai"],                                50, False),
    ("Verloop",        "wa_bsp",  [r"verloop\.io"],                                 40, False),
    ("Yellow.ai",      "wa_bsp",  [r"yellow\.ai", r"yellowmessenger"],              50, False),
    ("Haptik",         "wa_bsp",  [r"haptik\.ai", r"haptikapi"],                    50, False),
    ("Respond.io",     "wa_bsp",  [r"respond\.io"],                                 29, False),

    # booking
    ("Calendly",       "booking", [r"calendly\.com"],                               12, True),
    ("Cal.com",        "booking", [r"cal\.com/"],                                   12, True),

    # ── batch 2 categories ───────────────────────────────────────────────────
    # These map to the modules beyond WhatsApp: tasks, people/HR, tools, CRM.
    # Only vendors that actually leave a trace on a public website are listed.
    # Internal-only tools (Slack, Jira) are deliberately absent: we cannot see them,
    # and guessing at them would put an unverifiable claim in a cold email.

    # applicant tracking. The single highest-value batch 2 signal, because a careers
    # page on an ATS is public, countable, and proves headcount growth.
    ("Greenhouse",     "ats",     [r"boards\.greenhouse\.io", r"job-boards\.greenhouse"], 0, False),
    ("Lever",          "ats",     [r"jobs\.lever\.co"],                              0, False),
    ("Ashby",          "ats",     [r"jobs\.ashbyhq\.com"],                           0, False),
    ("Workable",       "ats",     [r"apply\.workable\.com"],                        129, False),
    ("Zoho Recruit",   "ats",     [r"zohorecruit\.com"],                             25, False),
    ("Freshteam",      "ats",     [r"freshteam\.com"],                               60, False),
    ("Recruitee",      "ats",     [r"recruitee\.com"],                              109, False),
    ("SmartRecruiters", "ats",    [r"smartrecruiters\.com"],                          0, False),
    ("Keka Hire",      "ats",     [r"keka\.com/careers", r"kekahire"],               0, False),

    # HR / payroll. India-weighted, matching where wrrk actually sells.
    ("Keka",           "hr",      [r"\bkeka\.com"],                                  90, False),
    ("Darwinbox",      "hr",      [r"darwinbox\.(com|in)"],                         150, False),
    ("greytHR",        "hr",      [r"greythr\.com"],                                 50, False),
    ("Zoho People",    "hr",      [r"people\.zoho"],                                 40, False),
    ("BambooHR",       "hr",      [r"bamboohr\.com"],                    	         99, False),
    ("Deel",           "hr",      [r"\bdeel\.com"],                                  49, False),
    ("Rippling",       "hr",      [r"rippling\.com"],                                80, False),

    # invoicing / accounting
    ("Zoho Books",     "finance", [r"books\.zoho", r"zohobooks"],                    30, False),
    ("QuickBooks",     "finance", [r"quickbooks\.intuit", r"qbo\.intuit"],           30, False),
    ("Xero",           "finance", [r"\bxero\.com"],                                  47, False),
    ("FreshBooks",     "finance", [r"freshbooks\.com"],                              30, False),
    ("Razorpay",       "finance", [r"razorpay\.com", r"checkout\.razorpay"],          0, False),
    ("Stripe",         "finance", [r"js\.stripe\.com", r"checkout\.stripe"],          0, False),

    # e-signature / contracts
    ("DocuSign",       "esign",   [r"docusign\.(net|com)"],                          25, True),
    ("Dropbox Sign",   "esign",   [r"hellosign\.com", r"dropboxsign"],               20, True),
    ("Zoho Sign",      "esign",   [r"sign\.zoho"],                                   12, True),
    ("Leegality",      "esign",   [r"leegality\.com"],                               25, False),

    # project / task / docs, where publicly visible
    ("Notion",         "project", [r"notion\.so", r"notion\.site"],                  10, True),
    ("Trello",         "project", [r"trello\.com"],                                   5, True),
    ("Asana",          "project", [r"\basana\.com"],                                 11, True),
    ("ClickUp",        "project", [r"clickup\.com"],                                  7, True),
    ("Monday.com",     "project", [r"monday\.com"],                                   9, True),
    ("Confluence",     "project", [r"atlassian\.net/wiki", r"confluence"],            6, True),

    # forms / surveys
    ("Typeform",       "forms",   [r"typeform\.com"],                                25, False),
    ("Jotform",        "forms",   [r"jotform\.com"],                                 34, False),
    ("Google Forms",   "forms",   [r"docs\.google\.com/forms"],                       0, False),

    # email marketing / engagement
    ("Klaviyo",        "email",   [r"static\.klaviyo\.com", r"klaviyo\.com"],       45, False),
    ("Mailchimp",      "email",   [r"chimpstatic\.com", r"list-manage\.com"],       20, False),
    ("Brevo/Sendinblue", "email", [r"sibautomation\.com", r"brevo\.com"],           25, False),
    ("CleverTap",      "email",   [r"clevertap\.com", r"wzrkt\.com"],               75, False),
    ("WebEngage",      "email",   [r"webengage\.com", r"ssl\.widgets\.webengage"],  75, False),
    ("MoEngage",       "email",   [r"moengage\.com", r"cdn\.moengage"],             75, False),
    ("Judge.me",       "email",   [r"judge\.me", r"cdn\.judge\.me"],                15, False),

    # ads + analytics (no cost attributed, but they prove paid acquisition)
    ("Meta Pixel",     "ads",     [r"connect\.facebook\.net", r"fbq\("],             0, False),
    ("Google Ads",     "ads",     [r"googleadservices\.com", r"gtag/js\?id=AW-"],    0, False),
    ("Google Analytics", "analytics", [r"gtag/js\?id=G-", r"googletagmanager\.com"], 0, False),
    ("Hotjar",         "analytics", [r"static\.hotjar\.com"],                        0, False),
    ("MS Clarity",     "analytics", [r"clarity\.ms"],                                0, False),

    # platform (context, not cost)
    ("Shopify",        "platform", [r"cdn\.shopify\.com", r"myshopify\.com"],        0, False),
    ("WooCommerce",    "platform", [r"woocommerce"],                                 0, False),
    ("Wix",            "platform", [r"static\.parastorage\.com", r"wix\.com"],       0, False),
    ("Squarespace",    "platform", [r"squarespace\.com"],                            0, False),
    ("Webflow",        "platform", [r"webflow\.com", r"assets\.website-files\.com"], 0, False),
    ("Framer",         "platform", [r"framerusercontent\.com"],                      0, False),
    # wp-content alone false-positives on any site embedding a single image from a WP
    # blog (it fired on sleepycat.in, a Shopify store). Require a real WP fingerprint.
    ("WordPress",      "platform", [r"wp-includes/js/", r"/wp-json/",
                                    r"generator[\"'][^>]*WordPress"],                0, False),
]

# customer-reachable channels (the "front doors" count)
WA_RE = re.compile(r"(?:wa\.me/|api\.whatsapp\.com/send|web\.whatsapp\.com/send|whatsapp://send)"
                   r"[^\"'\s<>]*", re.I)
WA_NUM_RE = re.compile(r"(?:wa\.me/|phone=)\+?(\d{8,15})")
IG_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})", re.I)
FB_RE = re.compile(r"facebook\.com/([A-Za-z0-9_.\-]{2,60})", re.I)
LI_RE = re.compile(r"linkedin\.com/company/([A-Za-z0-9_.\-]{2,60})", re.I)
X_RE = re.compile(r"(?:twitter|x)\.com/([A-Za-z0-9_]{2,20})", re.I)
MAILTO_RE = re.compile(r"mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})", re.I)
TEL_RE = re.compile(r"tel:\+?([0-9\-\s()]{7,20})")
FORM_RE = re.compile(r"<form[^>]*>", re.I)

SOCIAL_JUNK = {"sharer", "share", "intent", "profile.php", "plugins", "tr", "dialog",
               "home", "login", "p", "explore", "reel", "reels", "hashtag", "policies",
               "help", "privacy", "sharearticle"}


def _norm_domain(raw: str) -> str:
    d = raw.strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0].split("?")[0]
    return d.strip().strip(".")


def _fetch(url: str) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            return r.read(1_500_000).decode("utf-8", "replace")
    except Exception:
        return ""


def _gather_html(domain: str) -> tuple[str, str]:
    """Return (combined_html, resolved_base). Tries https then http, www then bare."""
    bases = [f"https://{domain}", f"https://www.{domain}",
             f"http://{domain}", f"http://www.{domain}"]
    base = ""
    home = ""
    for b in bases:
        home = _fetch(b)
        if home:
            base = b
            break
    if not home:
        return "", ""
    blobs = [home]
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_fetch, base + p): p for p in PATHS[1:]}
        for f in cf.as_completed(futs):
            try:
                blobs.append(f.result())
            except Exception:
                pass
    return "\n".join(blobs), base


def _clean_socials(matches: list[str]) -> list[str]:
    out = []
    for m in matches:
        h = m.strip("/").lower()
        if h in SOCIAL_JUNK or len(h) < 2:
            continue
        if h not in out:
            out.append(h)
    return out[:3]


def detect(domain: str, seats: int = 5) -> dict[str, Any]:
    domain = _norm_domain(domain)
    html, base = _gather_html(domain)
    rec: dict[str, Any] = {
        "domain": domain, "reachable": bool(html), "resolved": base,
        "tools": [], "wa_bsp": [], "channels": {}, "channel_count": 0,
        "stack_usd_mo": 0, "runs_ads": False, "confidence": "none", "hook": "",
    }
    if not html:
        return rec

    found: list[dict[str, Any]] = []
    for label, cat, pats, price, per_seat in SIGNATURES:
        if any(re.search(p, html, re.I) for p in pats):
            cost = round(price * seats) if per_seat else price
            found.append({"name": label, "category": cat,
                          "usd_mo": cost, "per_seat": per_seat})
    rec["tools"] = found
    rec["wa_bsp"] = [t["name"] for t in found if t["category"] == "wa_bsp"]
    rec["runs_ads"] = any(t["category"] == "ads" for t in found)
    rec["stack_usd_mo"] = sum(t["usd_mo"] for t in found)

    wa_links = WA_RE.findall(html)
    wa_nums = sorted(set(WA_NUM_RE.findall(html)))
    emails = sorted({e.lower() for e in MAILTO_RE.findall(html)
                     if not e.lower().endswith((".png", ".jpg", ".svg"))})
    ch = {
        "whatsapp": bool(wa_links) or bool(rec["wa_bsp"]),
        "whatsapp_numbers": wa_nums[:3],
        "instagram": _clean_socials(IG_RE.findall(html)),
        "facebook": _clean_socials(FB_RE.findall(html)),
        "linkedin": _clean_socials(LI_RE.findall(html)),
        "twitter": _clean_socials(X_RE.findall(html)),
        "emails": emails[:5],
        "phone": bool(TEL_RE.search(html)),
        "contact_form": bool(FORM_RE.search(html)),
        "live_chat": any(t["category"] in ("chat", "support") for t in found),
    }
    rec["channels"] = ch
    rec["channel_count"] = sum([
        ch["whatsapp"], bool(ch["instagram"]), bool(ch["emails"]),
        ch["phone"], ch["contact_form"], ch["live_chat"],
    ])

    paid = [t for t in found if t["category"] not in ("analytics", "platform", "ads")]
    if paid:
        rec["confidence"] = "high"
    elif len(html) > 40_000 and re.search(r"__NEXT_DATA__|react|hydrate", html, re.I):
        rec["confidence"] = "low"   # JS shell, widgets may load post-hydration
    else:
        rec["confidence"] = "medium"

    rec["has_inbox"] = any(t["category"] in ("crm", "chat", "support", "wa_bsp")
                           for t in found)
    rec["fit"], rec["fit_why"] = _fit(rec)
    rec["module_fit"], rec["gaps"] = _module_fit(rec)
    rec["best_module"] = max(rec["module_fit"], key=rec["module_fit"].get)
    rec["hook"] = _hook(rec)
    return rec


def _module_fit(rec: dict[str, Any]) -> tuple[dict[str, int], list[str]]:
    """Score each wrrk module separately, and name the gap that drives each score.

    Batch 1 only asked "is WhatsApp a mess". That misses companies whose pain is a
    hiring spike, client-project admin, or a shared inbox nobody owns. Scoring per
    module lets one prospect list serve every angle, and lets draft.py open with the
    thing that actually hurts rather than the thing we happen to sell hardest.
    """
    cats = {t["category"] for t in rec["tools"]}
    names = {t["name"] for t in rec["tools"]}
    ch = rec["channels"]
    m: dict[str, int] = {}
    gaps: list[str] = []

    # unified inbox: many reachable channels, nothing joining them
    doors = rec["channel_count"]
    m["inbox"] = min(doors * 12, 60) + (25 if not rec["has_inbox"] else 0)
    if doors >= 4 and not rec["has_inbox"]:
        gaps.append(f"{doors} channels, no shared inbox")

    # crm: they sell (ads, booking, forms) but nothing stores the pipeline
    m["crm"] = 0
    if "crm" not in cats:
        m["crm"] += 35
        if rec["runs_ads"]:
            m["crm"] += 25
            gaps.append("paying for ads with no CRM behind them")
        if "booking" in cats or "forms" in cats:
            m["crm"] += 20
            gaps.append("taking bookings or form fills with nowhere to put them")

    # people/HR: an ATS or careers page means headcount is moving
    m["hr"] = 0
    if "ats" in cats:
        m["hr"] += 55
        gaps.append(f"hiring through {', '.join(n for n in names if n in _ATS_NAMES)}")
        if "hr" not in cats:
            m["hr"] += 30
            gaps.append("hiring with no HR system for onboarding, leave or payroll")

    # tools: invoicing/contracts/projects scattered across separate vendors
    tool_cats = cats & {"finance", "esign", "project", "forms"}
    m["tools"] = len(tool_cats) * 22
    if len(tool_cats) >= 2:
        gaps.append(f"{len(tool_cats)} separate admin tools ({', '.join(sorted(tool_cats))})")

    # email: several role inboxes and no helpdesk to arbitrate them
    m["email"] = 0
    if ch.get("emails"):
        m["email"] = min(len(ch["emails"]) * 18, 70)
        if "support" not in cats and len(ch["emails"]) >= 3:
            m["email"] += 25
            gaps.append(f"{len(ch['emails'])} published inboxes, no helpdesk")

    # whatsapp: the batch 1 thesis, kept so one score set covers both batches
    m["whatsapp"] = (45 if ch.get("whatsapp") else 0) + (25 if rec["runs_ads"] and
                     ch.get("whatsapp") else 0) + (20 if rec["wa_bsp"] else 0)

    return {k: min(v, 100) for k, v in m.items()}, gaps


_ATS_NAMES = {"Greenhouse", "Lever", "Ashby", "Workable", "Zoho Recruit", "Freshteam",
              "Recruitee", "SmartRecruiters", "Keka Hire"}


def _fit(rec: dict[str, Any]) -> tuple[int, str]:
    """0-100 fit for wrrk.ai. The thesis: many customer front doors, nothing behind
    them. A brand with WhatsApp + Instagram + email + a form and no CRM or shared
    inbox is a better prospect than one already paying for HubSpot, because the pain
    is unowned rather than merely expensive."""
    ch, score, why = rec["channels"], 0, []

    doors = rec["channel_count"]
    score += min(doors, 6) * 10
    if doors >= 4:
        why.append(f"{doors} front doors")

    if ch.get("whatsapp"):
        score += 15
        why.append("WhatsApp is a live channel")
    if ch.get("instagram") and ch.get("whatsapp"):
        score += 10
        why.append("WhatsApp + Instagram both open")

    if not rec.get("has_inbox"):
        score += 20
        why.append("no CRM or shared inbox detected")
    if rec["wa_bsp"]:
        score += 15
        why.append(f"pays for {rec['wa_bsp'][0]} (WhatsApp only, no CRM behind it)")
    if rec["runs_ads"]:
        score += 10
        why.append("running paid ads into those channels")

    return min(score, 100), "; ".join(why)


def _hook(rec: dict[str, Any]) -> str:
    """One plain sentence of evidence, safe to paste into a cold email."""
    ch = rec["channels"]
    named = [t["name"] for t in rec["tools"]
             if t["category"] in ("crm", "chat", "support", "wa_bsp", "booking", "email")]
    doors = []
    if ch.get("whatsapp"):
        n = ch.get("whatsapp_numbers") or []
        doors.append(f"WhatsApp ({'wa.me/' + n[0] if n else 'wa.me link'})")
    if ch.get("instagram"):
        doors.append("Instagram DMs")
    if ch.get("live_chat"):
        chat = [t["name"] for t in rec["tools"] if t["category"] in ("chat", "support")]
        doors.append(f"{chat[0]} on the site" if chat else "a chat widget")
    if ch.get("emails"):
        doors.append(ch["emails"][0])
    if ch.get("contact_form"):
        doors.append("a contact form")
    if not doors:
        return ""
    lead = ", ".join(doors[:3])
    tail = f" You are running {', '.join(named[:3])}." if named else ""
    return f"{lead}.{tail}"


# ── I/O ───────────────────────────────────────────────────────────────────────

def _merge_save(records: list[dict]) -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    existing = {}
    if os.path.exists(OUT):
        try:
            existing = {r["domain"]: r for r in json.load(open(OUT))}
        except Exception:
            existing = {}
    for r in records:
        existing[r["domain"]] = r
    json.dump(list(existing.values()), open(OUT, "w"), indent=1, ensure_ascii=False)


def _print_table(records: list[dict]) -> None:
    print(f"\n{'domain':<30} {'fit':>4} {'doors':>6} {'$/mo':>6} {'conf':<7} tools")
    print("-" * 104)
    for r in sorted(records, key=lambda x: -x.get("fit", 0)):
        if not r["reachable"]:
            print(f"{r['domain']:<30} {'-':>4} {'-':>6} {'-':>6} unreachable")
            continue
        tools = ", ".join(t["name"] for t in r["tools"]
                          if t["category"] not in ("analytics", "platform"))
        tools = tools[:50] or "none detected"
        print(f"{r['domain']:<30} {r.get('fit', 0):>4} {r['channel_count']:>6} "
              f"{r['stack_usd_mo']:>6} {r['confidence']:<7} {tools}")
    bsp = [r for r in records if r.get("wa_bsp")]
    if bsp:
        print(f"\nPool 3 (WhatsApp BSP detected) — {len(bsp)}:")
        for r in bsp:
            print(f"  {r['domain']}: {', '.join(r['wa_bsp'])}")


def run(domains: list[str], seats: int = 5, json_only: bool = False) -> list[dict]:
    domains = [_norm_domain(d) for d in domains if d.strip()]
    records = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(detect, d, seats): d for d in domains}
        for f in cf.as_completed(futs):
            d = futs[f]
            try:
                records.append(f.result())
            except Exception as e:
                records.append({"domain": d, "reachable": False, "error": str(e),
                                "tools": [], "wa_bsp": [], "channels": {},
                                "channel_count": 0, "stack_usd_mo": 0,
                                "confidence": "none", "hook": ""})
            if not json_only:
                print(f"  checked {d}", file=sys.stderr)
    _merge_save(records)
    if json_only:
        print(json.dumps(records, indent=1, ensure_ascii=False))
    else:
        _print_table(records)
        print(f"\nWrote {OUT}")
    return records


if __name__ == "__main__":
    args = sys.argv[1:]
    seats, json_only, doms = 5, False, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--file":
            i += 1
            doms += [l.strip() for l in open(args[i]) if l.strip() and not l.startswith("#")]
        elif a == "--seats":
            i += 1
            seats = int(args[i])
        elif a == "--json-only":
            json_only = True
        else:
            doms.append(a)
        i += 1
    if not doms:
        sys.exit(__doc__)
    run(doms, seats, json_only)
