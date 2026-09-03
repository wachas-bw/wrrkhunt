# wrrkhunt team prospecting guide

This is the canonical handbook for finding, qualifying, contacting, and measuring
prospects for wrrk.ai. It is written for teammates and for coding agents working in this
repository. Read it before running discovery, changing qualification policy, generating
copy, or touching an external channel.

The repository has two generations of tooling:

- the current approval-gated system under `prospecting/` and the `automation` CLI;
- historical JSON/CSV scripts and campaign records under `data/`, `outreach/`,
  `sources/`, and `enrich/`.

The current system is authoritative. Historical files are useful evidence and design
history, but they are not live campaign state.

## 1. The operating model

```text
buying signal
    -> first-party website audit
    -> published business contact
    -> qualification and regional policy
    -> evidence-cited Codex draft
    -> human review and exact-hash approval
    -> explicit release and paced delivery
    -> reply, bounce, opt-out, and suppression handling
```

A company name is only a candidate. It becomes an actionable prospect only when the
system has usable public evidence, a fit score at or above the configured threshold,
a permitted market, and a contact that the business visibly published.

The core thesis is a scoring inversion: a small team with many customer front doors and
no detected shared operating layer is often a stronger fit than a company with an
expensive mature stack. Typical front doors are WhatsApp, Instagram, email, phone,
booking widgets, and forms. Tool non-detection is a lead hypothesis, not proof of
absence, so copy must say "not detected" and cite the page that was audited.

### Initial campaign shape

The default campaign covers India, UAE, Singapore, United Kingdom, and United States:

| Share | Pool | Primary signal |
|---:|---|---|
| 50% | WhatsApp-heavy service SMBs | Customer conversations, ads, forms, bookings, and visible operational fragmentation |
| 30% | Agencies and published directories | Multi-client operations, several inboxes, fragmented project/finance/HR tools |
| 20% | Funded and early-stage startups | New budget, growing team, low switching cost, founder-accessible evidence |

The mix is a discovery target, not permission to weaken qualification. If fewer than the
daily cap qualify, contact fewer companies.

## 2. What is production, optional, or historical

Use these labels consistently in issues, reports, and code review:

- **Production**: integrated with the SQLite lifecycle and covered by tests.
- **Supported manual**: a documented human step that can feed or complete the lifecycle.
- **Optional official**: automated only through an approved provider API and explicit
  setup.
- **Experimental reference**: useful code or a proven concept elsewhere in the original
  `jhunt` workspace, but not a supported wrrkhunt command.
- **Historical or rejected**: retained for learning; do not treat it as the current path.

### Complete method catalog

