# wrrkhunt prospecting automation

For the complete source/channel catalog and team operating model, start with
[`TEAM-PROSPECTING-GUIDE.md`](TEAM-PROSPECTING-GUIDE.md). This file is the detailed
setup and command reference.

This is a local, approval-gated prospecting system. It discovers fresh businesses,
audits their public sites, stores evidence in SQLite, asks the authenticated Codex CLI
for constrained copy, and exposes a loopback-only review desk. It cannot send an email
unless the exact current content hash was approved that day and explicitly released.
LinkedIn has two safe delivery modes: manual browser review (the default), or LinkedIn's
official OAuth-backed Comments API after the developer application has been approved for
Community Management access. wrrkhunt never controls or disguises activity in a signed-in
LinkedIn browser.

The held `legacy` campaign is imported for history and deduplication. It is never selected
for the fresh campaign. Existing JSON and CSV files are read but not modified.

## Install and initialize

```bash
cd /Users/wachas/Documents/GitHub/jhunt/wrrkhunt
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
# config/mcporter.json is included for the public Exa MCP endpoint. Set
# WRRKHUNT_MCPORTER_CONFIG only when your team uses a different configuration.
./automation init
./automation import-legacy
./automation status
```

Runtime state is private and outside Git at:

```text
~/Library/Application Support/wrrkhunt/
  wrrkhunt.sqlite3
  browser-profile/meta/
  automation-logs/
```

Set `WRRKHUNT_HOME` to a temporary directory for isolated tests or rehearsals.

## One-time channel setup

Gmail uses `wachas@wrrk.ai`, Gmail SMTP/IMAP, and a 16-character app password. The
setup command reads the password without echoing it and saves it to macOS Keychain.
`GMAIL_APP_PASSWORD` is supported only as a temporary fallback.

```bash
./automation setup gmail --postal-address "VALID BUSINESS POSTAL ADDRESS"
```

The command checks IMAP authentication before unpausing email. Email approval and
delivery remain blocked while the postal address is blank. Every delivered message is plain text and
contains truthful sender identity, a commercial-message disclosure, the configured
postal address, a reply-based opt-out, and a `List-Unsubscribe` mailto header.

LinkedIn setup enables manual-browser mode and opens the feed in your normal browser.
Sign in there if necessary. wrrkhunt does not inspect the session, browser profile,
cookies, page, or signed-in identity.

```bash
./automation setup linkedin
```

This boundary is intentional: there is no automated linkedin.com page control, stealth
mode, fingerprint spoofing, CAPTCHA handling, or attempt to conceal software control.

### Optional official LinkedIn API automation

Automated comment submission is supported only through LinkedIn's documented Comments
API. Before enabling it:

1. Create and verify a LinkedIn Developer application for the legal organization.
2. Request and receive Community Management API access. Development tier is for building
   and testing; use Standard tier for a live production integration under LinkedIn's
   approved use case.
3. Register the exact callback URL `http://127.0.0.1:8766/callback`. If LinkedIn has not
   enabled loopback redirects for the app, this local helper cannot activate it; request
   LinkedIn's approved HTTPS or native-PKCE setup instead of using a redirect workaround.
4. Run the setup command. The default `me` actor is resolved and verified through
   `r_basicprofile`; an organization can instead use its exact organization URN.

```bash
./automation setup linkedin-api --client-id "YOUR_LINKEDIN_CLIENT_ID"

# Organization-page actor, if that is the approved use case:
./automation setup linkedin-api --client-id "YOUR_LINKEDIN_CLIENT_ID" \
  --actor-urn "urn:li:organization:123456"
```

The client secret is requested without echo and saved in macOS Keychain. OAuth requests
only `r_basicprofile` plus `w_member_social_feed` or `w_organization_social_feed`. Access
and refresh tokens also stay in Keychain, not SQLite or Git. The API worker verifies the
OAuth member, actor, target URN, stored post-text hash, exact approved comment hash,
freshness, suppression, author cooldown, local-time window, pacing, and daily cap. A
401/403, quota response, missing definitive comment ID, network uncertainty, or local
recording uncertainty pauses the channel and is never retried automatically.

Official references:

