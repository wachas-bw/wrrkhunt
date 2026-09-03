"""Codex-only structured copy generation and deterministic post-generation linting."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from .config import BOOKING_URL, REPO_ROOT, SENDER_NAME, SENDER_TITLE
from .db import Database, StateError
from .policy import initial_outreach_gate
from .util import delivery_content_hash, evidence_excerpt, iso, parse_iso, utcnow

BANNED_CHARS = {"—": "em dash", "–": "en dash", "·": "middle dot", "•": "bullet",
                "‘": "smart quote", "’": "smart quote", "“": "smart quote", "”": "smart quote"}
URL_RE = re.compile(r"(?:https?://|www\.|\b[a-z0-9-]+\.(?:com|ai|in|ae|sg|co\.uk)/)", re.I)
MONEY_RE = re.compile(r"(?:[$£€]\s*\d|\b\d+\s*(?:dollars?|pounds?|percent|%)\b)", re.I)
WRONG_CLAIM_RE = re.compile(
    r"(?:meeting notetaker|six platforms|ziwo|rest api|meta ads orchestration|"
    r"google ads orchestration|replace[sd]? \$?400|guarantee|save \d+|increase .+%)", re.I,
)
STALE_SUBJECT_RE = re.compile(
    r"\b(?:workflow|inquir(?:y|ies)|enquir(?:y|ies)|coordinating|connecting|bringing|"
    r"follow[ -]?ups?)\b", re.I,
)
PROMOTIONAL_SUBJECT_RE = re.compile(
    r"(?:[$£€%]|\b(?:discount|sale|offer|limited[ -]?time|free|deal|promo(?:tion)?)\b)", re.I,
)
STALE_BODY_RE = re.compile(
    r"\b(?:I noticed|I saw|That mix can|That can leave|gives small teams one workspace|"
    r"one workspace for customer conversations|may become scattered|"
    r"may be harder to keep together|work behind each response|"
    r"would you be open to a 15[ -]minute|based on this public)\b", re.I,
)
FOUNDER_VOICE_RE = re.compile(r"\b(?:I am|I'm) building wrrk\.ai\b", re.I)
PROMOTIONAL_COMMENT_RE = re.compile(
    r"\b(?:wrrk(?:\.ai)?|our (?:tool|product|platform)|book a call|demo|dm me|we help|try us)\b", re.I,
)
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'/-]*")
BOOKING_COPY_STYLE = "founder_booking_note_v4"
LEGACY_COPY_STYLE = "founder_plain_note_v3"
STOPWORDS = {
    "about", "after", "again", "also", "and", "are", "been", "being", "but", "can",
    "could", "for", "from", "have", "into", "just", "more", "most", "not", "our",
    "that", "the", "their", "them", "then", "there", "this", "those", "through", "too",
    "very", "was", "were", "what", "when", "where", "which", "while", "with", "would",
    "you", "your",
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["request_id", "subject", "body", "evidence_ids"],
                "properties": {
                    "request_id": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
                },
            },
        }
    },
}


@dataclass
class CopyRequest:
    request_id: str
    channel: str
    kind: str
    prospect_id: int | None
    campaign_id: int
    contact_id: int | None
    post_id: int | None
    to_address: str
    evidence: list[dict[str, Any]]
    context: dict[str, Any]
    parent_message_id: int | None = None
    thread_id: str | None = None
    replace_message_id: int | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "channel": self.channel,
            "kind": self.kind,
            "recipient": self.to_address,
            "prospect": self.context,
            "evidence": self.evidence,
        }


class CodexCopyError(RuntimeError):
    pass


def find_codex() -> str:
    direct = shutil.which("codex")
    if direct:
        return direct
    matches = sorted(Path.home().glob(".vscode/extensions/openai.chatgpt-*/bin/*/codex"), reverse=True)
    if matches:
        return str(matches[0])
    raise CodexCopyError("Codex CLI was not found")


def _prompt(requests: list[CopyRequest], settings: dict[str, Any]) -> str:
    copy_style = str(settings.get("email_copy_style") or LEGACY_COPY_STYLE)
    booking_url = str(settings.get("email_booking_url") or BOOKING_URL).strip()
    rules = {
        "identity": {"name": SENDER_NAME, "title": SENDER_TITLE},
        "allowed_product_claims": settings.get("allowed_product_claims", []),
        "forbidden_product_claims": settings.get("forbidden_claims", []),
        "email": [
            "Use only supplied evidence and allowed product claims.",
            "Write like a founding engineer sending a plain personal note after doing real homework. It must not read like a campaign template.",
            "Return a truthful, natural 3 to 5 word subject built around a tangible supplied detail. Do not use workflow, inquiry, enquiry, connecting, coordinating, bringing, or follow-up in the subject. Do not mimic a promotion or use a price, discount, offer, free, or limited-time language.",
            "Body format: Hi there, then a 60 to 80 word pitch in two or three short paragraphs, then exactly Wachas and Founding engineer, wrrk.ai on separate lines.",
            "Open directly with one concrete supplied fact in plain language. Never open with I noticed, I saw, Your site, a compliment, or an introduction.",
            "Use the next sentence to make one modest, prospect-specific hypothesis about a handoff or response problem. Do not claim the process is broken and do not use may become scattered, harder to keep together, or work behind each response.",
            "Say I am building wrrk.ai or I'm building wrrk.ai, then express only the allowed benefit in natural first-person language. Mention wrrk.ai only once in the pitch and do not list features.",
            "Finish with one short, low-pressure question offering a 15-minute tailored demo using the concrete supplied contact, quote, booking, or service flow. Do not say would you be open, based on this public process, or public workflow.",
            f"For initial emails using style {BOOKING_COPY_STYLE}, include this exact booking URL once after the tailored-demo question: {booking_url}",
            "Do not include any other URL. Follow-ups must remain link-free.",
            "Make the subject and opening attention-worthy through a concrete supplied detail, not hype, clickbait, urgency, or unsupported claims.",
            "Vary sentence construction and wording across the batch. Apart from the sender identity and required CTA terms, do not repeat a sentence or an eight-word phrase between items.",
            "Do not use a guessed personal name, price, tracking language, or unsupported result claim.",
            "Use ASCII punctuation only. Never use em dash, en dash, middle dot, bullets, or curly quotation marks.",
        ],
        "linkedin": [
            "Return subject as an empty string and body as one comment of 40 to 250 characters.",
            "Be specific to the supplied post, thoughtful, non-promotional, and link-free.",
            "Do not mention wrrk, a demo, a service, or ask the author to contact us.",
        ],
        "evidence": "Every item must list only the integer evidence IDs actually used.",
        "email_copy_style": copy_style,
        "approved_booking_url": booking_url,
    }
    return (
        "You write restrained, evidence-grounded B2B outreach for the supplied batch. "
        "Return only data matching the provided output schema. Never invent a name, fact, metric, "
        "customer result, or product capability.\n\n"
        + json.dumps({"rules": rules, "requests": [r.payload() for r in requests]},
                     ensure_ascii=False, indent=2)
    )


def run_codex(requests: list[CopyRequest], settings: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
    if not requests:
        return {"items": []}
    codex = find_codex()
    with tempfile.TemporaryDirectory(prefix="wrrkhunt-copy-") as tmp:
        schema_path = Path(tmp) / "schema.json"
        output_path = Path(tmp) / "output.json"
        schema_path.write_text(json.dumps(OUTPUT_SCHEMA))
        command = [
            codex, "exec", "--ephemeral", "--sandbox", "read-only",
            "--output-schema", str(schema_path), "--output-last-message", str(output_path), "-",
        ]
        try:
            result = subprocess.run(command, input=_prompt(requests, settings), cwd=REPO_ROOT,
                                    capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise CodexCopyError("Codex copy generation timed out") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-1200:]
            raise CodexCopyError(f"Codex authentication or execution failed: {detail}")
        if not output_path.exists():
            raise CodexCopyError("Codex did not write structured output")
        try:
            value = json.loads(output_path.read_text())
        except json.JSONDecodeError as exc:
            raise CodexCopyError("Codex output was not valid JSON") from exc
        if not isinstance(value, dict) or not isinstance(value.get("items"), list):
            raise CodexCopyError("Codex output did not match the required schema")
        return value


def _pitch_words(body: str) -> int:
    lines = [line.strip() for line in body.strip().splitlines()]
    if lines and lines[0].lower() == "hi there,":
        lines = lines[1:]
    if len(lines) >= 2 and lines[-2:] == [SENDER_NAME, SENDER_TITLE]:
        lines = lines[:-2]
    return len(WORD_RE.findall(" ".join(lines)))


def _thread_subject(subject: str) -> str:
    base = re.sub(r"^(?:re:\s*)+", "", str(subject or "").strip(), flags=re.I)
    return f"Re: {base}" if base else "Re: Your message"


def lint_email(subject: str, body: str, evidence_ids: list[int], allowed_ids: set[int],
               to_address: str, kind: str = "initial", *,
               copy_style: str = LEGACY_COPY_STYLE,
               booking_url: str = BOOKING_URL) -> list[str]:
    errors: list[str] = []
    subject_words = len(WORD_RE.findall(subject))
    max_subject_words = 5 if kind == "initial" else 9
    if not subject.strip() or not 3 <= subject_words <= max_subject_words:
        errors.append(f"subject must contain 3 to {max_subject_words} words")
    if kind == "initial" and subject.lower().startswith("re:"):
        errors.append("initial subject cannot imply an existing thread")
    if kind == "initial" and STALE_SUBJECT_RE.search(subject):
        errors.append("initial subject uses the retired formulaic style")
    if kind == "initial" and PROMOTIONAL_SUBJECT_RE.search(subject):
        errors.append("initial subject must not mimic promotional copy")
    if not body.strip().startswith("Hi there,"):
        errors.append("body must use the safe greeting 'Hi there,'")
    if not body.rstrip().endswith(f"{SENDER_NAME}\n{SENDER_TITLE}"):
        errors.append("sender identity is missing or inaccurate")
    count = _pitch_words(body)
    max_pitch_words = 80 if kind == "initial" else 95
    if not 60 <= count <= max_pitch_words:
        errors.append(f"pitch is {count} words; expected 60 to {max_pitch_words}")
    if kind == "initial" and copy_style == BOOKING_COPY_STYLE:
        if not booking_url or body.count(booking_url) != 1:
            errors.append("booking-link copy must contain the approved booking URL exactly once")
        remaining = body.replace(booking_url, "") if booking_url else body
        if URL_RE.search(remaining) or URL_RE.search(subject):
            errors.append("booking-link copy contains an additional or unapproved link")
    elif URL_RE.search(body) or URL_RE.search(subject):
        errors.append("first-touch copy must not contain links")
    if "?" not in body or not re.search(r"15[ -]minute", body, re.I) or not re.search(r"tailored demo", body, re.I):
        errors.append("body needs one clear 15-minute tailored-demo question")
    if body.count("?") != 1:
        errors.append("body must contain exactly one question")
    for char, name in BANNED_CHARS.items():
        if char in subject or char in body:
            errors.append(f"banned {name}")
    if WRONG_CLAIM_RE.search(body) or MONEY_RE.search(body):
        errors.append("copy contains an unsupported or forbidden claim")
    if kind == "initial" and STALE_BODY_RE.search(body):
        errors.append("body uses the retired formulaic style")
    if kind == "initial" and not FOUNDER_VOICE_RE.search(body):
        errors.append("body must use a direct founding-engineer voice")
    if not evidence_ids or not set(evidence_ids).issubset(allowed_ids):
        errors.append("copy references missing or unauthorized evidence IDs")
    local = to_address.split("@", 1)[0].lower()
    if re.match(
        r"(?:careers?|jobs?|hr|recruit|hiring|no[._-]?reply|support|help|privacy|legal|"
        r"billing|accounts?|abuse|security)(?:$|[._+-])",
        local,
    ):
        errors.append("wrong-audience or unreplyable inbox")
    return sorted(set(errors))


def lint_comment(body: str, evidence_ids: list[int], allowed_ids: set[int],
                 post_text: str) -> list[str]:
    errors: list[str] = []
    length = len(body.strip())
    if not 40 <= length <= 250:
        errors.append(f"comment is {length} characters; expected 40 to 250")
    if "\n" in body.strip():
        errors.append("comment must be one paragraph")
    if URL_RE.search(body):
        errors.append("comment contains a link")
    if PROMOTIONAL_COMMENT_RE.search(body):
        errors.append("comment is promotional")
    for char, name in BANNED_CHARS.items():
        if char in body:
            errors.append(f"banned {name}")
    if not evidence_ids or not set(evidence_ids).issubset(allowed_ids):
        errors.append("comment references missing or unauthorized evidence IDs")
    comment_words = {x.lower() for x in WORD_RE.findall(body) if len(x) >= 5 and x.lower() not in STOPWORDS}
    post_words = {x.lower() for x in WORD_RE.findall(post_text) if len(x) >= 5 and x.lower() not in STOPWORDS}
    if not comment_words.intersection(post_words):
        errors.append("comment is not specific enough to the post")
    return sorted(set(errors))


def lint_reply(subject: str, body: str, evidence_ids: list[int], allowed_ids: set[int]) -> list[str]:
    errors: list[str] = []
    if not subject.strip():
        errors.append("reply subject is empty")
    if not body.strip().startswith("Hi there,"):
        errors.append("reply must use the safe greeting 'Hi there,'")
    if not body.rstrip().endswith(f"{SENDER_NAME}\n{SENDER_TITLE}"):
        errors.append("sender identity is missing or inaccurate")
    count = _pitch_words(body)
    if not 20 <= count <= 140:
        errors.append(f"reply is {count} words; expected 20 to 140")
    if URL_RE.search(body) or WRONG_CLAIM_RE.search(body) or MONEY_RE.search(body):
        errors.append("reply contains a link or unsupported claim")
    if not evidence_ids or not set(evidence_ids).issubset(allowed_ids):
        errors.append("reply references missing or unauthorized evidence IDs")
    for char, name in BANNED_CHARS.items():
        if char in body or char in subject:
            errors.append(f"banned {name}")
    return sorted(set(errors))


def _evidence_for_prospect(db: Database, prospect_id: int, limit: int = 5) -> list[dict[str, Any]]:
    rows = db.rows(
        "SELECT id,kind,source_url,excerpt,observed_value,confidence,detected_at FROM evidence "
        "WHERE prospect_id=? ORDER BY CASE confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,id DESC LIMIT ?",
        (prospect_id, limit),
    )
    return [dict(row) for row in rows]


def _copy_safe_audit(metadata: dict[str, Any]) -> dict[str, Any]:
    """Exclude estimates and internal scoring details from the generation prompt."""
    audit = metadata.get("audit") if isinstance(metadata.get("audit"), dict) else {}
    channels = audit.get("channels") if isinstance(audit.get("channels"), dict) else {}
    return {
        "fit_why": str(audit.get("fit_why") or ""),
        "gaps": [str(item) for item in audit.get("gaps", []) if isinstance(item, str)],
        "channels": {
            key: channels.get(key) for key in (
                "whatsapp", "instagram", "facebook", "linkedin", "twitter",
                "emails", "phone", "contact_form", "live_chat",
            ) if key in channels
        },
        "tools": [
            {"name": str(item.get("name") or ""), "category": str(item.get("category") or "")}
            for item in audit.get("tools", []) if isinstance(item, dict) and item.get("name")
        ],
    }


def email_requests(db: Database, limit: int = 20) -> list[CopyRequest]:
    if limit <= 0:
        return []
    settings = db.settings()
    prospects = db.rows(
        "SELECT p.*,c.id AS contact_id,c.email,c.normalized_email,c.kind AS contact_kind,"
        "c.published_url,c.evidence_excerpt,c.is_published,c.mx_available "
        "FROM prospects p JOIN campaigns ca ON ca.id=p.campaign_id "
        "JOIN contacts c ON c.prospect_id=p.id AND c.is_primary=1 "
        "WHERE ca.name='fresh' AND p.status='qualified' AND NOT EXISTS ("
        "SELECT 1 FROM messages m WHERE m.prospect_id=p.id AND m.channel='email' AND m.kind='initial') "
        "ORDER BY p.fit_score DESC,p.qualified_at", (),
    )
    requests = []
    for row in prospects:
        contact = dict(row)
        errors = initial_outreach_gate(db, row["id"], contact, settings)
        if errors:
            continue
        evidence = _evidence_for_prospect(db, row["id"])
        if not evidence:
            continue
        metadata = json.loads(row["metadata_json"] or "{}")
        requests.append(CopyRequest(
            request_id=f"email:{row['id']}", channel="email", kind="initial",
            prospect_id=row["id"], campaign_id=row["campaign_id"], contact_id=row["contact_id"],
            post_id=None, to_address=row["normalized_email"], evidence=evidence,
            context={"company": row["company"], "market": row["market"], "pool": row["pool"],
                     "fit_score": row["fit_score"], "audit": _copy_safe_audit(metadata)},
        ))
        if len(requests) >= limit:
            break
    return requests


def email_rewrite_requests(db: Database, message_ids: list[int]) -> list[CopyRequest]:
    """Build evidence-grounded requests for unsent initial-email rewrites."""
    ids = list(dict.fromkeys(int(value) for value in message_ids))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = db.rows(
        "SELECT m.id AS message_id,m.campaign_id,m.prospect_id,m.contact_id,m.to_address,"
        "m.status,p.company,p.market,p.pool,p.fit_score,p.metadata_json "
        "FROM messages m JOIN prospects p ON p.id=m.prospect_id "
        f"WHERE m.id IN ({placeholders}) AND m.channel='email' AND m.kind='initial' "
        "AND (m.status IN ('drafted','pending_approval','approved','scheduled') OR "
        "(m.status='blocked' AND m.body LIKE 'Generation blocked.%' "
        "AND m.last_error LIKE 'Codex %'))",
        ids,
    )
    by_id = {int(row["message_id"]): row for row in rows}
    requests: list[CopyRequest] = []
    for message_id in ids:
        row = by_id.get(message_id)
        if not row:
            continue
        evidence = _evidence_for_prospect(db, int(row["prospect_id"]))
        if not evidence:
            continue
        metadata = json.loads(row["metadata_json"] or "{}")
        requests.append(CopyRequest(
            request_id=f"rewrite:{message_id}", channel="email", kind="initial",
            prospect_id=int(row["prospect_id"]), campaign_id=int(row["campaign_id"]),
            contact_id=int(row["contact_id"]) if row["contact_id"] else None,
            post_id=None, to_address=row["to_address"], evidence=evidence,
            context={
                "company": row["company"], "market": row["market"], "pool": row["pool"],
                "fit_score": row["fit_score"], "audit": _copy_safe_audit(metadata),
            },
        ))
    return requests


def rewrite_email_messages(db: Database, message_ids: list[int],
                           batch_size: int = 10) -> dict[str, Any]:
    """Replace unsent initial copy and invalidate every previous approval."""
    requests = email_rewrite_requests(db, message_ids)
    result: dict[str, Any] = {"rewritten": [], "blocked": {}}
    requested = {int(request.request_id.split(":", 1)[1]) for request in requests}
    for message_id in message_ids:
        if int(message_id) not in requested:
            result["blocked"][int(message_id)] = "message is not an eligible unsent initial email"
    settings = db.settings()
    copy_style = str(settings.get("email_copy_style") or LEGACY_COPY_STYLE)
    booking_url = str(settings.get("email_booking_url") or BOOKING_URL)
    for offset in range(0, len(requests), batch_size):
        batch = requests[offset:offset + batch_size]
        try:
            output = run_codex(batch, settings)
        except CodexCopyError as exc:
            for request in batch:
                message_id = int(request.request_id.split(":", 1)[1])
                result["blocked"][message_id] = str(exc)
            continue
        returned = {
            item.get("request_id"): item for item in output.get("items", [])
            if isinstance(item, dict)
        }
        for request in batch:
            message_id = int(request.request_id.split(":", 1)[1])
            item = returned.get(request.request_id)
            if not item:
                result["blocked"][message_id] = "Codex omitted this rewrite"
                continue
            subject = str(item.get("subject") or "").strip()
            body = str(item.get("body") or "").strip()
            evidence_ids = item.get("evidence_ids") if isinstance(
                item.get("evidence_ids"), list
            ) else []
            evidence_ids = [int(value) for value in evidence_ids if isinstance(value, int)]
            allowed_ids = {int(value["id"]) for value in request.evidence}
            errors = lint_email(
                subject, body, evidence_ids, allowed_ids, request.to_address, "initial",
                copy_style=copy_style, booking_url=booking_url,
            )
            if errors:
                result["blocked"][message_id] = "; ".join(errors)
                continue
            try:
                db.edit_message(
                    message_id, subject, body, evidence_ids, copy_style=copy_style
                )
            except StateError as exc:
                result["blocked"][message_id] = str(exc)
                continue
            result["rewritten"].append(message_id)
    return result


def comment_requests(db: Database, limit: int = 5) -> list[CopyRequest]:
    if limit <= 0:
        return []
    cutoff = iso(utcnow() - timedelta(hours=int(db.setting("post_max_age_hours", 48))))
    campaign = db.row("SELECT id FROM campaigns WHERE name='fresh'")
    if not campaign:
        return []
    chosen = []
    chosen_authors: set[str] = set()
    manual_sources_only = db.setting("linkedin_post_discovery_mode", "manual") == "manual"
    for role, quota in (("prospect", 3), ("influencer", 2)):
        rows = db.rows(
            "SELECT po.* FROM posts po WHERE po.role=? AND po.status='discovered' AND po.market!='' "
            "AND po.author_url!='' "
            "AND (?=0 OR EXISTS (SELECT 1 FROM source_runs safe_sr WHERE safe_sr.id=po.source_run_id "
            "AND safe_sr.source='manual_linkedin')) "
            "AND (po.role!='prospect' OR EXISTS (SELECT 1 FROM prospects qp WHERE qp.id=po.prospect_id "
            "AND qp.fit_score>=? AND qp.status NOT IN ('discovered','audited','blocked','rejected','suppressed','failed','replied'))) "
            "AND po.published_at>=? AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.post_id=po.id) "
            "AND NOT EXISTS (SELECT 1 FROM messages m JOIN posts old ON old.id=m.post_id "
            "WHERE old.author_url=po.author_url AND old.author_url!='' AND m.status IN ('scheduled','posted') "
            "AND COALESCE(m.sent_at,m.scheduled_for,m.created_at)>=?) "
            "ORDER BY po.published_at DESC LIMIT ?",
            (role, int(manual_sources_only), int(db.setting("fit_threshold", 75)), cutoff,
             iso(utcnow() - timedelta(days=int(db.setting("author_cooldown_days", 14)))), quota * 4),
        )
        role_count = 0
        for row in rows:
            author_key = row["author_url"].lower()
            if author_key in chosen_authors or db.is_suppressed("linkedin", author_key):
                continue
            chosen.append(row)
            chosen_authors.add(author_key)
            role_count += 1
            if role_count >= quota:
                break
    requests = []
    for row in chosen[:limit]:
        # Post evidence is stored as an evidence row when tied to a prospect; otherwise
        # use a negative ID namespace local to this request and validate it explicitly.
        if row["prospect_id"]:
            evidence = _evidence_for_prospect(db, row["prospect_id"], 3)
        else:
            evidence = []
        post_evidence_id = -int(row["id"])
        evidence.insert(0, {
            "id": post_evidence_id, "kind": "linkedin_post", "source_url": row["post_url"],
            "excerpt": evidence_excerpt(row["text"], 800), "observed_value": row["text_hash"],
            "confidence": "high", "detected_at": row["published_at"],
        })
        requests.append(CopyRequest(
            request_id=f"comment:{row['id']}", channel="linkedin", kind="comment",
            prospect_id=row["prospect_id"], campaign_id=campaign["id"], contact_id=None,
            post_id=row["id"], to_address=row["author_url"], evidence=evidence,
            context={"author": row["author_name"], "role": row["role"],
                     "post_url": row["post_url"], "post_text": row["text"]},
        ))
    return requests


def followup_requests(db: Database, limit: int = 20) -> list[CopyRequest]:
    """Create only follow-ups that are due now; this forces a fresh daily approval."""
    if limit <= 0:
        return []
    rows = db.rows(
        "SELECT m.*,p.company,p.market,p.pool,p.metadata_json,c.id AS cid "
        "FROM messages m JOIN prospects p ON p.id=m.prospect_id "
        "LEFT JOIN contacts c ON c.id=m.contact_id "
        "WHERE m.channel='email' AND m.kind='initial' AND m.status='sent' "
        "AND p.status!='replied' ORDER BY m.sent_at", (),
    )
    requests: list[CopyRequest] = []
    now = utcnow()
    for row in rows:
        sent_at = parse_iso(row["sent_at"])
        if not sent_at:
            continue
        existing = {
            item["kind"]: item["status"] for item in db.rows(
                "SELECT kind,status FROM messages WHERE parent_message_id=? "
                "AND kind IN ('followup_1','followup_2')", (row["id"],))
        }
        if any(status in {"drafted", "pending_approval", "approved", "scheduled"}
               for status in existing.values()):
            continue
        due = None
        if sent_at + timedelta(days=10) <= now and "followup_2" not in existing:
            due = ("followup_2", 10)
        elif sent_at + timedelta(days=3) <= now and "followup_1" not in existing:
            due = ("followup_1", 3)
        if not due or db.is_suppressed("email", row["to_address"]):
            continue
        kind, days = due
        evidence = _evidence_for_prospect(db, row["prospect_id"])
        if not evidence:
            continue
        requests.append(CopyRequest(
            request_id=f"{kind}:{row['id']}", channel="email", kind=kind,
            prospect_id=row["prospect_id"], campaign_id=row["campaign_id"],
            contact_id=row["contact_id"], post_id=None, to_address=row["to_address"],
            evidence=evidence, parent_message_id=row["id"], thread_id=row["thread_id"],
            context={
                "company": row["company"], "market": row["market"], "pool": row["pool"],
                "original_subject": row["subject"], "original_body": row["body"],
                "followup_day": days, "instruction": "Continue the same thread without adding a link.",
            },
        ))
        if len(requests) >= limit:
            return requests
    return requests


def retryable_followup_requests(db: Database, limit: int = 20) -> list[CopyRequest]:
    """Rebuild requests for transient Codex failures without creating duplicate stages."""
    if limit <= 0:
        return []
    rows = db.rows(
        "SELECT m.*,p.company,p.market,p.pool,p.metadata_json,"
        "parent.subject AS original_subject,parent.body AS original_body,parent.status AS parent_status,"
        "parent.thread_id AS parent_thread_id "
        "FROM messages m JOIN messages parent ON parent.id=m.parent_message_id "
        "JOIN prospects p ON p.id=m.prospect_id "
        "WHERE m.channel='email' AND m.kind IN ('followup_1','followup_2') "
        "AND m.status='blocked' AND m.body LIKE 'Generation blocked.%' "
        "AND m.last_error LIKE 'Codex %' AND parent.status='sent' AND p.status!='replied' "
        "ORDER BY m.id LIMIT ?", (limit,),
    )
    requests: list[CopyRequest] = []
    for row in rows:
        if db.is_suppressed("email", row["to_address"]):
            continue
        evidence = _evidence_for_prospect(db, int(row["prospect_id"]))
        if not evidence:
            continue
        days = 3 if row["kind"] == "followup_1" else 10
        requests.append(CopyRequest(
            request_id=f"{row['kind']}:{row['parent_message_id']}",
            channel="email", kind=row["kind"], prospect_id=int(row["prospect_id"]),
            campaign_id=int(row["campaign_id"]),
            contact_id=int(row["contact_id"]) if row["contact_id"] else None,
            post_id=None, to_address=row["to_address"], evidence=evidence,
            parent_message_id=int(row["parent_message_id"]),
            thread_id=row["thread_id"] or row["parent_thread_id"],
            replace_message_id=int(row["id"]),
            context={
                "company": row["company"], "market": row["market"], "pool": row["pool"],
                "original_subject": row["original_subject"],
                "original_body": row["original_body"], "followup_day": days,
                "instruction": "Continue the same thread without adding a link.",
            },
        ))
    return requests


def inbound_reply_request(db: Database, original_message: dict[str, Any], evidence_id: int,
                          inbound_text: str) -> CopyRequest:
    evidence = _evidence_for_prospect(db, int(original_message["prospect_id"]))
    evidence.insert(0, {
        "id": evidence_id, "kind": "inbound_reply", "source_url": "gmail://inbound",
        "excerpt": evidence_excerpt(inbound_text, 1200), "observed_value": "human reply",
        "confidence": "high", "detected_at": iso(),
    })
    return CopyRequest(
        request_id=f"reply:{original_message['id']}:{evidence_id}", channel="email", kind="reply",
        prospect_id=original_message["prospect_id"], campaign_id=original_message["campaign_id"],
        contact_id=original_message["contact_id"], post_id=None,
        to_address=original_message["to_address"], evidence=evidence,
        parent_message_id=original_message["id"], thread_id=original_message.get("thread_id"),
        context={
            "original_subject": original_message["subject"], "original_body": original_message["body"],
            "inbound_reply": inbound_text,
            "instruction": "Draft a useful human response for review. Do not force a demo CTA.",
        },
    )


def _store_blocked(db: Database, request: CopyRequest, reason: str) -> int:
    settings = db.settings()
    copy_style = (
        str(settings.get("email_copy_style") or LEGACY_COPY_STYLE)
        if request.channel == "email" and request.kind == "initial"
        else LEGACY_COPY_STYLE
    )
    message_id = db.create_message(
        request.campaign_id, request.channel, request.kind,
        "Generation blocked. No content may be released.", prospect_id=request.prospect_id,
        contact_id=request.contact_id, post_id=request.post_id, to_address=request.to_address,
        evidence_ids=[int(x["id"]) for x in request.evidence if int(x["id"]) > 0],
        parent_message_id=request.parent_message_id, thread_id=request.thread_id, status="blocked",
        copy_style=copy_style,
    )
    with db.transaction(immediate=True) as conn:
        conn.execute("UPDATE messages SET last_error=? WHERE id=?", (reason[:2000], message_id))
    return message_id


def _record_generation_failure(db: Database, request: CopyRequest, reason: str) -> int:
    """Keep a retry on its existing unique message row."""
    if request.replace_message_id:
        retryable_reason = reason if reason.startswith("Codex ") else f"Codex output invalid: {reason}"
        with db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT status FROM messages WHERE id=?", (request.replace_message_id,)
            ).fetchone()
            if not row or row["status"] != "blocked":
                raise StateError("retry target is no longer blocked")
            conn.execute(
                "UPDATE messages SET last_error=?,updated_at=? WHERE id=?",
                (retryable_reason[:2000], iso(), request.replace_message_id),
            )
        return request.replace_message_id
    return _store_blocked(db, request, reason)


def generate_and_store(db: Database, requests: list[CopyRequest], batch_size: int = 10) -> dict[str, int]:
    result = {"pending_approval": 0, "blocked": 0}
    settings = db.settings()
    booking_url = str(settings.get("email_booking_url") or BOOKING_URL)
    for offset in range(0, len(requests), batch_size):
        batch = requests[offset:offset + batch_size]
        try:
            output = run_codex(batch, settings)
        except CodexCopyError as exc:
            for request in batch:
                _record_generation_failure(db, request, str(exc))
                result["blocked"] += 1
            continue
        returned = {item.get("request_id"): item for item in output.get("items", [])
                    if isinstance(item, dict)}
        for request in batch:
            item = returned.get(request.request_id)
            if not item:
                _record_generation_failure(
                    db, request, "Codex omitted this request from structured output"
                )
                result["blocked"] += 1
                continue
            subject = str(item.get("subject") or "").strip()
            body = str(item.get("body") or "").strip()
            if request.kind in {"followup_1", "followup_2", "reply"}:
                subject = _thread_subject(request.context.get("original_subject", subject))
            evidence_ids = item.get("evidence_ids") if isinstance(item.get("evidence_ids"), list) else []
            evidence_ids = [int(x) for x in evidence_ids if isinstance(x, int)]
            allowed_ids = {int(x["id"]) for x in request.evidence}
            copy_style = (
                str(settings.get("email_copy_style") or LEGACY_COPY_STYLE)
                if request.channel == "email" and request.kind == "initial"
                else LEGACY_COPY_STYLE
            )
            if request.kind == "reply":
                errors = lint_reply(subject, body, evidence_ids, allowed_ids)
            elif request.channel == "email":
                errors = lint_email(subject, body, evidence_ids, allowed_ids,
                                    request.to_address, request.kind,
                                    copy_style=copy_style, booking_url=booking_url)
            else:
                errors = lint_comment(body, evidence_ids, allowed_ids,
                                      request.context.get("post_text", ""))
            if errors:
                _record_generation_failure(db, request, "; ".join(errors))
                result["blocked"] += 1
                continue
            if request.replace_message_id:
                try:
                    db.edit_message(
                        request.replace_message_id, subject, body, evidence_ids,
                        copy_style=copy_style,
                    )
                except StateError as exc:
                    _record_generation_failure(db, request, str(exc))
                    result["blocked"] += 1
                    continue
                message_id = request.replace_message_id
            else:
                message_id = db.create_message(
                    request.campaign_id, request.channel, request.kind, body,
                    prospect_id=request.prospect_id, contact_id=request.contact_id,
                    post_id=request.post_id, to_address=request.to_address, subject=subject,
                    evidence_ids=evidence_ids, parent_message_id=request.parent_message_id,
                    thread_id=request.thread_id, copy_style=copy_style,
                )
                db.mark_pending(message_id)
            if request.prospect_id:
                prospect = db.row("SELECT status FROM prospects WHERE id=?", (request.prospect_id,))
                if prospect and prospect["status"] == "qualified":
                    db.transition_prospect(request.prospect_id, "drafted")
                    db.transition_prospect(request.prospect_id, "pending_approval")
            if request.post_id:
                with db.transaction(immediate=True) as conn:
                    conn.execute("UPDATE posts SET status='pending_approval' WHERE id=?", (request.post_id,))
            result["pending_approval"] += 1
    return result


def validate_stored_message(db: Database, message_id: int) -> list[str]:
    row = db.row("SELECT * FROM messages WHERE id=?", (message_id,))
    if not row:
        return ["message not found"]
    evidence_ids = json.loads(row["evidence_ids_json"] or "[]")
    allowed_ids: set[int] = set()
    positive = [int(x) for x in evidence_ids if int(x) > 0]
    if positive:
        placeholders = ",".join("?" for _ in positive)
        for evidence in db.rows(f"SELECT id FROM evidence WHERE id IN ({placeholders})", positive):
            allowed_ids.add(int(evidence["id"]))
    if row["post_id"]:
        allowed_ids.add(-int(row["post_id"]))
    hash_errors: list[str] = []
    if row["status"] in {"approved", "scheduled"}:
        expected = delivery_content_hash(
            row["channel"], row["kind"], row["to_address"], row["subject"], row["body"],
            evidence_ids, db.settings(),
        )
        if not row["approved_hash"] or expected != row["content_hash"] or expected != row["approved_hash"]:
            hash_errors.append("approved final-delivery content hash no longer matches")
    if row["kind"] == "reply":
        errors = hash_errors + lint_reply(row["subject"], row["body"], evidence_ids, allowed_ids)
        if row["parent_message_id"]:
            parent = db.row("SELECT subject FROM messages WHERE id=?", (row["parent_message_id"],))
            if not parent or row["subject"] != _thread_subject(parent["subject"]):
                errors.append("reply subject must preserve the original email thread")
        return sorted(set(errors))
    if row["channel"] == "email":
        errors = hash_errors + lint_email(row["subject"], row["body"], evidence_ids, allowed_ids,
                                          row["to_address"], row["kind"],
                                          copy_style=row["copy_style"],
                                          booking_url=str(db.setting("email_booking_url", BOOKING_URL)))
        if row["kind"] in {"followup_1", "followup_2"} and row["parent_message_id"]:
            parent = db.row("SELECT subject FROM messages WHERE id=?", (row["parent_message_id"],))
            if not parent or row["subject"] != _thread_subject(parent["subject"]):
                errors.append("follow-up subject must preserve the original email thread")
        if row["kind"] == "initial" and row["contact_id"] and row["prospect_id"]:
            contact = db.row("SELECT * FROM contacts WHERE id=?", (row["contact_id"],))
            if contact:
                errors.extend(initial_outreach_gate(db, row["prospect_id"], dict(contact), db.settings(),
                                                    exclude_message_id=message_id))
        return sorted(set(errors))
    post = db.row("SELECT text,published_at FROM posts WHERE id=?", (row["post_id"],))
    post_errors: list[str] = []
    published = parse_iso(post["published_at"] if post else None)
    if not published or published < utcnow() - timedelta(
            hours=int(db.setting("post_max_age_hours", 48))):
        post_errors.append("LinkedIn post is older than the permitted freshness window")
    return sorted(set(hash_errors + post_errors + lint_comment(
        row["body"], evidence_ids, allowed_ids, post["text"] if post else "")))


def revalidate_queue(db: Database) -> dict[str, Any]:
    checked = blocked = restored = 0
    details: dict[int, list[str]] = {}
    rows = db.rows(
        "SELECT m.id,m.prospect_id,m.status FROM messages m JOIN campaigns c ON c.id=m.campaign_id "
        "WHERE m.status IN ('pending_approval','approved') OR "
        "(m.status='blocked' AND c.name='fresh' AND m.kind='initial') ORDER BY m.id")
    for row in rows:
        checked += 1
        errors = validate_stored_message(db, row["id"])
        if not errors:
            if row["status"] == "blocked":
                with db.transaction(immediate=True) as conn:
                    conn.execute(
                        "UPDATE messages SET status='pending_approval',last_error=NULL,updated_at=? WHERE id=?",
                        (iso(), row["id"]),
                    )
                    if row["prospect_id"]:
                        conn.execute(
                            "UPDATE prospects SET status='pending_approval',updated_at=? "
                            "WHERE id=? AND status='blocked'",
                            (iso(), row["prospect_id"]),
                        )
                restored += 1
            continue
        details[int(row["id"])] = errors
        if row["status"] == "blocked":
            continue
        with db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE messages SET status='blocked',approved_hash=NULL,last_error=?,updated_at=? WHERE id=?",
                ("; ".join(errors), iso(), row["id"]),
            )
            if row["prospect_id"]:
                conn.execute(
                    "UPDATE prospects SET status='blocked',updated_at=? WHERE id=? "
                    "AND status IN ('qualified','drafted','pending_approval','approved')",
                    (iso(), row["prospect_id"]),
                )
        blocked += 1
    return {"checked": checked, "blocked": blocked, "restored": restored, "details": details}


def prepare(db: Database, email_limit: int = 20, comment_limit: int = 5) -> dict[str, Any]:
    db.initialize()
    retries = retryable_followup_requests(db, email_limit)
    followups = followup_requests(db, max(0, email_limit - len(retries)))
    emails = email_requests(db, max(0, email_limit - len(retries) - len(followups)))
    comments = comment_requests(db, comment_limit)
    return {
        "requested": {
            "email": len(emails), "followups": len(retries) + len(followups),
            "linkedin": len(comments),
        },
        "email": generate_and_store(db, retries + followups + emails),
        "linkedin": generate_and_store(db, comments),
    }