| Method | Status | What it finds | Cost/account | Canonical next step |
|---|---|---|---|---|
| Exa official-site discovery | Production | Service SMBs, agencies, directories, funded startups | `mcporter`; provider limits apply | Ingest domain and source excerpt, then audit |
| Exa default, expansion, fresh, and conversion waves | Production | Four disjoint query families for additional supply | Same as Exa | Use one wave per run; dedupe remains global |
| Published business and association directories | Production through Exa | Agencies and service firms with official sites | Usually free search | Resolve to the company site, never qualify from the directory alone |
| Funding news and accelerator cohorts | Production through Exa | Pre-seed, seed, Series A, YC, Techstars, and accelerator signals | Usually free search | Verify current company site and operating fit |
| Meta Ad Library | Production | Active click-to-WhatsApp advertisers in IN, AE, and SG | Free public library; Playwright required | Extract destination site, then first-party audit |
| Website stack and front-door detection | Production | 83 vendor signatures plus channels, forms, ads, hiring, and business metadata | Free public pages | Persist URL, excerpt, time, observed value, and confidence |
| First-party email discovery and MX check | Production | Visibly published business email addresses | Free public pages and DNS | Create one primary contact per company if policy passes |
| UAE business phone and WhatsApp-number prospecting | Production, isolated read-only export | Public UAE business numbers with first-party evidence | Exa optional; no contact action | Private ranked CSV and rejection audit only |
| LinkedIn post intake from a normal browser | Supported manual and default | Fresh prospect and influencer posts | LinkedIn account used by the human | Paste exact post facts into dashboard, draft, review, and manually post |
| LinkedIn post search through Apify | Implemented but disabled in safe mode | Founder/operations posts | Paid actor, hard budget cap | Enable only after explicit policy and budget decision; prior yield was poor |
| LinkedIn post search through Exa | Implemented but disabled in safe mode | Public indexed post URLs with provable dates | Exa limits apply | Reject unknown dates and posts older than 48 hours |
| LinkedIn Comments API | Optional official | Submission of an approved comment | Approved LinkedIn developer app and OAuth scope | Exact-hash API release after all checks |
| Legacy JSON/CSV import | Production migration path | Existing stack, contact, tracker, and batch history | Free | Import into held `legacy` campaign; never auto-select it first |
| X/Twitter intent search | Experimental reference | People explicitly asking for CRM, workflow, growth, or agency help | X session or approved API | Reuse only read-only discovery/classification; human engagement |
| LinkedIn hiring/intent actor from `jobhunt` | Experimental reference | Hiring posts and public contact/apply signals | Apify | Adapt the classifier, then route domains into the canonical audit |
| Threads keyword-post harvesting | Experimental reference | Recent hiring or founder-intent posts | Browser session | Manual review, terms check, then canonical intake adapter |
| Telegram hiring-channel ingestion | Experimental reference | Structured company, role, and apply-link posts | Telegram API ID/hash and user session | Use only for channels the operator may access; treat as a buying-signal feed |
| Greenhouse, Lever, and Ashby public job-board APIs | Experimental reference | Companies actively hiring growth, support, sales ops, CRM, or people roles | Public APIs | Treat hiring as intent; audit the company site before outreach |
| RemoteOK and Remotive APIs | Experimental reference | Remote-company hiring signals | Public APIs | Resolve company domain, dedupe, audit |
| Hacker News Who Is Hiring via Algolia | Experimental reference | Direct founder/company hiring posts | Public API | Extract company signal, not personal contact, then audit |
| LinkedIn Jobs and manual recent-role search | Experimental reference | Companies hiring growth, support, RevOps, customer success, or operations | Human browser/account | Record the role URL as intent, then audit the official company site |
| Google Ads Transparency Center | Supported manual | Active Google advertisers and creative/message evidence | Free public tool | Save source URL/screenshot and verify the destination site |
| PageSpeed and Core Web Vitals | Supported manual | Slow landing pages and technical friction | Free public tool | Use only when relevant to the wrrk workflow hypothesis |
| AEO/SEO answer-engine checks | Supported manual | Missing or inconsistent public discoverability | Public search tools | Record query, date, result, and uncertainty; never claim permanent absence |
| Reddit, Indie Hackers, founder Slack/Discord, and Facebook groups | Supported manual research | Explicit requests, recommendations, and operator pain | Usually free; community rules apply | Respond helpfully in-channel or route a company domain to audit |
| Upwork, Contra, Wellfound, YC Work at a Startup, and public RFPs | Supported manual research | Paid projects, new teams, and explicit tool/service demand | Account/platform terms apply | Follow platform rules; do not extract hidden contact data |
| Cutshort, Instahyre, Hasjob, and Naukri | Experimental reference | India startup and SMB hiring signals | Human browser/account | Use the company/role as intent; do not repurpose applicant-only contact data |
| Apollo company, job, and firmographic search | Optional external source, not shipped | Company lists and hiring/funding filters | Credits and account required | Company signal only until first-party contact evidence is found |
| Apify email verifier, website-email scraper, or contact database | Historical enrichment experiment | Address candidates and SMTP status | Credits and personal-data access | Cannot override the first-party publication rule; not a canonical contact source |
| Google/Meta business-profile phone corroboration | Optional evidence, not authoritative | Confirmation of an already first-party public number | Public profile | Exact E.164 match may raise confidence; cannot establish a contact alone |
| GitHub contribution or build-with-product outreach | Supported manual relationship channel | Technical founders and open-source companies | Time, no list purchase | Make a real contribution/demo, then contact transparently |
| Warm referrals and partner introductions | Supported manual relationship channel | Mutual-connection and ecosystem leads | Human relationship | Ask for a transparent introduction; record source and consent context |
| Build-in-public posts and useful public comments | Supported manual inbound channel | Founder and operator attention | Human-authored social activity | Measure inbound conversations, not vanity impressions |
| Guessed email patterns or synthesized addresses | Historical and prohibited | Unverified personal addresses | Bounce and privacy risk | Never use; first-party publication is mandatory |
| Automated LinkedIn browser control, stealth, fingerprinting, or CAPTCHA handling | Prohibited | None worth the account risk | Account/platform risk | Manual browser handoff or approved official API only |
| Automated X replies from the external twikit engine | Historical and not approved for wrrkhunt | High-volume engagement | Account/platform risk | Do not run; retain only discovery, dedupe, and review ideas |
| Greenhouse/Ashby form submission and Lever form filling | Historical job-hunt workflow only | Job applications, not sales leads | Applicant authorization; CAPTCHA may require a human | Never use application forms for vendor outreach |
| Deceptive WhatsApp response-time probes | Retired | Response-time anecdote | Deception and consent risk | Do not impersonate a buyer; use transparent research or public evidence |

