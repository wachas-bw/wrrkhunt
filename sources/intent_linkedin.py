#!/usr/bin/env python3
"""Batch 3 harvester — early-stage founders on LinkedIn, with profile URLs.

Forked from clienthunt/sources/intent_linkedin.py, which is proven working code, and
retuned for wrrk.ai. Uses Apify actor harvestapi/linkedin-post-search: cookieless, no
LinkedIn account, no browser, zero ban risk.

WHY THIS POOL. Batches 1 and 2 were sourced from ads and directories, so LinkedIn
profiles had to be guessed afterwards and most came back unverified. This actor returns
the post author's name and profile URL as first-class fields, so every lead is
messageable the moment it lands. It also reaches the segment the other sources cannot:
a two-person startup has no ad spend and no directory listing, but its founder posts.

TWO BUCKETS
  ask    a founder explicitly asking which CRM / inbox / WhatsApp tool to use.
         Rare, high value, reply the same day.
  early  a founder whose post proves the company is young. No incumbent stack means
         zero switching cost, which is the whole reason an early team will try a new
         workspace when an established one will not.

Usage:
    python3 wrrkhunt/sources/intent_linkedin.py --dry-run    # print queries, no spend
    python3 wrrkhunt/sources/intent_linkedin.py --ask        # ask queries only (~$0.19)
    python3 wrrkhunt/sources/intent_linkedin.py --early      # early queries only (~$0.14)
    python3 wrrkhunt/sources/intent_linkedin.py              # both (~$0.33)
    python3 wrrkhunt/sources/intent_linkedin.py --refilter   # re-tune on cache, FREE

--refilter re-runs the classifier over the saved raw pull at no cost. Tune the config
against it before spending again; the FREE plan is $5/month total.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(ROOT)
CFG = json.load(open(os.path.join(HERE, "intent_config.json")))
AP = CFG["apify"]
OUT = os.path.join(ROOT, "data", "pool3_founders.csv")
RAW = os.path.join(ROOT, "data", "pool3_raw.json")


def _token() -> str:
    tf = os.path.join(REPO, AP["token_file"])
    if os.path.exists(tf):
        return open(tf).read().strip()
    t = os.environ.get("APIFY_TOKEN", "").strip()
    if t:
        return t
    sys.exit(f"ERROR: no Apify token at {tf} and APIFY_TOKEN unset.")


def _run_actor(queries: list[str]) -> list[dict]:
    actor = AP["actor"].replace("/", "~")
    url = (f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
           f"?token={_token()}&timeout=300")
    payload = {
        "searchQueries": queries,
        "maxPosts": AP["max_posts_per_query"],
        "postedLimit": AP.get("posted_limit", "month"),
        "sortBy": AP.get("sort_by", "date"),
        "scrapePages": 1,
        "scrapeComments": False,
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=340) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code == 402:
            sys.exit("ERROR 402: Apify credit exhausted for this cycle.\n"
                     "The FREE plan resets monthly at $5. Either wait for the reset or\n"
                     "upgrade at https://console.apify.com/billing. Nothing was charged.")
        sys.exit(f"ERROR {e.code} from Apify: {body}")


OPS = [k.lower() for k in CFG["ops_keywords"]]
EARLY = [k.lower() for k in CFG["early_stage_tells"]]
BUYER = [k.lower() for k in CFG["buyer_phrases"]]
PROMO = [k.lower() for k in CFG["promo_phrases"]]
VENDOR = [k.lower() for k in CFG["vendor_author_tokens"]]

# Founder-ish titles in the author headline. A post from an employee is not a buyer.
FOUNDER_TELL = ["founder", "co-founder", "cofounder", "ceo", "owner", "director",
                "building", "solopreneur", "entrepreneur", "md "]

# Thought-leadership and engagement bait. These read as buyer intent to a naive keyword
# match but are content, and they are the dominant failure mode on LinkedIn.
CONTENT_TELL = ["here's how", "heres how", "here's why", "heres why", "here is how",
                "ways to", "steps to", "reasons why", "tips to", "lessons learned",
                "follow for", "comment below", "save this", "repost", "in this post",
                "let me explain", "read more", "learn how", "swipe", "thread",
                "i wrote", "new blog", "case study", "we tested", "i tested",
                "i analyzed", "i analysed", "what i learned", "my takeaways",
                "hot take", "unpopular opinion", "agree?", "thoughts?", "day "]


def _classify(text: str, author: str, headline: str = "") -> tuple[str | None, str]:
    """Return (bucket, reason). bucket in {'ask','early',None}."""
    t = " " + text.lower() + " "
    an = (author or "").lower()
    hl = (headline or "").lower()

    if len(text.strip()) < 40:
        return None, "too short to judge"

    # vendors and agencies post about CRM constantly; they are the loudest noise here
    if any(v in an for v in VENDOR):
        return None, "vendor/agency author"
    if any(p in t for p in PROMO):
        return None, "promo / self-marketing"

    is_founder = any(f in hl for f in FOUNDER_TELL) or not hl

    # ASK: explicitly shopping for the category
    if any(k in t for k in OPS) and any(b in t for b in BUYER):
        if any(c in t for c in CONTENT_TELL):
            return None, "content marketing, not a real ask"
        if len(text) > 900:
            return None, "too long, likely content"
        return "ask", "strong ask" if "?" in text else "buyer intent"

    # EARLY: young company, no stack to displace
    if any(e in t for e in EARLY):
        if not is_founder:
            return None, "early-stage tell but author is not a founder"
        if any(c in t for c in CONTENT_TELL) and "?" not in text:
            return None, "content marketing, not a founder update"
        if len(text) > 900:
            return None, "too long, likely content"
        hit = next((e for e in EARLY if e in t), "")
        return "early", f"early stage ({hit})"

    return None, "no ops keyword and no early-stage tell"


def _map(it: dict) -> dict:
    a = it.get("author") or {}
    if not isinstance(a, dict):
        a = {}
    return {
        "author": a.get("name") or a.get("fullName") or "",
        "headline": a.get("headline") or a.get("occupation") or "",
        "author_url": (a.get("linkedinUrl") or a.get("url") or "").split("?")[0],
        "company": (a.get("companyName") or a.get("company") or ""),
        "text": (it.get("content") or it.get("text") or "").replace("\n", " ").strip(),
        "post_url": (it.get("linkedinUrl") or it.get("url") or "").split("?")[0],
    }


def _filter(mapped: list[dict]) -> list[dict]:
    seen, out = set(), []
    for m in mapped:
        key = m["author_url"] or m["post_url"]
        if not key or key in seen:
            continue
        bucket, reason = _classify(m["text"], m["author"], m["headline"])
        if not bucket:
            continue
        seen.add(key)
        out.append({**m, "bucket": bucket, "signal": reason})
    # asks first, then strongest early-stage signals
    out.sort(key=lambda x: (0 if x["bucket"] == "ask" else 1,
                            0 if "strong" in x["signal"] else 1))
    return out


def _write(rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["bucket", "signal", "author", "headline", "company",
                    "linkedin_profile", "post_url", "text"])
        for m in rows:
            w.writerow([m["bucket"], m["signal"], m["author"], m["headline"],
                        m["company"], m["author_url"], m["post_url"], m["text"][:400]])


def _report(rows: list[dict], total: int) -> None:
    asks = [r for r in rows if r["bucket"] == "ask"]
    early = [r for r in rows if r["bucket"] == "early"]
    print(f"\nKept {len(rows)} of {total}: {len(asks)} ASK, {len(early)} EARLY")
    print(f"Wrote {OUT}")
    with_profile = sum(1 for r in rows if r["author_url"])
    print(f"With a LinkedIn profile URL: {with_profile}/{len(rows)}")
    for label, group in (("ASK (reply today)", asks), ("EARLY STAGE", early)):
        if group:
            print(f"\n--- {label} ---")
            for m in group[:10]:
                print(f"  {m['author']} | {m['headline'][:48]}")
                print(f"     {m['author_url']}")
                print(f"     [{m['signal']}] {m['text'][:110]}")


def harvest(which: str) -> None:
    queries = []
    if which in ("both", "ask"):
        queries += CFG["linkedin_queries_ask"]
    if which in ("both", "early"):
        queries += CFG["linkedin_queries_early"]
    est = len(queries) * AP["max_posts_per_query"] * 1.5 / 1000
    print(f"Running {AP['actor']} on {len(queries)} queries "
          f"(<= {AP['max_posts_per_query']} posts each). Estimated cost ~${est:.2f}")
    items = _run_actor(queries)
    posts = [i for i in items if i.get("type", "post") == "post"]
    print(f"  actor returned {len(items)} items ({len(posts)} posts)")
    mapped = [_map(it) for it in posts]
    json.dump(mapped, open(RAW, "w"), indent=1, ensure_ascii=False)
    rows = _filter(mapped)
    _write(rows)
    _report(rows, len(posts))


def refilter() -> None:
    if not os.path.exists(RAW):
        sys.exit(f"No cache at {RAW}. Run a harvest first.")
    mapped = json.load(open(RAW))
    print(f"Re-filtering {len(mapped)} cached posts. No Apify spend.")
    rows = _filter(mapped)
    _write(rows)
    _report(rows, len(mapped))


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--dry-run" in a:
        print("ASK queries:")
        for i, q in enumerate(CFG["linkedin_queries_ask"], 1):
            print(f"  {i}. {q}")
        print("EARLY queries:")
        for i, q in enumerate(CFG["linkedin_queries_early"], 1):
            print(f"  {i}. {q}")
    elif "--refilter" in a:
        refilter()
    elif "--ask" in a:
        harvest("ask")
    elif "--early" in a:
        harvest("early")
    else:
        harvest("both")
