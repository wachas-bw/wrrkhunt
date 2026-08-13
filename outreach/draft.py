#!/usr/bin/env python3
"""Batch drafter for wrrk.ai cold outreach.

Turns audited prospects (data/stacks.json + data/contacts.json + data/pool1_ctwa.json)
into email, LinkedIn and X drafts, and REFUSES to emit anything that breaks the copy
rules. The rules are not stylistic preferences, each one is a failure we already paid
for somewhere:

  no em/en dash, no middle dot   reads as AI-generated, kills reply rate
  60-90 words                    longer gets skimmed and deleted
  no URLs in email #1            links in a cold first touch hurt deliverability,
                                 and wrrk.ai's domain reputation is already fragile
                                 (new/docs/WRRK_AI_WARMUP_RUNBOOK.md)
  a question mark                a binary ask beats a soft "let me know"
  no unverified personal name    find_contacts.py returns CANDIDATES; greeting a
                                 prospect by a name scraped out of marketing copy is
                                 unrecoverable, so we fall back to a safe opener

Angle is chosen from what the audit actually found, never assigned by hand.

Usage:
    python3 wrrkhunt/outreach/draft.py                 # draft everything eligible
    python3 wrrkhunt/outreach/draft.py --min-fit 80    # only the strongest
    python3 wrrkhunt/outreach/draft.py --check         # lint existing drafts, write nothing
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
SENDER = "Wachas"
SENDER_TITLE = "Founding engineer, wrrk.ai"

BANNED_CHARS = {"—": "em dash", "–": "en dash", "·": "middle dot",
                "•": "bullet", "‘": "smart quote", "’": "smart quote",
                "“": "smart quote", "”": "smart quote"}
URL_RE = re.compile(r"https?://|www\.|\.com/|\.ai/|\.in/")
MIN_WORDS, MAX_WORDS = 60, 95


# ── helpers ───────────────────────────────────────────────────────────────────

def _load(name: str):
    p = os.path.join(DATA, name)
    return json.load(open(p)) if os.path.exists(p) else []


def _suppressed() -> set[str]:
    s = _load("suppression.json")
    if not isinstance(s, dict):
        return set()
    out = set()
    for k in ("customers", "own", "vendors_not_prospects"):
        out |= {v.lower() for v in s.get(k, [])}
    return out


def _wa_display(num: str) -> str:
    return f"wa.me/{num}" if num else "your wa.me link"


# Addresses that exist to send, not receive. Mail here reaches nobody and a reply is
# impossible by construction, so drafting to one is worse than having no address.
UNREPLYABLE = re.compile(r"^(no[._-]?reply|donotreply|do[._-]?not[._-]?reply|"
                         r"bounce|mailer[-_]daemon|postmaster|abuse|webmaster)", re.I)


# Real inboxes, but the wrong humans. careers@ and hr@ reach a recruiter, who has no
# budget and no reason to forward a vendor pitch. Deprioritised rather than excluded,
# since for some prospects they are the only address that exists.
WRONG_AUDIENCE = re.compile(r"^(careers?|jobs?|hr|recruit|recruitment|hiring|"
                            r"resume|cv|internship)s?$", re.I)


def _best_address(c: dict) -> str:
    """Best address a decision-maker would actually read.

    Priority: a named individual, then a general business inbox, then anything at all.
    """
    # Rank across ALL pools at once, not within each. find_contacts classifies any
    # non-generic local part as "personal", so careers@ landed in the top pool and beat
    # sales@ on skovian.com. Wrong-audience must outrank pool order.
    candidates: list[tuple[int, int, str]] = []
    for pool_rank, pool in enumerate((c.get("personal_emails") or [],
                                      c.get("role_emails") or [],
                                      c.get("emails") or [])):
        for e in pool:
            local = e.split("@")[0]
            if UNREPLYABLE.match(local):
                continue
            candidates.append((1 if WRONG_AUDIENCE.match(local) else 0, pool_rank, e))
    return min(candidates)[2] if candidates else "UNKNOWN"


# Locals that are a function, not a person. Greeting one of these by "name" produces
# "Hi Careers," which is how a mail merge announces itself.
NOT_A_PERSON = {"info", "hello", "sales", "admin", "contact", "office", "team", "care",
                "careers", "career", "hr", "jobs", "job", "support", "help", "enquiry",
                "enquiries", "business", "marketing", "people", "bde", "accounts",
                "billing", "service", "services", "mail", "email", "hi", "reach"}


def _greeting(to_addr: str) -> str:
    """Derive the greeting from the SAME address we are sending to.

    It previously read personal_emails[0], which on skovian.com was careers@ while the
    chosen recipient was sales@. That produced "Hi Careers," addressed to sales. The
    greeting and the envelope must never come from different places.
    """
    if not to_addr or "@" not in to_addr:
        return "Hi there,"
    local, domain = to_addr.split("@", 1)
    root = domain.split(".")[0].lower()

    # A local that echoes the domain is a company mailbox, not a person. Without this
    # acaddcntr@acaddcentre.com greeted the reader as "Hi Acaddcntr,".
    bare = re.sub(r"[^a-z]", "", local.lower())
    if bare and (bare in root or root in bare or _similar(bare, root)):
        return "Hi there,"

    # A separator gives a clean first name (prajapathi.m -> Prajapathi). Without one,
    # anything long is probably first+last run together (anilbhat), and "Hi Anilbhat,"
    # reads as a mail merge. Better to greet nobody than to greet them wrong.
    if any(sep in local for sep in "._-"):
        first = re.split(r"[._-]", local)[0]
    elif len(local) <= 7:
        first = local
    else:
        return "Hi there,"

    if first.isalpha() and 2 < len(first) <= 12 and first.lower() not in NOT_A_PERSON:
        return "Hi " + first.capitalize() + ","
    return "Hi there,"


def _similar(a: str, b: str) -> bool:
    """True when one string is a plausible abbreviation of the other, e.g.
    'acaddcntr' vs 'acaddcentre' (vowels dropped)."""
    devowel = lambda x: re.sub(r"[aeiou]", "", x)
    return len(a) > 4 and len(b) > 4 and devowel(a) == devowel(b)


# ── angle selection ───────────────────────────────────────────────────────────

def pick_angle(stack: dict, contact: dict, vertical: str = "", meta: dict | None = None) -> str:
    meta = meta or {}
    """Choose the angle from evidence. Order is by how undeniable the proof is.

    Batch 2 added the agency branch. An agency is the only prospect that needs the
    product twice, internally for staff and commercially for client work, so leading
    with "you have several inboxes" undersells it badly."""
    # Funding-news evidence outranks site evidence for pool 3: a pre-launch site is
    # thin by construction, and "you launch in two weeks" beats any stack observation.
    if meta.get("launch"):
        return "prelaunch"
    if meta.get("raised") and meta.get("raised") != "undisclosed":
        return "just_funded"
    if contact.get("free_mail_business"):
        return "freemail"
    if len(contact.get("role_emails", []) + contact.get("personal_emails", [])) >= 4:
        return "many_inboxes"
    if stack.get("wa_bsp"):
        return "bsp"

    mf = stack.get("module_fit") or {}
    # A published careers@/support@/sales@ set with no helpdesk is a sharper, more
    # specific observation than a generic channel count, so it outranks it.
    if mf.get("email", 0) >= 75:
        return "email_volume"
    if mf.get("crm", 0) >= 60 and stack.get("runs_ads"):
        return "crm_gap"

    if stack.get("has_inbox") and stack.get("stack_usd_mo", 0) > 0:
        return "partial_stack"
    if stack.get("runs_ads") and stack["channels"].get("whatsapp"):
        return "ctwa"
    if vertical == "agency":
        return "agency_ops"
    return "many_doors"


# ── body builders, one per angle ──────────────────────────────────────────────

def _body(angle: str, s: dict, c: dict, company: str, meta: dict | None = None) -> str:
    meta = meta or {}
    ch = s.get("channels", {})
    wa = _wa_display((ch.get("whatsapp_numbers") or [""])[0])
    ig = (ch.get("instagram") or [""])[0]
    # WhatsApp-led angles keep the specific build claim; the rest lead with the role,
    # because "I built the WhatsApp side" is a non-sequitur when the hook is hiring.
    if angle in ("ctwa", "bsp", "many_inboxes"):
        intro = ("I am a founding engineer at wrrk.ai and I built the WhatsApp side, "
                 "so I notice this stuff.")
    else:
        intro = "I am a founding engineer at wrrk.ai, so I notice this stuff."

    if angle == "many_inboxes":
        boxes = [e.split("@")[0] for e in
                 (c.get("role_emails", []) + c.get("personal_emails", []))][:5]
        listed = ", ".join(boxes[:-1]) + " and " + boxes[-1]
        return (f"{intro} You are running ads into {wa}, and the site lists "
                f"{len(boxes)} separate inboxes: {listed}. Nothing sits behind them, so "
                f"someone who messages WhatsApp on Sunday and emails a branch on Monday "
                f"looks like two different people to you. That is the part I would fix "
                f"first, and it is a config change, not a project.")

    if angle == "freemail":
        addr = (c.get("emails") or ["a Gmail address"])[0]
        return (f"{intro} Your enquiries land in {addr}. That works until two people "
                f"need to answer it, and then there is no way to see who replied, what "
                f"was promised, or which enquiry went cold. You are also on Instagram, "
                f"so the same person can reach you twice and be two records. Fixing that "
                f"is a morning, not a migration.")

    if angle == "bsp":
        bsp = s["wa_bsp"][0]
        return (f"{intro} You are paying for {bsp}, which handles WhatsApp and stops "
                f"there. Your email, your Instagram DMs and your deal history live "
                f"somewhere else, so nobody has the whole conversation in one place. "
                f"We built the version where all of it lands on one contact record. "
                f"Same WhatsApp number, nothing to migrate.")

    if angle == "partial_stack":
        tools = [t["name"] for t in s["tools"]
                 if t["category"] in ("crm", "chat", "support")][:2]
        named = " and ".join(tools) if tools else "a CRM and a chat widget"
        return (f"{intro} You already run {named}, so you have solved part of this. "
                f"What is still loose is {wa} and Instagram, which is where most of the "
                f"actual buying conversation happens and where neither of those tools "
                f"can see. One record per customer across all four is the gap. Worth a "
                f"look at what that changes.")

    if angle == "ctwa":
        return (f"{intro} You have live Meta and Google ads pointing at {wa}. You are "
                f"paying for every one of those leads, and they land in an inbox with no "
                f"owner, no history and no way to tell which ad produced which "
                f"conversation. The ads are the expensive part, so that is a strange "
                f"place to stop measuring.")

    if angle == "prelaunch":
        # Customer list comes from pool metadata, never hardcoded: the first version
        # said "contractors, electricians, plumbers" for every pre-launch prospect,
        # which is true of Fixxly and false of anyone else.
        buyers = meta.get("customers") or "the trades you sell to"
        # "Decorpot runs on us" is exactly what the public logo wall supports. The
        # earlier "we run the interiors side for Decorpot" claimed knowledge of which
        # modules they use, which we do not have.
        return (f"{intro} You launch on {meta.get('launch_h','1 September')} and your "
                f"customers are {buyers}, who all run on WhatsApp. Right now you get to "
                f"decide where those conversations land before there is any history to "
                f"migrate. That is a two-week window and it does not come back. Decorpot "
                f"runs on us, so I have watched this go both ways.")

    if angle == "just_funded":
        return (f"{intro} Saw the {meta.get('raised','seed')} round. The bit nobody warns "
                f"you about is that headcount and enquiry volume both jump before the "
                f"tooling does, so WhatsApp, Instagram and email each become somebody's "
                f"personal problem. Cheaper to fix at your size than at fifty people. "
                f"That is the whole reason we built it as one workspace.")

    if angle == "agency_ops":
        return (f"{intro} Two questions about running {company} rather than the "
                f"campaigns. Are proposal, contract, timesheet and invoice four separate "
                f"tools for you, and is attendance and payroll a fifth? If yes, that is "
                f"the thing I would fix, because we built all of it into one workspace "
                f"alongside the client inbox. I am in Pune too, so this is a local ask.")

    if angle == "email_volume":
        # role_emails alone under-counts: find_contacts files any non-generic local
        # (careers@) as "personal", so the body listed two inboxes while the subject
        # named three. Use both pools.
        boxes = sorted({e.split("@")[0] for e in
                        (c.get("role_emails") or []) + (c.get("personal_emails") or [])})[:4]
        listed = ", ".join(boxes[:-1]) + " and " + boxes[-1] if len(boxes) > 1 else (
            boxes[0] if boxes else "several addresses")
        return (f"{intro} You publish {listed} as separate addresses, with no helpdesk "
                f"behind them. So whoever opens one first owns it, there is no assignment, "
                f"no history, and nobody can tell you what the reply time actually is. "
                f"That gets expensive quietly, because the only evidence is the deal that "
                f"went quiet.")

    if angle == "crm_gap":
        return (f"{intro} You are running paid ads, and I cannot find a CRM anywhere "
                f"behind them. So the money buys a click, the click becomes an enquiry, "
                f"and the enquiry becomes somebody's inbox. There is no pipeline holding "
                f"it, which means no answer to which campaign produced revenue. The ads "
                f"are the expensive half, so that is a strange place to stop.")

    doors = []
    if ch.get("whatsapp"):
        doors.append("WhatsApp")
    if ig:
        doors.append("Instagram")
    if ch.get("emails"):
        doors.append("email")
    if ch.get("contact_form"):
        doors.append("a contact form")
    listed = ", ".join(doors[:-1]) + " and " + doors[-1] if len(doors) > 1 else (
        doors[0] if doors else "several channels")
    return (f"{intro} A customer can reach {company} on {listed}. Each one is a separate "
            f"inbox, so the same person asking the same question twice is two records "
            f"and nobody knows it. That is invisible until you try to work out why a "
            f"deal went quiet. It is the first thing I would put right.")


CTA = "Worth 15 minutes? I can show it with your own numbers in it."


def _email_subject(c: dict) -> str:
    """Name their actual inboxes. A hard-coded "careers@, sales@, support@" was wrong for
    anyone whose set differs, and a wrong detail in the subject is the first thing read."""
    boxes = sorted({e.split("@")[0] for e in
                    (c.get("role_emails") or []) + (c.get("personal_emails") or [])})[:3]
    return ", ".join(f"{b}@" for b in boxes) if boxes else "your shared inboxes"


def build(company: str, s: dict, c: dict, vertical: str = "", meta: dict | None = None) -> dict:
    meta = meta or {}
    angle = pick_angle(s, c, vertical, meta)
    to = _best_address(c)
    body = _body(angle, s, c, company, meta)
    email = f"{_greeting(to)}\n\n{body}\n\n{CTA}\n\n{SENDER}\n{SENDER_TITLE}"

    ch = s.get("channels", {})
    wa = _wa_display((ch.get("whatsapp_numbers") or [""])[0])
    subjects = {
        "many_inboxes": "your branch inboxes",
        "freemail": "where your enquiries land",
        "bsp": f"{s['wa_bsp'][0] if s.get('wa_bsp') else 'your WhatsApp tool'} and the rest of it",
        "partial_stack": "the half you have not connected",
        "ctwa": "your wa.me link",
        "many_doors": f"{company} has four front doors",
        "agency_ops": "running the agency, not the campaigns",
        "email_volume": _email_subject(c),
        "crm_gap": "where do the ad leads go?",
        "prelaunch": "before 1 September",
        "just_funded": "congrats on the round",
    }
    return {
        "company": company, "domain": s["domain"], "to": to,
        "fit": s.get("fit", 0), "angle": angle,
        "subject": subjects.get(angle, "one question"),
        "body": email,
        "linkedin": _linkedin(angle, company, s, c, vertical),
        "evidence": s.get("fit_why", ""),
        "mx": c.get("mx", False),
    }


# Only claims we can actually stand behind. Everything here is a public wrrk.ai logo
# (new/src/components/landing/MarqueeProof.tsx), so a prospect can verify it and the
# demo call will not contradict it. An invented "we see this at three other firms"
# costs more on the call than it buys in the inbox.
PROOF = {
    "interior design": "Decorpot runs their customer side on us",
    "fitout": "Decorpot runs their customer side on us",
    "construction": "Decorpot runs their customer side on us",
    "furniture": "Furniture One runs on us",
}


def _linkedin(angle: str, company: str, s: dict, c: dict, vertical: str = "") -> str:
    """Connect note. LinkedIn caps these at 300 characters."""
    proof = PROOF.get(vertical, "")
    proof_clause = f" {proof}." if proof else ""
    if angle == "many_inboxes":
        note = (f"I am a founding engineer at wrrk.ai. Noticed {company} runs ads into "
                f"WhatsApp with several branch inboxes behind it and nothing joining "
                f"them.{proof_clause} Happy to send what that setup usually looks like "
                f"once it is joined up, no pitch.")
    elif angle == "freemail":
        note = (f"I am a founding engineer at wrrk.ai. Noticed {company} takes enquiries "
                f"into a personal mailbox alongside Instagram. That gets painful the moment "
                f"two people answer.{proof_clause} Happy to send what usually fixes it, no pitch.")
    elif angle == "bsp":
        note = (f"I am a founding engineer at wrrk.ai. Saw you run {s['wa_bsp'][0]}. Curious "
                f"how you handle email and Instagram alongside it, since that is the seam "
                f"most teams tell us hurts.{proof_clause} Happy to compare notes.")
    elif angle == "agency_ops":
        note = (f"I am a founding engineer at wrrk.ai, also in Pune. Curious how {company} "
                f"handles the unglamorous half: proposals, contracts, timesheets, invoices, "
                f"attendance. We built those into one workspace with the client inbox. "
                f"Happy to compare notes, no pitch.")
    else:
        note = (f"I am a founding engineer at wrrk.ai. Noticed {company} takes customers on "
                f"WhatsApp, Instagram and email, with a separate inbox for "
                f"each.{proof_clause} Happy to send what we usually do about that, no pitch.")
    return note[:299]


# ── the linter: nothing ships without passing ────────────────────────────────

def lint(d: dict) -> list[str]:
    errs = []
    body = d["body"]
    for ch, name in BANNED_CHARS.items():
        if ch in body or ch in d["subject"] or ch in d["linkedin"]:
            errs.append(f"contains {name} ({ch!r})")
    # Count the pitch, not the letterhead. The greeting and the signature block are not
    # prose the reader parses, and counting them made a founding-engineer sig look like
    # bloated copy. Strip both, then measure what actually has to earn the reply.
    prose = body
    if "\n\n" in prose:
        prose = prose.split("\n\n", 1)[1]                      # drop greeting
    prose = prose.rsplit(f"\n\n{SENDER}", 1)[0]                # drop signature
    words = len(re.findall(r"\b[\w']+\b", prose))
    if not MIN_WORDS <= words <= MAX_WORDS:
        errs.append(f"{words} words of pitch, outside {MIN_WORDS}-{MAX_WORDS}")
    if URL_RE.search(body.replace("wrrk.ai", "").replace("wa.me/", "")):
        errs.append("contains a URL")
    if "?" not in body:
        errs.append("no question, so no binary ask")
    if len(d["linkedin"]) > 300:
        errs.append(f"linkedin note {len(d['linkedin'])} chars, over 300")
    if d["to"] == "UNKNOWN":
        errs.append("no deliverable address")
    if not d["mx"]:
        errs.append("domain has no MX, mail would bounce")
    return errs


def main(min_fit: int, check_only: bool) -> None:
    stacks = {r["domain"]: r for r in _load("stacks.json") if r.get("reachable")}
    contacts = {r["domain"]: r for r in _load("contacts.json")}
    pool1 = {}
    for fname in ("pool1_ctwa.json", "pool2_agencies.json", "pool3_startups.json"):
        blob = _load(fname)
        if isinstance(blob, dict):
            for a in blob.get("advertisers", []):
                if a.get("domain"):
                    pool1[a["domain"]] = a
    supp = _suppressed()

    drafts, skipped = [], []
    for dom, s in stacks.items():
        name = pool1.get(dom, {}).get("name") or dom.split(".")[0].title()
        # Strip our own descriptive parentheticals, e.g. "Invi Edutech (MBBS in
        # Vietnam)". Those are harvest labels, not what the company calls itself,
        # and addressing someone by a label we invented reads as a mail merge.
        name = re.sub(r"\s*\([^)]*\)", "", name).strip()
        if any(x in dom.lower() or x in name.lower() for x in supp):
            skipped.append((dom, "suppressed (customer/vendor/own)"))
            continue
        # stacks.json accumulates every domain ever audited, including the ones used to
        # validate the detector (mamaearth.in, sleepycat.in). Those are not prospects,
        # and mamaearth is a listed company nowhere near the ICP. Only draft to domains
        # that came out of an actual sourcing pool.
        if dom not in pool1:
            skipped.append((dom, "not in a sourcing pool (test/validation domain)"))
            continue
        if s.get("fit", 0) < min_fit:
            skipped.append((dom, f"fit {s.get('fit', 0)} below {min_fit}"))
            continue
        vert = pool1.get(dom, {}).get("vertical", "")
        pm = pool1.get(dom, {})
        meta = {"launch": pm.get("launch"), "raised": pm.get("raised"),
                "launch_h": "1 September", "customers": pm.get("customers")}
        d = build(name, s, contacts.get(dom, {}), vert, meta)
        d["market"] = pool1.get(dom, {}).get("market", "")
        d["vertical"] = vert
        d["errors"] = lint(d)
        drafts.append(d)

    drafts.sort(key=lambda x: (bool(x["errors"]), -x["fit"]))
    ok = [d for d in drafts if not d["errors"]]
    bad = [d for d in drafts if d["errors"]]

    if not check_only:
        json.dump(drafts, open(os.path.join(HERE, "batch1.json"), "w"),
                  indent=1, ensure_ascii=False)
        _write_md(ok, bad)

    print(f"{len(ok)} clean, {len(bad)} blocked, {len(skipped)} skipped\n")
    for d in ok:
        print(f"  PASS  fit={d['fit']:<4} {d['angle']:<14} {d['to']}")
    for d in bad:
        print(f"  BLOCK fit={d['fit']:<4} {d['angle']:<14} {d['domain']}: {'; '.join(d['errors'])}")
    if skipped:
        print()
        for dom, why in skipped:
            print(f"  skip  {dom}: {why}")


def _write_md(ok: list[dict], bad: list[dict]) -> None:
    lines = ["# Batch 1 drafts", "",
             "Generated by draft.py. Every draft below passed the copy linter: no em dashes,",
             "60-95 words, no URLs, a binary ask, a deliverable address, and a domain with MX.",
             "", "Send 1:1 from wachas@wrrk.ai, plain text, 20/day maximum, spread through the",
             "day. Do not paste these into a bulk tool.", ""]
    for d in ok:
        lines += [f"## {d['company']}  ({d['domain']})", "",
                  f"- **To:** {d['to']}",
                  f"- **Fit:** {d['fit']} | **Angle:** {d['angle']} | "
                  f"**Market:** {d['market'] or 'n/a'} | **Vertical:** {d['vertical'] or 'n/a'}",
                  f"- **Evidence:** {d['evidence']}", "",
                  f"**Subject:** {d['subject']}", "", "```", d["body"], "```", "",
                  f"**LinkedIn connect note** ({len(d['linkedin'])} chars):", "",
                  "```", d["linkedin"], "```", ""]
    if bad:
        lines += ["---", "", "## Blocked, do not send", ""]
        for d in bad:
            lines.append(f"- **{d['domain']}** ({d['angle']}): {'; '.join(d['errors'])}")
    open(os.path.join(HERE, "batch1.md"), "w").write("\n".join(lines))


if __name__ == "__main__":
    a = sys.argv[1:]
    mf = int(a[a.index("--min-fit") + 1]) if "--min-fit" in a else 50
    main(mf, "--check" in a)