Do not run any credit-consuming search, export, enrichment, or personal-contact access without the user's prior explicit approval.

## 3. Canonical repository map

| Path | Responsibility |
|---|---|
| `automation` | Stable command-line entry point |
| `prospecting/cli.py` | Public command definitions and dispatch |
| `prospecting/discovery.py` | Exa, Meta, optional LinkedIn post discovery, budgets, provenance, and ingestion |
| `prospecting/audit.py` | Website audit orchestration and qualification |
| `sources/stack_detect.py` | Public-site stack, channel, phone, location, confidence, and fit evidence |
| `enrich/find_contacts.py` | First-party published email discovery, candidate people, phone extraction, and MX |
| `prospecting/policy.py` | Regional, contact, suppression, cooldown, and qualification gates |
| `prospecting/db.py` | SQLite schema, state transitions, immutable approvals, counters, suppressions, and leases |
| `prospecting/copy_engine.py` | Codex structured generation and deterministic copy validation |
| `prospecting/email_delivery.py` | Gmail SMTP, IMAP replies/bounces, compliance body, and stop conditions |
| `prospecting/gmail_queue.py` | Connector-assisted scheduling and Gmail state reconciliation |
| `prospecting/linkedin_delivery.py` | Manual browser handoff and confirmation |
| `prospecting/linkedin_api.py` | Optional official OAuth Comments API path |
| `prospecting/dashboard.py` | Loopback-only review, edit, approve, release, history, and emergency controls |
| `prospecting/scheduling.py` | Recipient-local weekday windows and pacing |
| `prospecting/worker.py` | Short email/LinkedIn worker cycle, leases, and retention |
| `prospecting/launchd.py` | macOS LaunchAgent definitions and private runtime staging |
| `prospecting/phone_prospecting.py` | Isolated UAE phone workflow |
| `prospecting/exporter.py` | Private, formula-safe email-contact CSV export |
| `prospecting/migration.py` | Read-only import of historical campaign artifacts |
| `tests/` | Unit and integration-style safety tests |
| `AUTOMATION.md` | Exact setup and operator reference |
| `SEND-RUNBOOK.md` | Current email release, delivery, and inbox procedure |
| `AGENTS.md` | Codex and general coding-agent context |
| `CLAUDE.md` | Claude Code entry point, importing `AGENTS.md` |

Private runtime state lives outside Git:

```text
~/Library/Application Support/wrrkhunt/
  wrrkhunt.sqlite3
  browser-profile/meta/
  automation-logs/
  exports/
  launchd-runtime/
```

Never infer live state from `TODAY.md`, old batches, or CSV files. Use:

```bash
./automation status
./automation health all
```

## 4. Discovery runbooks

### 4.1 Exa: the broad, repeatable source

The repository includes `config/mcporter.json`, which points `mcporter` at Exa. A custom
configuration may be supplied with `WRRKHUNT_MCPORTER_CONFIG`; a nonstandard executable
may be supplied with `WRRKHUNT_MCPORTER_BIN`.

The query matrix contains ten units per market: five service-SMB queries, three agency
queries, and two startup/funding queries. Four mutually exclusive waves prevent repeated
wording from returning the same result set:

```bash
./automation discover --markets IN,AE,SG,GB,US --mix --target 60 --source exa
./automation discover --markets IN,AE,SG,GB,US --mix --target 60 --source exa --expansion
./automation discover --markets IN,AE,SG,GB,US --mix --target 60 --source exa --fresh-wave
./automation discover --markets IN,AE,SG,GB,US --mix --target 60 --source exa --conversion-wave
```

The waves cover clinics, interior/renovation firms, real estate/property services,
education and training, business/professional services, home/field services, recruitment,
logistics, legal/accounting, hospitality-adjacent operators, agencies, funding rounds,
YC, Techstars, and other accelerators. The exact current terms are in
`prospecting/discovery.py` and are the source of truth.

