# wrrkhunt — evidence-first prospecting for wrrk.ai

> **Historical architecture snapshot:** this file describes the original script-based
> campaign and contains dated counts and statuses. The canonical current guide is
> [`TEAM-PROSPECTING-GUIDE.md`](TEAM-PROSPECTING-GUIDE.md); current setup and commands are
> in [`AUTOMATION.md`](AUTOMATION.md). Do not infer live state from this file.

## The thesis

Every competitor emails "we are an AI CRM." Nobody replies, because the claim is about
the seller. This system only sends claims about the **buyer**, that the buyer can verify
in ten seconds by looking at their own website.

The wedge, in one sentence:

> **A small team whose customers talk to them on WhatsApp and Instagram, where those
> conversations are invisible to the business.**

Not "AI CRM." The provable version: *your revenue conversations live on someone's
personal phone, and you are buying ads to send more of them there.*

### The scoring inversion that makes this work

The instinct is to target companies paying a lot for tooling. That is backwards. The
best prospect has **many customer front doors and nothing behind them**: WhatsApp,
Instagram, email and a contact form, with no CRM and no shared inbox. The pain is
unowned rather than merely expensive, and there is nothing to rip out.

`stack_detect.py` scores exactly that. Sanity check: **Decorpot scored 100 and is
already a wrrk customer.** The scorer finds the profile wrrk already sells to, which is
why `data/suppression.json` exists.

---

## Pipeline

```
SOURCE                  AUDIT                    ENRICH              DRAFT            SEND
Meta Ad Library    ->   stack_detect.py     ->   find_contacts.py -> draft.py    ->   1:1, 20/day
(CTWA advertisers)      tools, front doors,      decision maker,     angle from       from
                        fit score, hook          address, MX         evidence,        wachas@wrrk.ai
                                                                     linted
```

Each stage writes JSON that the next reads, so any stage can be re-run alone.

---

## The four pools

| # | Pool | Source | Status |
|---|---|---|---|
| 1 | **Click-to-WhatsApp advertisers** | Meta Ad Library, public, no login | **Live. 37 harvested, 13 audited.** |
| 2 | Funded seed/pre-seed, last 90 days | Inc42, Entrackr, YourStory, accelerator cohorts | Not built |
| 3 | WATI / AiSensy / Interakt / DoubleTick users | `stack_detect.py` already detects all of them, needs a domain feed | Detector ready, no feed |
| 4 | Agencies | Apify `harvestapi/linkedin-post-search` | Not built |

Pool 1 first because its intent is the most provable: they are **paying Meta** for leads
that land in an unowned inbox. That is not an inference, it is a receipt.

---

## Files

| Path | What it does |
|---|---|
| `sources/stack_detect.py` | Fetches a prospect's public pages, detects ~45 vendors (CRM, chat, WhatsApp BSP, booking, email, ads, platform), counts customer front doors, estimates real monthly spend, scores fit 0-100, and writes a one-sentence hook safe to paste into an email. Pure stdlib, no keys, free. |
| `sources/meta_ads_extract.js` | Pool 1 harvester. Runs inside Playwright MCP because the Ad Library 403s plain HTTP clients and its async endpoint 404s. Contains the measured query recipe. |
| `enrich/find_contacts.py` | Decision-maker and address finder. Published mailto, then team pages, then pattern derivation **only where the site demonstrates the pattern**. Never invents an address. |
| `outreach/draft.py` | Picks the angle from evidence, renders email plus LinkedIn note, and **refuses to emit** anything breaking the copy rules. |
| `data/suppression.json` | Never-prospect list: existing customers, own domains, and the BSP vendors themselves. |
| `SEND-RUNBOOK.md` | The send process, deliverability constraints, and the do-not-claim list. **Read before sending.** |

---

## How to run it

```bash
# 1. Source Pool 1 (browser, see meta_ads_extract.js for the query recipe)
#    navigate -> evaluate -> append to data/pool1_ctwa.json

# 2. Audit
python3 wrrkhunt/sources/stack_detect.py --seats 8 --file domains.txt

# 3. Enrich
python3 wrrkhunt/enrich/find_contacts.py --from-stacks

# 4. Draft (nothing ships that fails the linter)
python3 wrrkhunt/outreach/draft.py --min-fit 50

# 5. Send by hand, 20/day. See SEND-RUNBOOK.md
```

---

## The five hard rules

1. **Never claim anything you cannot show them.** Every sentence in a draft traces to a
   line in `stacks.json`. The one invented claim that got written during development,
   *"saw the same shape at three other interiors firms this month,"* was removed and
   replaced with Decorpot, a public logo the prospect can verify.
2. **Never sell the half-built.** The do-not-claim table in `SEND-RUNBOOK.md` is binding.
   An engineer-voiced email that overpromises and then dies in the demo is worse than no
   email.
3. **Never guess a name or an address.** A wrong greeting is unrecoverable, a bounce
   costs sender reputation. Both fail closed.
4. **Quote their stack, not our marketing.** Never the "$400/mo" figure. Their detected
   spend is smaller, true, and lands harder.
5. **Sending is the bottleneck, not sourcing.** If drafted-but-unsent exceeds 10, stop
   sourcing.

---

## Measured numbers

Query shape decides everything in Pool 1. Pairing the literal token `"whatsapp"` with a
vertical is the difference between a batch of irrelevant giants and a batch of real
prospects:

| Query | WhatsApp CTA hit rate |
|---|---|
| `IN skincare` | **1 / 84** — big D2C driving to websites, wrong shape entirely |
| `IN "whatsapp" interior design` | **17 / 27** |
| `IN "whatsapp" study abroad consultant` | **15 / 23** |
| `AE "whatsapp" interior fit out` | **20 / 29** |

Batch 1: 9 drafted, 6 clean, 3 blocked by the linter for undeliverable addresses.

---

## Reused rather than rebuilt

- `clienthunt/AUDIT-PLAYBOOK.md`, `outreach-kit.md`, `OFFER-LADDER.md` — the audit-first
  mechanic, which is the documented reason those templates got 10-18% instead of 2%.
- `jobhunt/sources/posts_common.py` — the noise filters, for when Pools 2 and 4 land.
- `twikit-reply-engine/` — the X channel. Already configured with wrrk.ai's profile,
  13 keywords and 13 target accounts. Note `twitter-cli` is broken
  (`ClientTransaction` init failure); the patched `.venv` there is the only working path.

## Known constraints

- **Apify: $2.72 of $5 remaining.** Enough for ~11 harvester runs. Pools 1 and 3 need
  none of it.
- **Email verification is not wired.** `MILLIONVERIFIER_API_KEY` and `BOUNCER_API_KEY`
  are both absent in the `new/` repo, so `verifyEmail()` collapses unknowns to invalid.
  Adding MillionVerifier is the single highest-leverage unblock for scaling this.
- **`stack_detect` reads served HTML.** Widgets injected only after hydration can read as
  clean; those come back `confidence: low` and should be re-checked in a browser before
  being quoted.
