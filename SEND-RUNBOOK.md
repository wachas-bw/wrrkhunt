# Send runbook

This file exists because of one specific failure. `clienthunt/TODAY.md` records 12 warm,
fully-drafted leads that went cold unsent over four days. Sourcing and drafting were
automated; sending stayed a vague intention. **The bottleneck is never finding people.
It is sending.**

So: sending is a scheduled task with a number attached, not a thing you get to when the
drafts look ready.

---

## Before the first send, once

- [ ] **Google Postmaster Tools** for `wrrk.ai` (https://postmaster.google.com), verified
      by DNS TXT. This is the only real signal for how Gmail sees the domain. SES metrics
      cannot tell you this.
- [ ] **Seed test.** Send 3 of the batch-1 drafts, unmodified, to a Gmail, an Outlook and
      a Yahoo address you control. Confirm **Primary inbox, not Promotions, not Spam.**
      If any land in spam, stop and fix before touching a real prospect.
- [ ] **Confirm the signature.** Plain text, no image, no tracking link. Name, one line on
      role, and the wrrk.ai domain as plain text.

### Why this domain needs the care

`wachas@wrrk.ai` is Google Workspace, verified:

```
MX      smtp.google.com
SPF     v=spf1 include:_spf.google.com ~all
DKIM    google._domainkey present
DMARC   v=DMARC1; p=none; adkim=s; aspf=r
```

That is a healthy, correctly-configured mailbox, and it is a **different sending lane**
from the one that got into trouble. The spam problem in `new/docs/WRRK_AI_WARMUP_RUNBOOK.md`
was `noreply@wrrk.ai` sending bulk through **SES**, different IPs entirely. Workspace 1:1
is fine to start now.

But Gmail's reputation signal is partly **domain-level**, so the SES history is not
irrelevant to us. Hence the caps below.

Also worth knowing: SPF currently authorises Google only. Any future SES send from
`wrrk.ai` would fail SPF alignment until that record is updated.

---

## The daily loop

**20 emails per day. Hard cap.** Spread across the working day, never in a burst.
`jobhunt` learned this the expensive way: 35 sends in 2 minutes measurably hurt
deliverability.

1. Open `outreach/batch1.md`.
2. Take the next 20 that have not been sent.
3. For each: paste subject and body into Gmail, send, mark the row in `data/tracker.csv`.
4. Stop at 20 even if you have momentum.

**Rules that are not negotiable:**

- Plain text. No HTML, no tracking pixel, no image signature.
- **No links in email #1.** The Loom goes in follow-up #2, or immediately after a reply.
- One prospect, one thread. Never CC, never BCC a second prospect.
- If a draft says something you cannot personally defend on a call, delete the draft.
  Do not soften it.

### The anti-stall rule

**If drafted-but-unsent exceeds 10, stop sourcing entirely and send.**

More prospects is never the answer when the queue is already full. Update `TODAY.md`
with sent-count and next action at the end of every session, even a session where
nothing was sent. Especially then.

---

## Follow-up cadence: 3-7-7

Reply-to-your-own-thread, never a fresh email.

| Day | What | Contains a link? |
|-----|------|------------------|
| 0 | The batch-1 email | No |
| +3 | The audit Loom. 90 seconds, their site on screen, the specific gap. No pitch. | Yes, the Loom |
| +10 | One line: "Should I close this out?" | No |
| +17 | Nothing. Stop. | |

The day-3 Loom is the highest-converting asset in the whole system and the one most
likely to get skipped. Record it in one take. Two minutes of your time; do not polish it.

---

## The WhatsApp response-time probe

Optional, manual, **top 20 prospects only**, never automated.

Send one genuine buying question to their listed WhatsApp, as a real prospective
customer would. Note the timestamp. Note when the reply comes, if it comes.

Then say so plainly in the email:

> I messaged the WhatsApp on Sunday at 9pm asking about a 3BHK. The reply came Tuesday
> at 11am.

This is the single most powerful line available, because it is not an argument, it is
something that happened. Rules: one message, a real question, never a follow-up nag,
and **always disclose it in the email**. If you would not be comfortable with them
knowing you timed it, do not do it.

---

## What must never appear in an email

Named in the plan as binding, restated here because this is the file you will actually
have open. These are verified half-built in the `new/` repo:

| Never claim | Why |
|---|---|
| AI meeting notetaker | `src/lib/feature-flags/notetaker.ts` disables it in production. It is feature #02 on the landing page. |
| Lead discovery across Reddit, X, Instagram, Facebook, Google, LinkedIn | `conversation-platforms.ts` defaults to **Reddit only**. |
| Ziwo voice | Code-ready, blocked on SIP credentials. |
| A public REST API or developer console | Does not exist. Webhook-in and http-out only. |
| Meta or Google Ads orchestration | Absent entirely. |
| "Replaces $400/mo" | wrrk's own `roi-config.ts` figure, built by summing 17 tools' list prices. Quote the prospect's **detected** stack instead. It is smaller, true, and hits harder. |

Sell WhatsApp, unified inbox, CRM, workflows, email campaigns, people/HR. Those carry
live customer traffic.

---

## Tracking

`data/tracker.csv`, one row per prospect, segmented by **market x pool x angle** so reply
rate is measurable per cell. Batch 2 doubles down on whichever cell actually replied
rather than on whichever cell felt good to write.

Columns: `company, domain, market, pool, angle, to, sent_date, opened, replied, positive,
call_booked, followup_stage, notes`