`--target` is the number of candidates to audit after discovery, not a guaranteed source
count and not a hard ceiling on search results. Global registrable-domain deduplication
means a later wave may add few candidates. That is expected.

Every Exa result must resolve to a non-blocked company domain and include an excerpt.
LinkedIn, Facebook, directories, news sites, and aggregators may establish the discovery
signal, but the company website establishes qualification.

### 4.2 Meta Ad Library: paid WhatsApp intent

Meta discovery runs only for IN, AE, and SG. It opens the public Ad Library in a dedicated
persistent Playwright profile and searches active ads using the measured pattern:

```text
"whatsapp" + vertical
```

Run it with:

```bash
./automation discover --markets IN,AE,SG --mix --target 60 --source meta
```

The useful artifact is the advertiser's external destination domain, not the Facebook
page. Challenges, login walls, network uncertainty, or changed page structure fail
closed. Do not bypass a challenge. The older `sources/meta_ads_extract.js` is a manual
extractor and research record; the integrated adapter in `prospecting/discovery.py` is
the current path.

### 4.3 Directories, funding, accelerators, events, and associations

These are discovery surfaces, not evidence substitutes. Capture the listing or article
URL and the signal it establishes, then resolve the official website and run the normal
audit. Useful high-intent variants include:

- member directories for agencies, clinics, property firms, professional services, and
  local business associations;
- newly funded companies, accelerator cohorts, product launches, and new-location
  announcements;
- event exhibitor, sponsor, and speaker lists where the company clearly serves a target
  market;
- hiring pages for growth, support, sales operations, customer success, CRM, RevOps,
  people operations, or operations roles;
- public tenders and RFPs that explicitly request customer-service, workflow, CRM,
  WhatsApp, or automation capability.

Add a source adapter only when it can retain provenance and respect access terms. Do not
scrape login-only or private data merely because it is technically reachable.

### 4.4 Hiring feeds as business-intent signals

The original `jobhunt` workspace has normalized adapters for Greenhouse, Lever, Ashby,
RemoteOK, Remotive, and Hacker News via Algolia. It also has ranking, geography,
recency, title gates, and per-company caps. Those files are not shipped in this public
repository, but their reusable pattern is:

1. read a legitimate public feed;
2. normalize company, role, location, URL, publication time, and excerpt;
3. dedupe by company and source URL;
4. treat a relevant opening as a buying signal;
5. resolve the official domain and pass it into the standard wrrkhunt audit;
6. find contact details only on the company's own public pages.

A role opening is not permission to pitch a recruiting mailbox. `careers@`, `jobs@`, and
`hr@` are blocked for vendor outreach.

The same workspace also used LinkedIn Easy Apply, Wellfound, YC Work at a Startup,
Cutshort, Instahyre, Hasjob, and Naukri for job hunting, plus Greenhouse/Ashby form
submission and human-completed Lever forms. These are documented for context only. For
wrrk prospecting, their legitimate reusable signal is that a company is hiring, not that
an applicant form can be turned into a sales channel. The reusable ranking pattern is
title gate, geography/market gate, recency, score, tier, per-company cap, dedupe, and a
channel-specific draft queue.

### 4.5 Social and community intent

The original `clienthunt` workspace measured two useful classifiers:

- **ask**: a person explicitly asks for a recommendation or help;
- **hire**: a company is hiring for the function, with freelance/project roles treated as
  especially strong service intent.

It tested LinkedIn posts through Apify and X searches through `twikit`. The X run found a
higher genuine-ask rate than the LinkedIn test, while the wrrkhunt LinkedIn experiment
returned 180 posts and no usable leads. These are historical observations, not forecasts.

For wrrk.ai, adapt the query vocabulary to CRM, shared inbox, WhatsApp operations,
follow-up leakage, customer support, RevOps, sales operations, and small-team workflow.
Keep discovery read-only. A human should inspect the post and choose whether to reply,
DM, or route the company to email. Do not run the external repository's automated reply
scripts.

Manual communities can be high signal when their rules permit requests and vendor
responses: Reddit, Indie Hackers, operator Slack/Discord communities, Facebook owner
groups, and marketplace/RFP feeds. Contribute a specific answer in-channel. Do not use a
helpful comment as a disguised advertisement.

## 5. Audit and qualification

### 5.1 First-party website audit

