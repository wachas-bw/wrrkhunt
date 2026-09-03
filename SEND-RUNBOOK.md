# Email send runbook

This is the current operator procedure for email delivery. The automation and SQLite
state are authoritative; old batch files and `TODAY.md` are historical records.

## 1. What sending means in this system

An eligible contact is not permission to email. A draft is not permission to email. An
approval from another day is not permission to email. SMTP may act only after the exact
final delivery content hash has been approved today and explicitly released from the
loopback dashboard.

The final hash includes the subject, body, recipient, sender identity, disclosure,
business postal address, opt-out text, and evidence references. Editing any of these
invalidates approval.

## 2. One-time setup

Create the environment and initialize the private database:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./automation init
```

Configure Gmail with a valid business postal address:

```bash
./automation setup gmail --postal-address "VALID BUSINESS POSTAL ADDRESS"
./automation health email
```

The command asks for the 16-character Gmail app password without echoing it and stores
it in macOS Keychain. `GMAIL_APP_PASSWORD` is a temporary fallback only. Never put the
password in Git, a prompt, a log, or a campaign file.

Before production, send controlled test messages to inboxes you own at Gmail, Outlook,
and Yahoo. Confirm authentication, formatting, replies, bounces, opt-out handling, and
the expected inbox placement manually. Setup alone never unpauses, approves, releases,
or sends campaign messages.

## 3. Start-of-day preflight

```bash
git status --short
./automation status
./automation health email
./automation inbox
./automation worker --channel email --dry-run
```

Resolve any human reply, opt-out, bounce stop, authentication failure, quota failure, or
uncertain delivery before preparing more mail. Do not clear a stop by editing SQLite.

Email qualification requires fit score at least 75, usable first-party evidence, an
email visibly published by the business, MX for the exact email domain, enabled regional
policy, no suppression, one initial recipient per company, and no initial email to the
same registrable domain inside 90 days.

UK contacts require visible corporate incorporation evidence and cannot use freemail.
US delivery is blocked until the business postal address is configured.

## 4. Prepare, inspect, and approve

```bash
./automation prepare --email-limit 20 --comment-limit 0
./automation serve
```

Review at `http://127.0.0.1:8765`. For every email, inspect:

- company, market, pool, fit score, and confidence;
- source URL and discovery excerpt;
- website-audit URLs and evidence excerpts;
- publication URL for the exact recipient address;
- subject, body, approved product claim, and evidence IDs;
- final delivery preview, including compliance footer and opt-out;
- previous domain activity and suppression state.

Reject the item if any claim cannot be defended from its displayed evidence. Edit only
when needed; an edit returns the item to `pending_approval` and requires a new approval.

## 5. Current initial-email rules

The `founder_booking_note_v4` validator requires:

- a concrete, non-promotional subject of three to five words;
- the safe greeting `Hi there,`;
- 60 to 80 pitch words in two or three short paragraphs;
- one specific public evidence detail;
- one restrained 15-minute tailored-demo question;
- the exact configured booking URL once and no other URL;
- `Wachas` and `Founding engineer, wrrk.ai` on separate sender lines;
- no guessed name, unsupported claim, banned punctuation, wrong-audience inbox, HTML,
  tracking pixel, image signature, CC, BCC, or attachment.

Allowed and forbidden product claims are defined in `prospecting/config.py`. Do not sell
features merely because they appeared in an old campaign document. In particular, do
not claim an AI meeting notetaker, six-platform lead discovery, Ziwo voice, a public REST
API, Meta/Google Ads orchestration, or a generic `$400/mo` replacement figure.

The detector's stack-cost estimate is directional and must never be quoted as the
prospect's actual spend. Name the detected tool and source page instead.

## 6. Release and pacing

Release reviewed items in the dashboard. The fresh-install cap is 20 email attempts per
approved day. A failure or retry cannot bypass the persisted counter. The scheduler uses
random 7 to 15 minute gaps and recipient-local weekday business hours. Overflow moves to
the next valid window; it is never released as a catch-up burst after a late wake-up.

Run one short cycle manually only when needed:

```bash
./automation worker --channel email
```

Do not use a second sender, Gmail UI schedule, connector action, or another process for
the same IDs unless ownership is explicitly reconciled. A successful
`gmail_scheduled` event means Gmail owns that item and SMTP excludes it.

An outbound attempt is transactionally marked before SMTP. If the process loses certainty
after submission, the item is blocked instead of retried blindly.

## 7. Follow-ups and human replies

The current cadence is:

| Day | Action | Approval |
|---:|---|---|
| 0 | Initial evidence-led email with the approved booking URL | Same-day approval and release |
| +3 | Evidence-grounded follow-up in the same thread, no link | New approval on the due day |
| +10 | Final concise follow-up in the same thread, no link | New approval on the due day |
| after +10 | Stop | No further automated touch |

`./automation prepare` creates only follow-ups that are due. Any human reply cancels
pending follow-ups. Codex may draft a response for review, but database policy prohibits
automatic release of reply-kind messages.

Match the reply's tone and length, answer the actual question, and do not force the
booking link into a human conversation.

## 8. Bounces, opt-outs, and stop conditions

Poll IMAP every 15 minutes through launchd or run:

```bash
./automation inbox
```

The system records human replies, automated replies, delivery failures, and opt-outs.
An opt-out immediately suppresses the email and registrable domain. Two bounces in the
rolling last 20 sent messages pause email. Gmail authentication/quota failures and
uncertain SMTP results also pause the channel.

Use these controls rather than altering records:

```bash
./automation pause email
./automation status
./automation health email
./automation resume email
```

The dashboard emergency stop has a separate explicit clear action. Clearing it leaves
channels paused until they are individually resumed.

## 9. What can and cannot be measured

The canonical record can report:

- approved, scheduled, attempted, sent, and failed counts;
- SMTP/Gmail external IDs and timestamps when available;
- bounces and their messages;
- human and automated replies;
- opt-outs and resulting suppressions;
- follow-up stage and cancellation;
- positive replies and calls booked when classified by a human.

The system deliberately has no tracking pixel, HTML beacon, or hidden link redirect. It
therefore cannot report opens. A message without a bounce is not proof of inbox placement
or reading. Do not put `opened` in a team report unless a recipient explicitly confirms
it through a reply or another legitimate event.

## 10. End-of-day report

Use database facts, not estimates. Report:

```text
Prospecting update: [N] candidates discovered, [N] audited, [N] qualified, [N] emails
sent from wachas@wrrk.ai, [N] replies, [N] positive replies, [N] bounces, [N] opt-outs,
and [N] calls booked. [N] approved items remain scheduled in recipient-local windows.
Email channel: [healthy/paused, reason].
```

Do not report queued or drafted messages as sent.

## 11. Compliance note

Every delivered message contains accurate sender identity, the configured business
postal address, a commercial-message disclosure, a reply-based opt-out, and a
`List-Unsubscribe` mailto header. These controls support responsible operation but are
not legal advice. Recheck current requirements for each market before enabling a new
campaign. See the FTC CAN-SPAM and ICO B2B guidance linked from `AUTOMATION.md`.
