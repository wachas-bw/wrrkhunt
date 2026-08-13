# TODAY

Live state. Update at the end of every session, including sessions where nothing was
sent. Especially those.

## 2026-08-11 (batch 2 built)

**Batch 1: 6 sent. Batch 2: 15 drafted, 0 sent.**

Drafted-but-unsent is 15, above the stop-sourcing threshold of 10. **Send before
sourcing anything else.**

### Next action, in order

1. **Batch 1 follow-ups** are due. Cadence from the send date:
   - **14 Aug** the audit Loom. 90 seconds, their site on screen, the specific gap. This
     is the highest-converting asset in the system and the one most likely to be skipped.
   - **21 Aug** one line: "Should I close this out?"
   - **28 Aug** stop.
   Reply in the same thread, never a fresh email.
2. **Send batch 2**, 20/day cap, from `outreach/batch2.md`.
3. Unblock the 4 failures below.

### Batch 2: 15 ready, Pune agencies plus batch 1 leftovers

| Module pitched | Prospects |
|---|---|
| CRM | 6 |
| Tools + People/HR + Tasks | 5 |
| Email + unified inbox | 3 |
| WhatsApp + CRM | 1 |

Strongest three:

- **BrandLoom** (fit 100) pays for **Interakt**, which does WhatsApp and nothing else.
  The only prospect so far where we can name the exact tool they are already paying for.
- **Bright Brain** (fit 100) runs Meta and Google ads with **no CRM detectable anywhere**.
- **Skovian Ventures** (fit 85) publishes `careers@`, `sales@` and `support@` with no
  helpdesk behind them, and separately runs HubSpot.

### Blocked, do not send

| Company | Reason |
|---|---|
| DLIFE Home Interiors (85) | No address published. Best of the blocked, already runs Zoho + Tidio. Find a founder on LinkedIn. |
| Brainmine (75) | No address published. |
| Bemaster Education (60) | Domain has no MX. Their working address is the Gmail one, which is itself the hook. |
| MBBS Expert (50) | No address published. |

### What changed in the system this session

- **stack_detect** now covers ATS, HR/payroll, accounting, e-sign, project and forms
  vendors, and scores **per module** (`module_fit`) instead of one WhatsApp-centric
  number. One prospect list now serves every angle.
- **draft.py** gained `agency_ops`, `email_volume` and `crm_gap`, and now ranks
  **evidence above assumption**: a detected tool beats a guess about the vertical.
- Voice moved to **founding engineer**, which is a materially stronger frame. A founder
  can change the product on the call; an employee cannot.

### Bugs caught by verification, worth not reintroducing

- `careers@skovian.com` was chosen as the send address. A vendor pitch to a recruiting
  inbox is ignored. Wrong-audience locals are now demoted across all pools.
- Greeting read a different address than the envelope, producing **"Hi Careers,"** on a
  mail addressed to `sales@`. Greeting now derives from the send address only.
- **"Hi Acaddcntr,"** and **"Hi Anilbhat,"**: a company mailbox and a first+last run
  together. Both now fall back to "Hi there,". Better to greet nobody than wrongly.
- The agency body asserted proposal/contract/timesheet/invoice were four separate tools.
  We cannot see that. It is now phrased as a question.

### Worth knowing

- **Meta Ad Library 403s after sustained use** on one browser profile. Switching to a
  different playwright server (separate Chrome profile) cleared it immediately.
- Ad Library is the wrong source for batch 2. UAE recruitment agencies returned **1
  domain in 13**: they advertise straight into WhatsApp with no website, so there is
  nothing to audit. Published directories were the right source for agencies.
- **`stack_usd_mo` is an estimate**, seat count times list price. Never quote it as a
  figure in an email. Name the tools, not the number.

### Do not

- Do not source more prospects until batch 2 is sent. This is exactly how `clienthunt`
  stalled with 12 warm leads.