The detector fetches the homepage plus relevant public pages with normal certificate and
hostname verification. It never disables TLS checks. It records source URL, evidence
excerpt, observed value, detection time, and confidence.

There are currently 83 signatures across 15 categories:

| Category | Signatures | Category | Signatures |
|---|---:|---|---:|
| WhatsApp BSP | 12 | ATS | 9 |
| Chat | 8 | Email | 7 |
| HR | 7 | Platform | 7 |
| Finance | 6 | Project | 6 |
| CRM | 5 | E-sign | 4 |
| Analytics | 3 | Forms | 3 |
| Ads | 2 | Booking | 2 |
| Support | 2 |  |  |

The audit also inspects WhatsApp links, social links, mailto addresses, phones, forms,
booking, ad pixels, and multiple published inboxes. JavaScript-heavy sites with no vendor
hit are low-confidence and require separate browser verification before copy may rely on
the finding.

Run a standalone legacy audit with:

```bash
python3 sources/stack_detect.py --seats 8 example.com
```

Run the canonical queue audit with:

```bash
./automation audit --limit 60
./automation audit --revalidate
```

### 5.2 Contact evidence

Only an email visibly published by the business on its own public page is usable.
Published mailto links count. A freemail address can count outside the UK only when the
business itself presents it as the business contact. Candidate names from team pages are
review hints only and must never be used to synthesize an address.

The system rejects wrong-audience and unreplyable inboxes, including careers, jobs, HR,
recruiting, legal, privacy, support, and no-reply variants. It checks MX for the exact
email domain, including published freemail domains.

### 5.3 Qualification gates

Email eligibility requires all of the following:

- fit score at or above 75, unless the configured threshold is made stricter;
- usable URL-backed evidence and adequate confidence;
- a visibly published business contact and MX availability;
- enabled market policy;
- no email, domain, or LinkedIn suppression;
- no previous initial email to the same registrable domain inside 90 days;
- one initial recipient per company;
- for the UK, visible corporate incorporation evidence and no freemail contact;
- for the US, a configured valid business postal address.

Deduplication is by registrable domain, normalized email, and normalized LinkedIn profile
or post URL. Existing customer and own-company suppressions always win over score.

## 6. Outreach channels

### 6.1 Email

Email is the only fully integrated outbound channel in the default installation. It uses
Gmail SMTP/IMAP for `wachas@wrrk.ai`, with the app password in macOS Keychain. Messages
are plain text, one recipient at a time, with truthful identity, postal address,
commercial disclosure, reply-based opt-out, and a `List-Unsubscribe` mailto header.

The current `founder_booking_note_v4` initial-email style requires:

- a concrete, non-promotional subject of three to five words;
- `Hi there,` rather than a guessed name;
- 60 to 80 pitch words in two or three short paragraphs;
- a supplied evidence detail and valid evidence IDs;
- one restrained 15-minute tailored-demo question;
- the exact configured booking URL, `https://wrrk.ai/book/wrrkaidemo`, once, with no
  other URL;
- sender lines `Wachas` and `Founding engineer, wrrk.ai`;
- no unsupported features, banned punctuation, wrong-audience inbox, or guessed claim.

Follow-ups are due at +3 and +10 days, remain in the original thread, contain no links,
and require a fresh approval on the due day. A human reply cancels queued follow-ups.
Codex may draft a response, but a reply can never be released automatically.

The fresh-install daily cap is 20. Attempts count across retries and restarts. Messages
are spaced 7 to 15 minutes apart and placed in recipient-local weekday business hours.
The cap is a ceiling, not a target. See `SEND-RUNBOOK.md` for the exact release procedure.

The system records scheduled/sent IDs, delivery attempts, bounces, replies, opt-outs,
and failures. It intentionally does not use pixels or open tracking, so it cannot claim
an email was opened or reached the primary inbox. "No bounce observed" means only that
no bounce was observed.

### 6.2 LinkedIn

The safe default is manual post intake and manual submission:

1. A human finds a relevant post in their normal browser.
2. In the local dashboard, they enter the exact post URL, author URL/name, full visible
   text, visible publication time, role, market, and prospect domain when applicable.
3. Only posts no older than 48 hours are eligible.
4. Codex drafts a 40 to 250 character, post-specific, non-promotional, link-free comment.
5. The human edits or approves the exact hash.
6. The dashboard opens the exact post and copies the approved comment.
7. The human verifies the author and text, pastes, posts, and confirms the exact comment.

