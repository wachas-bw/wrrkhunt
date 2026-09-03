"""SQLite canonical state and fail-closed lifecycle transitions."""
from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Sequence

from .config import DB_PATH, DEFAULT_SETTINGS, ensure_home
from .util import (
    content_hash, delivery_content_hash, iso, normalize_domain, normalize_email,
    normalize_linkedin, registrable_domain,
)

SCHEMA_VERSION = 2
ACTIVE_STATES = {
    "discovered", "audited", "qualified", "drafted", "pending_approval",
    "approved", "scheduled",
}
TERMINAL_STATES = {"sent", "posted", "blocked", "rejected", "suppressed", "failed", "replied"}
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "discovered": {"audited", "blocked", "suppressed", "failed"},
    "audited": {"qualified", "blocked", "suppressed", "failed"},
    "qualified": {"drafted", "blocked", "suppressed", "failed"},
    "drafted": {"pending_approval", "blocked", "failed"},
    "pending_approval": {"approved", "rejected", "blocked", "suppressed", "failed"},
    "approved": {"pending_approval", "scheduled", "rejected", "suppressed", "blocked"},
    "scheduled": {"pending_approval", "sent", "posted", "suppressed", "failed", "blocked"},
    "sent": {"replied", "suppressed"},
    "posted": set(), "blocked": set(), "rejected": set(), "suppressed": set(),
    "failed": set(), "replied": set(),
}

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_meta (
  version INTEGER NOT NULL,
  applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL DEFAULT 'fresh',
  status TEXT NOT NULL DEFAULT 'active',
  markets_json TEXT NOT NULL DEFAULT '[]',
  pool_mix_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS source_runs (
  id INTEGER PRIMARY KEY,
  campaign_id INTEGER REFERENCES campaigns(id),
  source TEXT NOT NULL,
  query TEXT,
  market TEXT,
  pool TEXT,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  candidates_count INTEGER NOT NULL DEFAULT 0,
  audited_count INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  budget_note TEXT,
  error TEXT
);
CREATE TABLE IF NOT EXISTS prospects (
  id INTEGER PRIMARY KEY,
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  company TEXT NOT NULL DEFAULT '',
  domain TEXT NOT NULL,
  registrable_domain TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT '',
  pool TEXT NOT NULL DEFAULT '',
  vertical TEXT NOT NULL DEFAULT '',
  website TEXT NOT NULL DEFAULT '',
  linkedin_url TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'discovered',
  fit_score INTEGER NOT NULL DEFAULT 0,
  confidence TEXT NOT NULL DEFAULT 'none',
  corporate_type TEXT NOT NULL DEFAULT 'unknown',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  discovered_at TEXT NOT NULL,
  audited_at TEXT,
  qualified_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE(campaign_id, registrable_domain)
);
CREATE INDEX IF NOT EXISTS prospects_status_idx ON prospects(status, campaign_id);
CREATE INDEX IF NOT EXISTS prospects_domain_idx ON prospects(registrable_domain);
CREATE UNIQUE INDEX IF NOT EXISTS prospects_linkedin_idx ON prospects(linkedin_url)
  WHERE linkedin_url != '';
CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY,
  prospect_id INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
  email TEXT NOT NULL,
  normalized_email TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'role',
  published_url TEXT NOT NULL,
  evidence_excerpt TEXT NOT NULL DEFAULT '',
  is_published INTEGER NOT NULL DEFAULT 1 CHECK(is_published IN (0,1)),
  mx_available INTEGER NOT NULL DEFAULT 0 CHECK(mx_available IN (0,1)),
  linkedin_profile TEXT NOT NULL DEFAULT '',
  is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
  created_at TEXT NOT NULL,
  UNIQUE(normalized_email)
);
CREATE INDEX IF NOT EXISTS contacts_prospect_idx ON contacts(prospect_id, is_primary DESC);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY,
  prospect_id INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
  source_run_id INTEGER REFERENCES source_runs(id),
  kind TEXT NOT NULL,
  source_url TEXT NOT NULL,
  excerpt TEXT NOT NULL,
  observed_value TEXT NOT NULL DEFAULT '',
  confidence TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS evidence_prospect_idx ON evidence(prospect_id, kind);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY,
  prospect_id INTEGER REFERENCES prospects(id),
  source_run_id INTEGER REFERENCES source_runs(id),
  author_name TEXT NOT NULL DEFAULT '',
  author_url TEXT NOT NULL DEFAULT '',
  post_url TEXT NOT NULL UNIQUE,
  text TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  published_at TEXT,
  market TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'prospect',
  status TEXT NOT NULL DEFAULT 'discovered',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_status_idx ON posts(status, published_at);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  prospect_id INTEGER REFERENCES prospects(id),
  contact_id INTEGER REFERENCES contacts(id),
  post_id INTEGER REFERENCES posts(id),
  channel TEXT NOT NULL CHECK(channel IN ('email','linkedin')),
  kind TEXT NOT NULL,
  copy_style TEXT NOT NULL DEFAULT 'founder_plain_note_v3',
  to_address TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'drafted',
  content_hash TEXT NOT NULL,
  approved_hash TEXT,
  scheduled_for TEXT,
  sent_at TEXT,
  external_id TEXT,
  parent_message_id INTEGER REFERENCES messages(id),
  thread_id TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_queue_idx ON messages(channel, status, scheduled_for);
