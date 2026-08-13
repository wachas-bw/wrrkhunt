# wrrkhunt

Evidence-first prospecting for **wrrk.ai**. Finds companies that demonstrably have the
problem we solve, and proves it to them with evidence they can verify on their own
website in ten seconds.

> **Private repo. It contains real contact data for ~90 people at ~30 companies.**
> Do not make it public and do not share outside the team. See [Data and privacy](#data-and-privacy).

**Start here:** [`wrrk-prospecting-playbook.pdf`](wrrk-prospecting-playbook.pdf), 7 pages,
the whole method. Read that before touching the code.

---

## The thesis

Every competitor emails "we are an AI CRM." Nobody replies, because that claim is about
the seller. We only send claims about the buyer:

> **A small team whose customers talk to them on WhatsApp and Instagram, where those
> conversations are invisible to the business.**

### The scoring inversion

The instinct is to target companies paying a lot for tooling. That is backwards. The best
prospect has **many customer front doors and nothing behind them**: WhatsApp, Instagram,
email, a contact form, and no CRM or shared inbox. The pain is unowned rather than merely
expensive, and there is nothing to rip out.

Sanity check that this is right: **Decorpot scored 100/100 and is already a wrrk
customer.** The scorer independently rediscovered the profile we already sell to. That is
also why `data/suppression.json` exists.

---

## Quick start

```bash
# 1. Audit any list of domains (free, no API keys)
python3 sources/stack_detect.py --seats 8 acme.com other.in

# 2. Find decision-makers and addresses
python3 enrich/find_contacts.py --from-stacks

# 3. Draft. Nothing ships that fails the copy linter.
python3 outreach/draft.py --min-fit 50

# 4. Create Gmail drafts (or just write .eml files with --dry-run)
python3 outreach/gmail_drafts.py --dry-run
```

Python 3.9+. **No dependencies**, standard library only.

---

## Sourcing methods, by measured yield

Every number here was counted, not estimated.

| Method | Best for | Cost | Yield |
|---|---|---|---|
| **Meta Ad Library** | WhatsApp-first SMBs | Free | **17 of 27** |
| Published directories | Agencies, 30 to 70 staff | Free | 15 of 19 |
| Funding reports | Early-stage startups | Free | 5 of 7 |
| Apify LinkedIn posts | Founder intent | $0.42 | **0 of 180** |

### The Meta Ad Library trick

Searching a vertical alone returns the wrong companies: large brands driving to websites,
sorted by ad spend. **Pair the literal token `"whatsapp"` with the vertical.**

```
q=skincare                     ->  1 / 84 WhatsApp CTAs
q="whatsapp" interior design   -> 17 / 27
```

Recipe and extractor: [`sources/meta_ads_extract.js`](sources/meta_ads_extract.js).
It runs inside a browser because the Ad Library returns 403 to plain HTTP clients.

### The one that failed

Apify LinkedIn post search returned 180 posts and **zero** usable leads. LinkedIn post
search is dominated by engagement-optimised content and the genuine-ask base rate is ~1%.
That is a base-rate problem, not a tuning problem. Full breakdown in
`data/pool3_startups.json` under `_apify_result`. **Do not repeat it.**

---

## Layout

```
sources/
  stack_detect.py       audits a domain: ~55 vendors, front-door count, per-module fit
  meta_ads_extract.js   Meta Ad Library harvester (paste into a browser)
  intent_linkedin.py    Apify LinkedIn harvester (built, measured, low yield)
  intent_config.json
enrich/
  find_contacts.py      decision-maker + address finder, never invents an address
outreach/
  draft.py              angle picker + renderer + the copy linter
  gmail_drafts.py       IMAP APPEND to Gmail Drafts, or .eml files
  batch1.md .. batch3.md
data/
  stacks.json           audit output
  contacts.json         addresses and candidate names
  tracker.csv           one row per prospect, segmented market x pool x angle
  suppression.json      NEVER prospect these (existing customers, our own domains)
  pool1_ctwa.json       click-to-WhatsApp advertisers
  pool2_agencies.json   Pune agencies, 30-70 staff
  pool3_startups.json   recently funded early-stage
```

---

## The rules, and why each exists

The linter in `draft.py` **refuses to emit** anything breaking these. Judgement does not
scale, a check does. Each rule is a failure someone already paid for.

1. **No em dashes, en dashes, middle dots.** Reads as AI-generated.
2. **60 to 95 words of pitch.** Measured on the body, excluding greeting and signature.
3. **No links in email one.** Hurts deliverability on a cold first touch.
4. **A question mark.** A binary ask beats a soft "let me know."
5. **Never greet by a scraped name.** Derive the greeting from the address you are
   sending to. Fall back to "Hi there" whenever it is not clearly a person.
6. **Never write to `careers@` or `hr@`.** A vendor pitch to a recruiting inbox is ignored.
7. **Never send to a domain with no MX.** It bounces, and bounces cost sender reputation.

Real mistakes the linter caught before they went out: **"Hi Careers,"** on a mail
addressed to `sales@`, **"Hi Acaddcntr,"** (a company mailbox), **"Hi Anilbhat,"** (first
and last name run together), and drafts addressed to `no-reply@`.

### Never claim what you cannot show

Three claims were written during development and deleted before sending:

| Written | Why it was cut |
|---|---|
| "Saw the same shape at three other interiors firms" | Could not back it. Replaced with "Decorpot runs on us", a public logo. |
| "We run the interiors side for Decorpot" | Decorpot is a customer, but we do not know which modules they use. |
| "...are *probably* four different tools" | "Probably" is a guess wearing a fact's clothes. Rewritten as a question. |

### Do not sell these

Verified half-built in the `new/` repo. An email that overpromises and then dies in the
demo is worse than no email.

- **AI meeting notetaker** is disabled in production, and is feature #02 on our landing page
- **Lead discovery across six platforms** defaults to Reddit only
- **Ziwo voice** is code-ready, blocked on credentials
- **Public REST API**, **Meta/Google Ads orchestration**: do not exist

Also never quote the marketing "replaces $400/mo" figure, or `stack_usd_mo` from the
detector. Both are estimates. **Name the tools, not the number.**

---

## Sending is the bottleneck

An earlier campaign automated sourcing and drafting perfectly and left sending as a vague
intention. **Twelve warm, fully drafted leads went cold unsent over four days.**

- **20 emails/day, hard cap**, spread out. A 35-in-two-minutes burst already hurt
  deliverability once.
- Plain text, 1:1. No HTML, no tracking pixel.
- Set up **Google Postmaster Tools** and seed-test into Gmail/Outlook/Yahoo before the
  first real send.
- Follow up **3, 7, 7**, same thread. Day three is the audit video, the highest-converting
  asset and the one most likely to be skipped.
- **If drafted-but-unsent exceeds 10, stop sourcing and send.**

Full detail: [`SEND-RUNBOOK.md`](SEND-RUNBOOK.md). Live state: [`TODAY.md`](TODAY.md).

---

## Data and privacy

This repo holds third-party personal data: roughly 90 email addresses, named individuals,
and LinkedIn profiles, all collected from public sources.

- **Keep the repo private.** Do not fork it to a public account.
- Honour opt-outs immediately and add the domain to `data/suppression.json`.
- Do not commit API tokens. `.gitignore` covers the known ones; check before you push.
- Generated `.eml` files are gitignored, since they are rebuildable from `batch*.json`
  and duplicate personal data unnecessarily.
- Cold email carries legal obligations that differ by market: CAN-SPAM in the US needs a
  postal address and a working opt-out, and the EU needs a documented legitimate-interest
  basis. **Skip Germany and Austria**, which are effectively opt-in.

---

## Where to start on Monday

1. Pick one vertical where customers plausibly use WhatsApp.
2. Search the Ad Library for `"whatsapp" <vertical>` in your country.
3. Pull the destination URLs. Those are your audit targets.
4. Run `stack_detect.py`. Keep the ones with many front doors and nothing behind them.
5. Write one sentence of evidence per prospect. If you cannot, you do not have a prospect.
6. Send 20 a day, and diarise the day-three follow-up **before** you send the first one.