The daily allocation is three qualified-prospect posts and two relevant-influencer posts,
subject to supply. The daily cap is 5, with one comment per post and a 14-day author
cooldown. Email and LinkedIn can remain independently paused.

Automated submission is permitted only through the official LinkedIn Comments API after
the developer app and Community Management use case are approved. There is no supported
automation of linkedin.com pages, no cookie extraction, no stealth, no fingerprint
spoofing, and no CAPTCHA handling.

### 6.3 X/Twitter, communities, DMs, phone, and WhatsApp

These are relationship or research channels, not automated delivery paths in wrrkhunt:

- X/Twitter: find explicit pain or recommendation asks; reply manually and specifically.
- LinkedIn DMs/connections: manual only and outside the current automation lifecycle.
- Communities: answer the question where it was asked; follow community rules.
- Phone/WhatsApp: the UAE command produces a private research export only. It does not
  call, message, import, or enqueue anyone.
- GitHub: a real contribution or demo can be a higher-signal reason to contact a technical
  founder than a cold pitch.
- Referrals: ask a mutual contact for a transparent introduction and record the source.

Any future channel must enter the same evidence, suppression, approval, cap, and audit
model before it becomes an automated action.

## 7. Codex copy and approval integrity

Copy is generated only by the locally authenticated Codex CLI:

```text
codex exec --ephemeral --sandbox read-only --output-schema ...
```

The prompt contains structured evidence, allowed product claims, post content when
needed, and channel constraints. Every output item must cite evidence IDs. There is no
fallback model or generic template when Codex authentication, schema validation, or
linting fails.

The lifecycle is:

```text
discovered -> audited -> qualified -> drafted -> pending_approval
           -> approved -> scheduled -> sent / posted
```

Terminal states are `blocked`, `rejected`, `suppressed`, `failed`, and `replied`.
Editing approved copy clears approval. Workers may act only on the exact immutable final
delivery hash approved that day. Daily counters are transactionally reserved before an
external attempt, so restarts and retries cannot bypass caps.

## 8. Daily team workflow

### Start of day

```bash
git status --short
./automation status
./automation health all
./automation inbox
```

Resolve channel stops, replies, bounces, and opt-outs before sourcing more. Never edit or
delete the SQLite database to clear a gate.

### Discover and audit

```bash
./automation discover --markets IN,AE,SG,GB,US --mix --target 60
./automation audit --limit 60
```

Use a later Exa wave only when prior waves are already represented. Do not spend Apify or
another provider's credits merely to fill a quota.

### Prepare and review

```bash
./automation prepare --email-limit 20 --comment-limit 5
./automation serve
```

Open `http://127.0.0.1:8765`. For each item, inspect the company, source URL, evidence
excerpt, confidence, contact publication URL, copy, and recipient. Edit if needed, then
approve. Any edit requires reapproval.

### Release and observe

Release only the reviewed items from the dashboard. The worker may be inspected without
mutation first:

```bash
./automation worker --dry-run
./automation worker
./automation inbox
```

Use `pause`, `resume`, and the dashboard emergency stop for channel control. Do not run a
second delivery mechanism against the same scheduled IDs.

### End of day

Record at least:

- candidates seen and inserted by source;
- audited and qualified counts;
- drafts, approvals, releases, sends/posts, and remaining queue;
- bounces, human replies, positive replies, opt-outs, and calls booked;
- source spend and provider budget remaining;
- tests or copy variant used;
- paused channels and exact stop reasons.

Use the database and delivery events as evidence. Do not report opens because they are
not tracked.

