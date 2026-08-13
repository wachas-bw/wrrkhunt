#!/usr/bin/env python3
"""Create real Gmail drafts from batch1.json, via IMAP APPEND.

Why IMAP and not browser automation: Gmail's compose DOM is obfuscated and its
contenteditable body is flaky to drive, whereas IMAP APPEND to the Drafts folder is a
documented operation that either works or returns an error. It also runs headless, so
the daily 20-send loop in SEND-RUNBOOK.md stays a one-command job.

Nothing here SENDS. Every message lands in Drafts for you to read and send by hand,
which is deliberate: see clienthunt/TODAY.md for what happened when sending was
automated away from human judgement (it was not, and 12 warm leads went cold instead,
but the same principle applies in reverse. A cold email you did not read before sending
is a liability).

SETUP (one time, ~2 minutes)
---------------------------
Google Workspace blocks plain-password IMAP, so you need an App Password:

  1. 2-Step Verification must be ON for wachas@wrrk.ai
     https://myaccount.google.com/signinoptions/two-step-verification
  2. Create an App Password ("Mail" / "Mac")
     https://myaccount.google.com/apppasswords
  3. Enable IMAP in Gmail: Settings > See all settings > Forwarding and POP/IMAP
     > Enable IMAP > Save Changes
  4. Export it (do not paste it into a file in this repo):
     export GMAIL_APP_PASSWORD='xxxx xxxx xxxx xxxx'

USAGE
-----
    python3 wrrkhunt/outreach/gmail_drafts.py --dry-run     # build, validate, write .eml
    python3 wrrkhunt/outreach/gmail_drafts.py               # create the Gmail drafts
    python3 wrrkhunt/outreach/gmail_drafts.py --limit 5

--dry-run needs no credentials at all and still writes .eml files to outreach/eml/,
which you can drag straight into Gmail if you would rather not use an App Password.
"""
from __future__ import annotations

import argparse
import imaplib
import json
import os
import ssl
import sys
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

HERE = os.path.dirname(os.path.abspath(__file__))
BATCH = os.path.join(HERE, "batch1.json")
EML_DIR = os.path.join(HERE, "eml")

FROM_ADDR = "wachas@wrrk.ai"
FROM_NAME = "Wachas"
IMAP_HOST = "imap.gmail.com"
DRAFTS = '"[Gmail]/Drafts"'

SIGNATURE = "\n\nWachas\nwrrk.ai"


def build_message(d: dict) -> EmailMessage:
    """Plain text only. No HTML part, no tracking pixel, no links in the body.

    That is not minimalism for its own sake: wrrk.ai's domain reputation is fragile
    (new/docs/WRRK_AI_WARMUP_RUNBOOK.md), and a multipart HTML cold email with a
    tracked link is the single easiest way to land in Promotions or Spam.
    """
    m = EmailMessage()
    m["From"] = f"{FROM_NAME} <{FROM_ADDR}>"
    m["To"] = d["to"]
    m["Subject"] = d["subject"]
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain="wrrk.ai")
    # body already ends with the sender name from draft.py; append the domain line only
    body = d["body"]
    if not body.rstrip().endswith("wrrk.ai"):
        body = body.rstrip() + "\nwrrk.ai"
    m.set_content(body)
    return m


def validate(d: dict) -> list[str]:
    """Re-run the gates here too. draft.py already linted, but a hand edit to
    batch1.json between drafting and sending would otherwise slip through unchecked."""
    errs = list(d.get("errors") or [])
    if d.get("to", "UNKNOWN") == "UNKNOWN" or "@" not in d.get("to", ""):
        errs.append("no valid recipient")
    for ch in ("—", "–", "·", "•"):
        if ch in d["body"] or ch in d["subject"]:
            errs.append(f"banned character {ch!r}")
    if not d.get("mx"):
        errs.append("recipient domain has no MX")
    return errs


def write_eml(msgs: list[tuple[dict, EmailMessage]]) -> None:
    os.makedirs(EML_DIR, exist_ok=True)
    for d, m in msgs:
        safe = d["domain"].replace(".", "_")
        with open(os.path.join(EML_DIR, f"{safe}.eml"), "wb") as f:
            f.write(bytes(m))
    print(f"  wrote {len(msgs)} .eml files to {EML_DIR}")
    print("  (drag these into Gmail if you prefer not to use an App Password)")


def append_drafts(msgs: list[tuple[dict, EmailMessage]], pw: str) -> None:
    ctx = ssl.create_default_context()
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, ssl_context=ctx)
        imap.login(FROM_ADDR, pw)
    except imaplib.IMAP4.error as e:
        sys.exit(
            f"IMAP login failed: {e}\n\n"
            "Most likely causes, in order:\n"
            "  1. GMAIL_APP_PASSWORD is a normal account password. It must be a\n"
            "     16-character App Password from https://myaccount.google.com/apppasswords\n"
            "  2. IMAP is not enabled in Gmail settings (Forwarding and POP/IMAP).\n"
            "  3. Workspace admin policy blocks IMAP or app passwords for this account.\n"
            "     Then use --dry-run and drag the .eml files in instead."
        )

    ok = 0
    for d, m in msgs:
        raw = bytes(m)
        typ, resp = imap.append(DRAFTS, "\\Draft", imaplib.Time2Internaldate(time.time()), raw)
        if typ == "OK":
            ok += 1
            print(f"  drafted -> {d['to']:<34} {d['subject']}")
        else:
            print(f"  FAILED  -> {d['to']}: {resp}", file=sys.stderr)
    imap.logout()
    print(f"\n{ok}/{len(msgs)} drafts created in Gmail.")
    print("Open Gmail > Drafts. Read each one before sending. 20/day maximum.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and write .eml only, no credentials needed")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    if not os.path.exists(BATCH):
        sys.exit(f"No {BATCH}. Run draft.py first.")
    drafts = json.load(open(BATCH))

    ready, blocked = [], []
    for d in drafts:
        errs = validate(d)
        (blocked if errs else ready).append((d, errs))

    if a.limit:
        ready = ready[:a.limit]

    print(f"{len(ready)} ready, {len(blocked)} blocked\n")
    for d, errs in blocked:
        print(f"  BLOCKED {d['domain']}: {'; '.join(errs)}")
    if blocked:
        print()
    if not ready:
        sys.exit("Nothing to draft.")

    msgs = [(d, build_message(d)) for d, _ in ready]
    write_eml(msgs)

    if a.dry_run:
        print("\nDry run. No Gmail drafts created.")
        print("Preview of the first message:\n")
        print("-" * 68)
        print(bytes(msgs[0][1]).decode("utf-8", "replace")[:900])
        print("-" * 68)
        return

    pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not pw:
        sys.exit(
            "\nGMAIL_APP_PASSWORD is not set, so no Gmail drafts were created.\n"
            "The .eml files above are ready to drag into Gmail regardless.\n\n"
            "To create drafts directly:\n"
            "  1. https://myaccount.google.com/apppasswords  (needs 2FA on)\n"
            "  2. Gmail settings > Forwarding and POP/IMAP > Enable IMAP\n"
            "  3. export GMAIL_APP_PASSWORD='xxxx xxxx xxxx xxxx'\n"
            "  4. re-run this script"
        )
    print()
    append_drafts(msgs, pw)


if __name__ == "__main__":
    main()
