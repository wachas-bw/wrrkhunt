# Batch 1 — LinkedIn targets

None of these six prospects link LinkedIn from their own website. That is not a gap in
the research, it is a finding: they are **Instagram and WhatsApp first**, which is the
ICP working exactly as intended. So every URL below came from search, and each carries a
confidence level.

**Confidence matters here.** Sending a connect note that references someone's WhatsApp
setup to the wrong company is worse than sending nothing. Only VERIFIED rows are safe to
send without opening the profile first.

---

## VERIFIED — safe to send

### 1. Montdor Interior Pvt Ltd  (fit 100, IN, interior design)

| | |
|---|---|
| Company page | https://www.linkedin.com/company/montdor-interior-pvt-ltd |
| Person | **Kashyap Dhanak** — https://in.linkedin.com/in/kashyap-dhanak-60213126 |
| Company ID | 77199822 |

**New signal found while searching, and it is the best one in the batch:** Montdor is
**actively hiring a Senior Sales Executive**.

- Job post: https://in.linkedin.com/jobs/view/senior-sales-executive-at-montdor-interior-pvt-ltd-4437263295
- They also posted "Urgent Hiring at Montdor Interior Pvt Ltd!"

That is audit check #4 confirmed. They are about to pay a salary to a person whose first
problem will be that leads arrive across five unconnected inboxes. Use the alternate
email below instead of the generic one.

**Alternate email for Montdor (use this one, 88 words):**

```
Subject: before your new sales hire starts

Hi Chandni,

I built the WhatsApp side of wrrk.ai, so I notice this stuff. You are hiring a Senior Sales Executive, and running ads into wa.me/918980531520 at the same time. Whoever takes that job inherits five separate inboxes: info, chandni, gandhinagar, ghatkopar and goregaon, with nothing joining them. They will spend their first month rebuilding context that should already exist.

Worth 15 minutes before they start? I can show it with your own numbers in it.

Wachas
```

**Connect note (287 chars):**

```
I build the WhatsApp side of wrrk.ai. Noticed Montdor runs ads into WhatsApp with several branch inboxes behind it and nothing joining them, right as you are hiring a sales exec. Decorpot runs their customer side on us. Happy to send what that setup looks like joined up, no pitch.
```

### 2. ACADD Centre  (fit 65, IN, CAD training)

| | |
|---|---|
| Company page | https://in.linkedin.com/company/acadd-centre |
| Alt profile | https://www.linkedin.com/in/acadd-centre-77479a204/ |
| Location | Airoli, Mumbai + Thane, plus online |
| Size | 72 followers, ISO certified, MSME registered, claims 80,000 students over 15 years |

**Careful:** "CADD Centre" (one A) is a large, unrelated national franchise. ACADD Centre
is a different and much smaller company. Do not conflate them, and do not reference
anything from CADD Centre's pages.

---

## UNVERIFIED — open the profile and confirm before connecting

### 3. Muse Interior Design Dubai  (fit 95, AE)

Three plausible pages, none confirmed as `musedesign.ae`:

- https://www.linkedin.com/company/museinteriordesign
- https://in.linkedin.com/company/musedesign
- https://ae.linkedin.com/company/muse-interior-designing

Search surfaced founders "Stanislava Rudas-Dudnyk and Michael Dydnyk, founded 2007" for a
company called Muse Design in the UAE. **I could not confirm that is the same entity as
musedesign.ae**, and "Muse" is a common studio name. Do not use those names in a note
until you have matched the website on the profile.

### 4. Apex Homes  (fit 95, AE)

- https://www.linkedin.com/company/apexhomesdxb

Listed as **Real Estate**, whereas `apexhomes.ae` presents as interior and fit-out. Could
be the same firm with a broad description, could be a different company. Verify first.

Unrelated despite the name: Royal Apex (https://ae.linkedin.com/company/royal-apex-uae).

---

## NOT FOUND — use email or Instagram

### 5. Invi Edutech  (fit 95, IN, study abroad)

No LinkedIn presence found. They run several domains: `inviedutech.com` (the one we
audited), plus `invi.com.vn` and `invi-mbbsglobal.com`. Worth confirming which entity
`prajapathi.m@inviedutech.com` belongs to before sending.

Reachable instead on Instagram `@studymbbs_invietnam_` and Facebook.

### 6. KHA Interior Decorations  (fit 50, AE)

No LinkedIn company page found. Their site says the firm came from a partnership of two
Dubai engineering and design companies. `m.amin@khainterior.ae` is a named individual, so
email is the better channel here anyway.

---

## Sending rules for this channel

- Connect note only, **under 300 characters**, no links, no pitch.
- Do not send the connect note and the email on the same day. Email first, LinkedIn on
  day 2 if there is no reply.
- If wrrk's own LinkedIn automation is ever used for this, respect the safety limits
  already coded in `new/src/lib/linkedin/safety-config.ts`: 80 connections/day,
  30-180s random delays, 9am-6pm only, weekends paused, auto-pause under 15% acceptance.
  For a batch this small, send them by hand.