## 9. Setup and scheduling

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
./automation init
./automation import-legacy --dry-run
./automation import-legacy
./automation setup gmail --postal-address "VALID BUSINESS POSTAL ADDRESS"
./automation setup linkedin
```

Both channels start paused. Setup does not approve or release messages.

After controlled Gmail seed tests and a private/test LinkedIn post test pass:

```bash
./automation install-launchd
```

The normal schedule starts the dashboard on login, runs discovery at 07:30 and
preparation at 09:00 on weekdays, runs a worker every five minutes, and polls IMAP every
15 minutes. If Gmail delivery credentials are absent, the SMTP/IMAP agents are omitted.
Late wake-ups are moved to a future valid local window instead of bursting overdue work.

## 10. Stops, failures, and compliance behavior

Email pauses on authentication/quota failures, uncertain SMTP state, or two bounces in a
rolling 20-message window. LinkedIn API mode pauses on expired authentication, 401/403,
rate limits, uncertain submission, missing definitive external ID, or state mismatch.
Manual LinkedIn mode never submits anything itself.

An opt-out immediately suppresses both email address and domain. A human reply cancels
pending follow-ups. Suppression identifiers remain until explicitly removed; completed
campaign personal data is eligible for cleanup after the configured 180 days.

These are engineering safeguards, not legal advice. Review current market requirements
before enabling outreach. The implementation is designed around the FTC's CAN-SPAM
guidance and the ICO's B2B direct-marketing guidance linked from `AUTOMATION.md`.

## 11. Adding a new source safely

A source is not complete when it returns names. A production adapter must:

1. create and finish a `source_run` with source, query, market, pool, status, error, and
   cost/budget note;
2. retain source URL, excerpt, observed time, and enough metadata to reproduce the signal;
3. normalize and dedupe registrable domain, email, LinkedIn URL, and post URL;
4. respect suppression before insertion;
5. avoid private/login-only data and preserve normal TLS verification;
6. separate discovery evidence from first-party qualification evidence;
7. fail closed on ambiguous dates, identity, page structure, or submission state;
8. add parser, failure, dedupe, budget, and no-side-effect tests;
9. document account, cost, access terms, measured yield, and rollback/disable behavior;
10. leave external action behind the existing approval and immutable-hash release gates.

Good next adapters, in priority order, are public ATS hiring signals for customer-facing
operations roles, public event/association directories, public RFP feeds, and a manual
Google Ads Transparency evidence intake. X/Threads adapters should remain discovery-only
until their access model and platform terms are explicitly approved.

Other useful candidates for the backlog are first-party release notes or expansion/news
feeds, newly opened locations, public partner directories, competitor migration pages,
and manually reviewed G2/Capterra complaints that describe fragmented support or CRM
work. Google Business Profile/Maps can help resolve a public company website or
corroborate a phone, but profile data should not replace first-party contact evidence.
Every one of these remains an idea until its adapter, provenance model, access terms,
tests, measured yield, and disable path satisfy the checklist above.

## 12. Historical evidence and measured lessons

Historical records report these source outcomes:

| Source experiment | Result | Lesson |
|---|---:|---|
| Meta Ad Library | 17 usable of 27 in the strongest measured query | Literal `"whatsapp"` plus a vertical materially improves fit |
| Published directories | 15 usable of 19 | Strong source for agencies with auditable official sites |
| Funding reports | 5 usable of 7 | Good budget/growth signal, still requires operational fit |
| Apify LinkedIn posts | 0 usable of 180 in the wrrkhunt experiment | Low base rate; do not lower qualification or spend blindly |
| Clienthunt LinkedIn intent | 2 explicit asks and 22 hiring signals of 196 posts | Hiring is more common than direct asks; human review matters |
| Clienthunt X intent | 3 explicit asks and 4 hiring signals of 177 posts | Fast explicit asks can be valuable but go stale quickly |

These counts describe specific historical query sets. They are not promises about future
yield. Keep measured source performance in canonical run records and compare cohorts by
market, pool, source, and copy style.

## 13. Repository and data boundary

The published Git repository is the nested `wrrkhunt` project. The original parent
`jhunt` workspace had no configured remote at the time this guide was written and also
contained resumes, personal job-hunt records, raw lead exports, and other private
artifacts. Those files are intentionally not copied here. Their reusable methods are
described above without importing their private datasets.

This GitHub repository is currently public, while historical tracked `data/` and
`outreach/` files contain real business contacts and campaign material. Treat that as a
privacy issue: do not add new lead exports, credentials, browser sessions, email files,
or SQLite state. Prefer making the repository private and cleaning sensitive history
before sharing beyond the authorized team.

## 14. Sources and coverage

This guide was derived from the current `wrrkhunt` CLI, `prospecting/` modules, legacy
scripts, tests, and operator documents. It also generalized reusable methods from the
local parent workspace's `clienthunt` and `jobhunt` implementations and from the separate
local `twikit-reply-engine` reference. Private datasets, cookies, sessions, credentials,
and resume/job-application content were not copied.

Coverage is repository-bounded. It catalogs every prospecting and job-signal method
found in the audited local workspace, plus a clearly labeled extension backlog; it is not
an exhaustive survey of every lead source available in the market. Provider pricing,
access, quotas, API scopes, laws, and platform terms can change and must be rechecked at
execution time.