- [Comments API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/comments-api?view=li-lms-2026-07)
- [3-legged OAuth](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow)
- [Community Management access tiers](https://learn.microsoft.com/en-us/linkedin/marketing/increasing-access?view=li-lms-2026-05)
- [Native/loopback OAuth](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow-native)

## Daily workflow

```bash
# Find and audit a balanced 50/30/20 fresh cohort.
./automation discover --markets IN,AE,SG,GB,US --mix --target 60

# Use the third, disjoint Exa query wave when the standard and expansion waves
# have already been collected.
./automation discover --markets IN,AE,SG,US --mix --target 160 --source exa --fresh-wave

# Use the fourth disjoint wave for service, agency, and 2026 funding/accelerator
# searches after the earlier three query sets have been collected.
./automation discover --markets IN,AE,SG,GB,US --mix --target 180 --source exa --conversion-wave

# Audit an additional already-discovered cohort without spending source credits.
./automation audit --limit 60

# Re-run all current policy and lint gates after configuration or code changes.
./automation audit --revalidate

# Generate initial emails, due +3/+10 follow-ups, comments, and reply drafts with Codex.
./automation prepare

# Review at http://127.0.0.1:8765
./automation serve

# For comments, manually add fresh posts from your normal browser in the dashboard,
# then run prepare again. Only those user-supplied posts are eligible.
./automation prepare --email-limit 0 --comment-limit 5

# One short worker cycle. LinkedIn either reports manual items or uses the official API.
./automation worker

# Read-only inspection: no counters, rescheduling, SMTP, or browser actions.
./automation worker --dry-run

# Poll replies, bounces, and opt-outs; launchd normally runs this every 15 minutes.
./automation inbox

# Import a connector-read Gmail snapshot into canonical state. This command only
# reconciles exact scheduled IDs, replies, and bounces; it never sends email.
./automation reconcile-gmail --input gmail-snapshot.json
```

The lead source mix is 50% WhatsApp-heavy service SMBs, 30% agencies/directories, and 20%
funded or early-stage startups. Exa runs through the repository's `config/mcporter.json`
by default. `WRRKHUNT_MCPORTER_CONFIG` can select an alternative file.
Meta Ad Library uses Playwright only on Meta's public Ad Library for India, UAE, and
Singapore. Safe mode disables both the Apify LinkedIn actor and Exa LinkedIn-post search.
LinkedIn post text, author, market, and visible publication time must be copied by the user
from their normal browser into the dashboard. Previously collected source records remain
in history but cannot become new comment drafts. API mode automates approved submission,
not scraping or feed discovery.

Auditing never lowers the fit threshold to fill the queue. Email qualification requires:

- fit score of at least 75;
- URL-backed public-site evidence;
- an address visibly published by the business;
- MX for that exact email domain;
- a permitted regional policy;
- no matching email, registrable-domain, or LinkedIn suppression;
- no initial outreach to that company in the previous 90 days.

Published freemail is accepted only when it appears on the business's own page. UK
freemail, sole traders, unincorporated businesses, and businesses without visible
incorporation evidence are blocked. An absent tool is always reported as “not detected,”
never as proof that the business does not use it.

## Approval and release

The dashboard shows evidence, source URLs, audit time, copy, history, source spend,
credential state, and channel stop reasons. Its actions are protected by a local CSRF
token and loopback address check.

The state flow is:

```text
discovered -> audited -> qualified -> drafted -> pending_approval
           -> approved -> scheduled -> sent / posted
```

Terminal states are `blocked`, `rejected`, `suppressed`, `failed`, and `replied`.

Editing a draft clears its approved hash. For email, the hash covers the exact final
delivery preview, including sender identity, disclosure, postal address, and opt-out.
Changing any of those settings after approval makes the worker fail closed. Approval is
valid only for the current content hash and the current approval day. Release re-runs
deterministic linting and schedules 7–15 minute email gaps or 8–15 minute comment gaps in
the recipient's local weekday business hours. Each worker re-runs linting and the hash
check immediately before action. In manual LinkedIn mode, **Open post + copy approved
comment** performs a user-triggered handoff: it opens the exact permalink in the normal
browser and copies the immutable approved body. Verify the author and post, paste the
comment, click Post yourself, then use **I posted this exact comment** to record the audit
event. The handoff never reads or modifies LinkedIn's page. In official API mode, release
places the exact hash on the API queue. Missed automated actions are moved to a later valid
local window, not released in a wake-up burst.

Inbound human replies cancel pending follow-ups. Codex may create a reply draft for the
dashboard, but the database rejects any attempt to release a message whose kind is
`reply`. Opt-outs immediately suppress both address and domain.

## Codex-only copy

The copy engine invokes the installed authenticated CLI as:

```text
codex exec --ephemeral --sandbox read-only --output-schema ...
```

Only structured prospect evidence, allowed claims, post content, and the requested
channel are passed. Every returned item must cite evidence IDs. There is no model API,
other model provider, or template fallback. Authentication errors, invalid JSON/schema,
missing evidence, or lint failures produce a blocked item.

Initial-email checks use the `founder_booking_note_v4` style: 60–80 pitch words, a
specific three-to-five-word non-promotional subject, a direct evidence-backed opening,
one modest handoff hypothesis, first-person founding-engineer language, and one
15-minute tailored-demo question. The exact configured booking URL must appear once in
an initial email and no other URL is allowed. Guessed names, support/recruiting/legal
inboxes, retired campaign phrasing, banned punctuation, and forbidden product claims are
blocked deterministically. Follow-ups retain the wider 60–95-word bound and are link-free.
Comments must be 40–250 characters, specific to the immutable post text, non-promotional,
and link-free.

## Channel stops and caps

- Email: the persisted `email_daily_cap` (20 on a fresh install), enforced across retries
  and restarts. Runtime overrides are private campaign configuration and must never be
  inferred from repository documentation.
- LinkedIn: five API attempts or user-confirmed manual comments per day, persisted across
  restarts.
- One initial recipient per company and a 90-day company cooldown.
- One comment per post and a 14-day author cooldown.
- Two bounces among the rolling last 20 sent emails pause email.
- Any Gmail authentication/quota failure or uncertain SMTP state pauses email.
- wrrkhunt performs no authenticated LinkedIn page reads and no browser submission.
  Manual mode is visually verified by the user; API mode uses only LinkedIn's official
  endpoints and the actor authorized by OAuth.
- An email action is transactionally marked before SMTP submission. A process crash leaves
  an uncertain attempt that is terminally blocked, never retried.
- Email and LinkedIn remain independent unless the emergency stop is active.

Use these controls at any time:

```bash
./automation pause email
./automation pause linkedin
./automation pause all
./automation resume email
./automation status
./automation health all
```

The emergency stop and its explicit clear action are also in the dashboard. Clearing it
leaves both channels paused until individually resumed.

## launchd

After controlled inbox and private-post seed tests pass, install the user LaunchAgents:

```bash
./automation install-launchd
```

With a Gmail app password, this installs five agents under `~/Library/LaunchAgents/`:

- dashboard on login;
- worker every five minutes;
- IMAP polling every fifteen minutes;
- discovery at 07:30 each weekday;
- preparation at 09:00 each weekday.

When delivery is using the interactive Gmail connector and no app password is in
Keychain, only dashboard, discovery, and preparation are installed. This deliberately
omits SMTP and IMAP agents so the same approved message cannot be sent twice. The agent
code is staged under `~/Library/Application Support/wrrkhunt/launchd-runtime/` because
macOS can block background Python from reading a repository beneath Documents.
Installation captures the active NVM `node`/`mcporter` paths and initializes the private
runtime snapshot as its own Git worktree, allowing `codex exec` to keep its repository
safety check enabled.
If SMTP is enabled later, messages carrying a successful `gmail_scheduled` event remain
owned by Gmail and are excluded from both SMTP delivery and overdue rescheduling.

Remove them with `./automation uninstall-launchd`. Logs stay in the private runtime home.

## Verification

```bash
.venv/bin/python -W error::ResourceWarning -m unittest discover -v
```

The suite covers normalization, regional gates, suppression, state transitions,
immutable final-delivery hashes, cross-run pacing, crash-safe attempts, daily caps,
timezone scheduling, Exa parsing, verified TLS defaults,
legacy counts, mocked Codex output, fake SMTP/IMAP, dashboard loopback/CSRF checks, and
proof that no SMTP/API action starts without setup and approval, that manual LinkedIn mode
never submits, and that official API failures pause without browser fallback or blind retry.

Before production, complete the three controlled inbox-provider deliveries. For LinkedIn,
test the selected mode on a private/test post and stay within the access tier and use case
LinkedIn approved for the developer application. Then approve and release at most 20 fresh
emails and at most five comments from the dashboard. Installing launchd or configuring
credentials does not itself approve or release anything.

The compliance gates implement the operational constraints in the current FTC CAN-SPAM
guide and ICO B2B marketing guidance. They are safeguards, not legal advice; review the
policy for each market before enabling a campaign, especially while ICO guidance is
being revised.

## Isolated UAE phone prospecting

The phone-enriched UAE run is intentionally separate from the campaign lifecycle. It
opens the canonical database with SQLite `mode=ro` and `PRAGMA query_only=ON`, stages all
working evidence beneath the private application-support directory, and never imports a
lead or contacts anyone.

```bash
./automation prospect-phones \
  --market AE \
  --target 100 \
  --angle ai-whatsapp \
  --city-priority Dubai \
  --no-import \
  --output "$HOME/Library/Application Support/wrrkhunt/exports/wrrkhunt_uae_phone_leads_2026-08-28.csv"
```

The command re-audits first-party websites, validates UAE numbers with `phonenumbers`,
requires public business-use evidence and fit score 75 or higher, checks suppressions and
prior actions, and writes a ranked CSV plus a `_rejected.csv` audit file with permissions
`0600`. A low-confidence JavaScript audit fails closed unless a separately recorded
browser verification is supplied. Optional Apollo evidence can corroborate an already
public first-party or verified-profile number; it can never establish a phone by itself.

For a formal first-20 review, stage and finalize separately:

```bash
./automation prospect-phones --market AE --target 100 --angle ai-whatsapp \
  --city-priority Dubai --no-import --stage-only --output "/tmp/not-written.csv"
./automation prospect-phones --market AE --target 100 --angle ai-whatsapp \
  --city-priority Dubai --no-import --finalize-stage "/private/run/stage.json" \
  --output "$HOME/Library/Application Support/wrrkhunt/exports/wrrkhunt_uae_phone_leads_2026-08-28.csv"
```

Finalization refuses to run if the canonical database checksum or audited table counts
changed after staging. No call, WhatsApp message, email, LinkedIn action, or delivery
queue mutation exists in this command path.