CREATE INDEX IF NOT EXISTS messages_prospect_idx ON messages(prospect_id, kind);
CREATE UNIQUE INDEX IF NOT EXISTS messages_one_initial_idx ON messages(prospect_id)
  WHERE channel='email' AND kind='initial' AND prospect_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS messages_one_comment_idx ON messages(post_id)
  WHERE channel='linkedin' AND kind='comment' AND post_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS messages_one_followup_stage_idx ON messages(parent_message_id,kind)
  WHERE kind IN ('followup_1','followup_2') AND parent_message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS messages_external_id_idx ON messages(external_id)
  WHERE external_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  action TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'local-user',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppressions (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('email','domain','linkedin')),
  value TEXT NOT NULL,
  reason TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(kind, value)
);
CREATE TABLE IF NOT EXISTS delivery_events (
  id INTEGER PRIMARY KEY,
  message_id INTEGER REFERENCES messages(id),
  channel TEXT NOT NULL,
  event_type TEXT NOT NULL,
  external_id TEXT,
  details_json TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS delivery_events_recent_idx ON delivery_events(channel, event_type, occurred_at);
CREATE TABLE IF NOT EXISTS channel_state (
  channel TEXT PRIMARY KEY,
  paused INTEGER NOT NULL DEFAULT 1 CHECK(paused IN (0,1)),
  reason TEXT NOT NULL DEFAULT 'setup required',
  emergency_stop INTEGER NOT NULL DEFAULT 0 CHECK(emergency_stop IN (0,1)),
  credential_status TEXT NOT NULL DEFAULT 'unknown',
  last_health_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_counters (
  day TEXT NOT NULL,
  channel TEXT NOT NULL,
  action TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(day, channel, action)
);
CREATE TABLE IF NOT EXISTS imports (
  source_path TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  record_count INTEGER NOT NULL,
  imported_at TEXT NOT NULL,
  PRIMARY KEY(source_path, source_hash)
);
CREATE TABLE IF NOT EXISTS leases (
  name TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
"""


class StateError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        if self.path == DB_PATH:
            ensure_home()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextlib.contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.transaction(immediate=True) as conn:
            conn.executescript(SCHEMA)
            row = conn.execute("SELECT MAX(version) AS version FROM schema_meta").fetchone()
            version = int(row["version"] or 0)
            columns = {item["name"] for item in conn.execute("PRAGMA table_info(messages)")}
            if "copy_style" not in columns:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN copy_style TEXT NOT NULL "
                    "DEFAULT 'founder_plain_note_v3'"
                )
            if version < SCHEMA_VERSION:
                conn.execute("INSERT INTO schema_meta(version, applied_at) VALUES (?,?)",
                             (SCHEMA_VERSION, iso()))
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key,value_json,updated_at) VALUES (?,?,?)",
                    (key, json.dumps(value, ensure_ascii=False), iso()),
                )
            for channel in ("email", "linkedin"):
                conn.execute(
                    "INSERT OR IGNORE INTO channel_state(channel,updated_at) VALUES (?,?)",
                    (channel, iso()),
                )
            self._ensure_campaign_conn(conn, "fresh", "fresh")
            self._ensure_campaign_conn(conn, "legacy", "legacy", status="held")

    def _ensure_campaign_conn(self, conn: sqlite3.Connection, name: str, kind: str,
                              status: str = "active") -> int:
        conn.execute(
            "INSERT OR IGNORE INTO campaigns(name,kind,status,created_at) VALUES (?,?,?,?)",
            (name, kind, status, iso()),
        )
        return int(conn.execute("SELECT id FROM campaigns WHERE name=?", (name,)).fetchone()[0])

    def ensure_campaign(self, name: str, kind: str = "fresh", status: str = "active") -> int:
        with self.transaction(immediate=True) as conn:
            return self._ensure_campaign_conn(conn, name, kind, status)

    def setting(self, key: str, default: Any = None) -> Any:
        with contextlib.closing(self.connect()) as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def settings(self) -> dict[str, Any]:
        with contextlib.closing(self.connect()) as conn:
            rows = conn.execute("SELECT key,value_json FROM settings").fetchall()
        return {r["key"]: json.loads(r["value_json"]) for r in rows}

    def set_setting(self, key: str, value: Any) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO settings(key,value_json,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), iso()),
            )

    def create_source_run(self, campaign_id: int, source: str, query: str = "",
                          market: str = "", pool: str = "") -> int:
        with self.transaction(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO source_runs(campaign_id,source,query,market,pool,status,started_at) "
                "VALUES (?,?,?,?,?,'running',?)",
                (campaign_id, source, query, market, pool, iso()),
            )
            return int(cur.lastrowid)

    def finish_source_run(self, run_id: int, *, status: str, candidates: int = 0,
                          audited: int = 0, cost_usd: float = 0, budget_note: str = "",
                          error: str = "") -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE source_runs SET status=?,finished_at=?,candidates_count=?,audited_count=?,"
                "cost_usd=?,budget_note=?,error=? WHERE id=?",
                (status, iso(), candidates, audited, cost_usd, budget_note, error, run_id),
            )

    def upsert_prospect(self, campaign_id: int, *, domain: str, company: str = "",
                        market: str = "", pool: str = "", vertical: str = "",
                        website: str = "", linkedin_url: str = "",
                        metadata: dict[str, Any] | None = None) -> tuple[int, bool]:
        domain = normalize_domain(domain)
        root = registrable_domain(domain)
        if not root:
            raise ValueError(f"invalid domain: {domain!r}")
        linkedin_url = normalize_linkedin(linkedin_url)
        now = iso()
        with self.transaction(immediate=True) as conn:
            existing = conn.execute(
                "SELECT id FROM prospects WHERE campaign_id=? AND registrable_domain=?",
                (campaign_id, root),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE prospects SET company=CASE WHEN company='' THEN ? ELSE company END,"
                    "market=CASE WHEN market='' THEN ? ELSE market END,"
                    "pool=CASE WHEN pool='' THEN ? ELSE pool END,"
                    "vertical=CASE WHEN vertical='' THEN ? ELSE vertical END,"
                    "website=CASE WHEN website='' THEN ? ELSE website END,"
                    "linkedin_url=CASE WHEN linkedin_url='' THEN ? ELSE linkedin_url END,updated_at=? WHERE id=?",
                    (company, market, pool, vertical, website, linkedin_url, now, existing["id"]),
                )
                return int(existing["id"]), False
            cur = conn.execute(
                "INSERT INTO prospects(campaign_id,company,domain,registrable_domain,market,pool,vertical,"
                "website,linkedin_url,status,metadata_json,discovered_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'discovered',?,?,?)",
                (campaign_id, company, domain, root, market.upper(), pool, vertical,
                 website or f"https://{domain}", linkedin_url,
                 json.dumps(metadata or {}, ensure_ascii=False), now, now),
            )
            return int(cur.lastrowid), True

    def add_evidence(self, prospect_id: int, kind: str, source_url: str, excerpt: str,
                     confidence: str, observed_value: str = "", source_run_id: int | None = None,
                     metadata: dict[str, Any] | None = None, detected_at: str | None = None) -> int:
        if not source_url or not excerpt:
            raise ValueError("evidence requires a source URL and excerpt")
        with self.transaction(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO evidence(prospect_id,source_run_id,kind,source_url,excerpt,observed_value,"
                "confidence,detected_at,metadata_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (prospect_id, source_run_id, kind, source_url, excerpt, observed_value,
                 confidence, detected_at or iso(), json.dumps(metadata or {}, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def add_contact(self, prospect_id: int, email: str, *, kind: str, published_url: str,
                    excerpt: str, mx: bool, linkedin_profile: str = "",
                    primary: bool = False) -> int | None:
        email = normalize_email(email)
        if not email or not published_url or not excerpt:
            return None
        with self.transaction(immediate=True) as conn:
            if self._is_suppressed_conn(conn, "email", email):
                return None
            try:
                cur = conn.execute(
                    "INSERT INTO contacts(prospect_id,email,normalized_email,kind,published_url,"
                    "evidence_excerpt,is_published,mx_available,linkedin_profile,is_primary,created_at) "
                    "VALUES (?,?,?,?,?,?,1,?,?,?,?)",
                    (prospect_id, email, email, kind, published_url, excerpt, int(mx),
                     normalize_linkedin(linkedin_profile), int(primary), iso()),
                )
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def transition_prospect(self, prospect_id: int, new_status: str, **fields: Any) -> None:
        allowed_fields = {"fit_score", "confidence", "corporate_type", "audited_at", "qualified_at", "metadata_json"}
        with self.transaction(immediate=True) as conn:
            row = conn.execute("SELECT status FROM prospects WHERE id=?", (prospect_id,)).fetchone()
            if not row:
                raise StateError("prospect not found")
            old = row["status"]
            if old != new_status and new_status not in ALLOWED_TRANSITIONS.get(old, set()):
                raise StateError(f"invalid prospect transition {old} -> {new_status}")
            assignments = ["status=?", "updated_at=?"]
            values: list[Any] = [new_status, iso()]
            for key, value in fields.items():
                if key not in allowed_fields:
                    raise ValueError(f"unsupported prospect field: {key}")
                assignments.append(f"{key}=?")
                values.append(value)
            values.append(prospect_id)
            conn.execute(f"UPDATE prospects SET {','.join(assignments)} WHERE id=?", values)

    def create_message(self, campaign_id: int, channel: str, kind: str, body: str, *,
                       prospect_id: int | None = None, contact_id: int | None = None,
                       post_id: int | None = None, to_address: str = "", subject: str = "",
                       evidence_ids: Sequence[int] = (), parent_message_id: int | None = None,
                       thread_id: str | None = None, status: str = "drafted",
                       copy_style: str = "founder_plain_note_v3") -> int:
        digest = content_hash(channel, kind, to_address, subject, body, list(evidence_ids))
        now = iso()
        with self.transaction(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO messages(campaign_id,prospect_id,contact_id,post_id,channel,kind,copy_style,to_address,"
                "subject,body,evidence_ids_json,status,content_hash,parent_message_id,thread_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (campaign_id, prospect_id, contact_id, post_id, channel, kind, copy_style, to_address,
                 subject, body, json.dumps(list(evidence_ids)), status, digest,
                 parent_message_id, thread_id, now, now),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _transition_message_conn(conn: sqlite3.Connection, message_id: int, new_status: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not row:
            raise StateError("message not found")
        old = row["status"]
        if old != new_status and new_status not in ALLOWED_TRANSITIONS.get(old, set()):
            raise StateError(f"invalid message transition {old} -> {new_status}")
        conn.execute("UPDATE messages SET status=?,updated_at=? WHERE id=?", (new_status, iso(), message_id))
        return row

    def mark_pending(self, message_id: int) -> None:
        with self.transaction(immediate=True) as conn:
            self._transition_message_conn(conn, message_id, "pending_approval")

    def edit_message(self, message_id: int, subject: str, body: str,
                     evidence_ids: Sequence[int] | None = None,
                     copy_style: str | None = None) -> str:
        with self.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row:
                raise StateError("message not found")
            retryable_generation_block = (
                row["status"] == "blocked"
                and row["body"].startswith("Generation blocked.")
                and str(row["last_error"] or "").startswith("Codex ")
            )
            if row["status"] not in {"drafted", "pending_approval", "approved", "scheduled"} \
                    and not retryable_generation_block:
                raise StateError("only an unsent message can be edited")
            if row["status"] == "scheduled":
                scheduled = conn.execute(
                    "SELECT MAX(id) AS id FROM delivery_events WHERE message_id=? "
                    "AND channel='email' AND event_type='gmail_scheduled'", (message_id,),
                ).fetchone()["id"]
                cancelled = conn.execute(
                    "SELECT MAX(id) AS id FROM delivery_events WHERE message_id=? "
                    "AND channel='email' AND event_type='gmail_schedule_cancelled'", (message_id,),
                ).fetchone()["id"]
                if scheduled and (not cancelled or int(cancelled) < int(scheduled)):
                    raise StateError("cancel the Gmail scheduled send before editing its copy")
            referenced = list(evidence_ids) if evidence_ids is not None else json.loads(
                row["evidence_ids_json"]
            )
            selected_style = copy_style if copy_style is not None else row["copy_style"]
            digest = content_hash(row["channel"], row["kind"], row["to_address"], subject,
                                  body, referenced)
            conn.execute(
                "UPDATE messages SET subject=?,body=?,evidence_ids_json=?,copy_style=?,content_hash=?,"
                "approved_hash=NULL,status='pending_approval',scheduled_for=NULL,last_error=NULL,"
                "updated_at=? WHERE id=?",
                (subject, body, json.dumps(referenced), selected_style, digest, iso(), message_id),
            )
            if row["prospect_id"]:
                conn.execute(
                    "UPDATE prospects SET status='pending_approval',updated_at=? WHERE id=? "
                    "AND status IN ('drafted','pending_approval','approved','scheduled','blocked')",
                    (iso(), row["prospect_id"]),
                )
            conn.execute(
                "INSERT INTO approvals(message_id,action,content_hash,note,created_at) VALUES (?,?,?,?,?)",
                (message_id, "edited", digest, "approval invalidated", iso()),
            )
            return digest

    def approve_message(self, message_id: int, actor: str = "local-user") -> str:
        with self.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row or row["status"] != "pending_approval":
                raise StateError("message must be pending approval")
            settings = {
                item["key"]: json.loads(item["value_json"])
                for item in conn.execute("SELECT key,value_json FROM settings").fetchall()
            }
            if row["channel"] == "email" and not str(
                    settings.get("business_postal_address") or "").strip():
                raise StateError("configure the valid business postal address before approving email")
            digest = delivery_content_hash(
                row["channel"], row["kind"], row["to_address"], row["subject"], row["body"],
                json.loads(row["evidence_ids_json"]), settings,
            )
            conn.execute(
                "UPDATE messages SET status='approved',content_hash=?,approved_hash=?,updated_at=? WHERE id=?",
                (digest, digest, iso(), message_id),
            )
            conn.execute(
                "INSERT INTO approvals(message_id,action,content_hash,actor,created_at) VALUES (?,?,?,?,?)",
                (message_id, "approved", digest, actor, iso()),
            )
            if row["kind"] == "initial" and row["prospect_id"]:
                conn.execute(
                    "UPDATE prospects SET status='approved',updated_at=? "
                    "WHERE id=? AND status='pending_approval'",
                    (iso(), row["prospect_id"]),
                )
            return digest

    def reject_message(self, message_id: int, note: str = "", actor: str = "local-user") -> None:
        with self.transaction(immediate=True) as conn:
            row = self._transition_message_conn(conn, message_id, "rejected")
            conn.execute(
                "INSERT INTO approvals(message_id,action,content_hash,actor,note,created_at) VALUES (?,?,?,?,?,?)",
                (message_id, "rejected", row["content_hash"], actor, note, iso()),
            )
            if row["kind"] == "initial" and row["prospect_id"]:
                conn.execute(
                    "UPDATE prospects SET status='rejected',updated_at=? WHERE id=? "
                    "AND status IN ('qualified','drafted','pending_approval','approved')",
                    (iso(), row["prospect_id"]),
                )
            if row["kind"] == "comment" and row["post_id"]:
                conn.execute("UPDATE posts SET status='rejected' WHERE id=?", (row["post_id"],))

    def release_message(self, message_id: int, scheduled_for: str) -> None:
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT m.*,p.registrable_domain,p.market FROM messages m "
                "LEFT JOIN prospects p ON p.id=m.prospect_id WHERE m.id=?", (message_id,),
            ).fetchone()
            if not row or row["status"] != "approved":
                raise StateError("message must be approved before release")
            if not row["approved_hash"] or row["approved_hash"] != row["content_hash"]:
                raise StateError("approved content hash does not match current content")
            approved = conn.execute(
                "SELECT created_at FROM approvals WHERE message_id=? AND action='approved' "
                "AND content_hash=? ORDER BY id DESC LIMIT 1",
                (message_id, row["content_hash"]),
            ).fetchone()
            if not approved or approved["created_at"][:10] != iso()[:10]:
                raise StateError("release requires a fresh approval today")
            if row["kind"] == "reply":
                raise StateError("human replies are draft-only and can never be auto-released")
            if row["registrable_domain"] and self._is_suppressed_conn(conn, "domain", row["registrable_domain"]):
                raise StateError("prospect domain is suppressed")
            if row["to_address"] and self._is_suppressed_conn(conn, "email", normalize_email(row["to_address"])):
                raise StateError("recipient is suppressed")
            conn.execute(
                "UPDATE messages SET status='scheduled',scheduled_for=?,updated_at=? WHERE id=?",
                (scheduled_for, iso(), message_id),
            )
            conn.execute(
                "INSERT INTO approvals(message_id,action,content_hash,created_at) VALUES (?,?,?,?)",
                (message_id, "released", row["content_hash"], iso()),
            )
            if row["kind"] == "initial" and row["prospect_id"]:
                conn.execute(
                    "UPDATE prospects SET status='scheduled',updated_at=? "
                    "WHERE id=? AND status='approved'",
                    (iso(), row["prospect_id"]),
                )

    def mark_delivered(self, message_id: int, external_id: str, *, posted: bool = False,
                       delivered_at: str | None = None) -> None:
        target = "posted" if posted else "sent"
        stamp = delivered_at or iso()
        with self.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row or row["status"] != "scheduled":
                raise StateError("only a scheduled message can be delivered")
            if not row["approved_hash"] or row["approved_hash"] != row["content_hash"]:
                raise StateError("approved content hash does not match current content")
            conn.execute(
                "UPDATE messages SET status=?,sent_at=?,external_id=?,last_error=NULL,updated_at=? WHERE id=?",
                (target, stamp, external_id, iso(), message_id),
            )
            if row["kind"] == "initial" and row["prospect_id"]:
                conn.execute(
                    "UPDATE prospects SET status='sent',updated_at=? WHERE id=? AND status='scheduled'",
                    (iso(), row["prospect_id"]),
                )

    def set_thread_id(self, message_id: int, thread_id: str) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute("UPDATE messages SET thread_id=?,updated_at=? WHERE id=?",
                         (thread_id, iso(), message_id))

    def mark_replied(self, message_id: int, note: str = "human reply") -> None:
        with self.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row:
                raise StateError("message not found")
            if row["status"] == "sent":
                conn.execute("UPDATE messages SET status='replied',last_error=?,updated_at=? WHERE id=?",
                             (note, iso(), message_id))
            if row["prospect_id"]:
                conn.execute("UPDATE prospects SET status='replied',updated_at=? WHERE id=?",
                             (iso(), row["prospect_id"]))
                conn.execute(
                    "UPDATE messages SET status='suppressed',last_error='cancelled after human reply',updated_at=? "
                    "WHERE prospect_id=? AND kind IN ('followup_1','followup_2') "
                    "AND status IN ('drafted','pending_approval','approved','scheduled')",
                    (iso(), row["prospect_id"]),
                )

    def reserve_message_attempt(self, message_id: int) -> None:
        """Persist intent before an external side effect so crashes cannot be retried blindly."""
        with self.transaction(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE messages SET attempt_count=attempt_count+1,updated_at=? "
                "WHERE id=? AND status='scheduled' AND attempt_count=0",
                (iso(), message_id),
            )
            if cur.rowcount != 1:
                raise StateError("message already has a delivery attempt or is not scheduled")

    def mark_failed(self, message_id: int, error: str, terminal: bool = True,
                    count_attempt: bool = True) -> None:
        with self.transaction(immediate=True) as conn:
            row = conn.execute("SELECT status FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row:
                raise StateError("message not found")
            status = "failed" if terminal else row["status"]
            conn.execute(
                "UPDATE messages SET status=?,attempt_count=attempt_count+?,last_error=?,updated_at=? WHERE id=?",
                (status, int(count_attempt), error[:2000], iso(), message_id),
            )

    def reschedule(self, message_id: int, when: str, reason: str = "") -> None:
        with self.transaction(immediate=True) as conn:
            row = conn.execute("SELECT status FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row or row["status"] != "scheduled":
                raise StateError("only scheduled messages can be rescheduled")
            conn.execute("UPDATE messages SET scheduled_for=?,last_error=?,updated_at=? WHERE id=?",
                         (when, reason, iso(), message_id))

    def acquire_lease(self, name: str, owner: str, expires_at: str) -> bool:
        with self.transaction(immediate=True) as conn:
            existing = conn.execute("SELECT * FROM leases WHERE name=?", (name,)).fetchone()
            now = iso()
            if existing and existing["expires_at"] > now and existing["owner"] != owner:
                return False
            conn.execute(
                "INSERT INTO leases(name,owner,expires_at) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at",
                (name, owner, expires_at),
            )
            return True

    def release_lease(self, name: str, owner: str) -> None:
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM leases WHERE name=? AND owner=?", (name, owner))

    def suppress(self, kind: str, value: str, reason: str, source: str = "manual") -> None:
        if kind == "domain":
            value = registrable_domain(value)
        elif kind == "email":
            value = normalize_email(value)
        elif kind == "linkedin":
            value = normalize_linkedin(value)
        if not value:
            raise ValueError("invalid suppression value")
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO suppressions(kind,value,reason,source,created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(kind,value) DO UPDATE SET reason=excluded.reason,source=excluded.source",
                (kind, value, reason, source, iso()),
            )
            if kind == "email":
                conn.execute(
                    "UPDATE messages SET status='suppressed',last_error=?,updated_at=? "
                    "WHERE to_address=? AND status IN ('drafted','pending_approval','approved','scheduled')",
                    (reason, iso(), value),
                )
                conn.execute(
                    "UPDATE prospects SET status='suppressed',updated_at=? WHERE id IN "
                    "(SELECT prospect_id FROM contacts WHERE normalized_email=?) "
                    "AND status IN ('discovered','audited','qualified','drafted','pending_approval',"
                    "'approved','scheduled','sent')",
                    (iso(), value),
                )
            if kind == "domain":
                conn.execute(
                    "UPDATE messages SET status='suppressed',last_error=?,updated_at=? WHERE prospect_id IN "
                    "(SELECT id FROM prospects WHERE registrable_domain=?) AND status IN "
                    "('drafted','pending_approval','approved','scheduled')",
                    (reason, iso(), value),
                )
                conn.execute(
                    "UPDATE prospects SET status='suppressed',updated_at=? WHERE registrable_domain=? "
                    "AND status IN ('discovered','audited','qualified','drafted','pending_approval',"
                    "'approved','scheduled','sent')",
                    (iso(), value),
                )
            if kind == "linkedin":
                conn.execute(
                    "UPDATE messages SET status='suppressed',last_error=?,updated_at=? "
                    "WHERE to_address=? AND status IN ('drafted','pending_approval','approved','scheduled')",
                    (reason, iso(), value),
                )
                conn.execute("UPDATE posts SET status='suppressed' WHERE author_url=?", (value,))

    @staticmethod
    def _is_suppressed_conn(conn: sqlite3.Connection, kind: str, value: str) -> bool:
        return bool(conn.execute("SELECT 1 FROM suppressions WHERE kind=? AND value=?", (kind, value)).fetchone())

    def is_suppressed(self, kind: str, value: str) -> bool:
        if kind == "domain":
            value = registrable_domain(value)
        elif kind == "email":
            value = normalize_email(value)
        elif kind == "linkedin":
            value = normalize_linkedin(value)
        with contextlib.closing(self.connect()) as conn:
            return self._is_suppressed_conn(conn, kind, value)

    def reserve_daily_action(self, channel: str, action: str, day: str, cap: int) -> int:
        """Atomically reserve an attempt. Failed attempts remain counted by design."""
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT count FROM daily_counters WHERE day=? AND channel=? AND action=?",
                (day, channel, action),
            ).fetchone()
            count = int(row["count"]) if row else 0
            if count >= cap:
                raise StateError(f"{channel} daily cap of {cap} reached")
            count += 1
            conn.execute(
                "INSERT INTO daily_counters(day,channel,action,count) VALUES (?,?,?,?) "
                "ON CONFLICT(day,channel,action) DO UPDATE SET count=excluded.count",
                (day, channel, action, count),
            )
            return count

    def set_channel(self, channel: str, *, paused: bool, reason: str = "",
                    emergency_stop: bool | None = None, credential_status: str | None = None) -> None:
        with self.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM channel_state WHERE channel=?", (channel,)).fetchone()
            if not row:
                raise ValueError(f"unknown channel: {channel}")
            conn.execute(
                "UPDATE channel_state SET paused=?,reason=?,emergency_stop=?,credential_status=?,"
                "last_health_at=?,updated_at=? WHERE channel=?",
                (int(paused), reason, int(row["emergency_stop"] if emergency_stop is None else emergency_stop),
                 credential_status or row["credential_status"], iso(), iso(), channel),
            )

    def channel(self, channel: str) -> sqlite3.Row:
        with contextlib.closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM channel_state WHERE channel=?", (channel,)).fetchone()
        if not row:
            raise ValueError(f"unknown channel: {channel}")
        return row

    def record_event(self, channel: str, event_type: str, *, message_id: int | None = None,
                     external_id: str | None = None, details: dict[str, Any] | None = None,
                     occurred_at: str | None = None) -> int:
        with self.transaction(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO delivery_events(message_id,channel,event_type,external_id,details_json,occurred_at) "
                "VALUES (?,?,?,?,?,?)",
                (message_id, channel, event_type, external_id,
                 json.dumps(details or {}, ensure_ascii=False), occurred_at or iso()),
            )
            return int(cur.lastrowid)

    def rows(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with contextlib.closing(self.connect()) as conn:
            return conn.execute(sql, params).fetchall()

    def row(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with contextlib.closing(self.connect()) as conn:
            return conn.execute(sql, params).fetchone()
