from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .sources import (
    GITHUB_ISSUE_ANALYZE_SOURCE,
    JIRA_ANALYZE_SOURCE,
    JIRA_ISSUE_AUTO_ANALYZE_SOURCE,
    JIRA_STATUS_SUMMARY_SOURCE,
    LARK_JIRA_ANALYZE_SOURCE,
    requires_jira_automation_report,
)


LOG = logging.getLogger(__name__)
EVENT_STREAM_LIMIT = 500
_EVENT_STREAMS: dict[str, deque[dict[str, Any]]] = {}
_EVENT_IDS: dict[str, int] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Slot:
    slot_id: str
    source: str
    lark_event_id: str | None
    lark_message_id: str | None
    chat_id: str | None
    chat_type: str | None
    sender_id: str | None
    incoming_text: str
    status: str
    codex_status: str
    reply_status: str
    reply_text: str | None
    codex_attempts: int
    codex_thread_id: str | None
    codex_turn_id: str | None
    trigger_key: str | None
    jira_issue_key: str | None
    jira_comment_id: str | None
    github_repo: str | None
    github_issue_number: int | None
    case_id: str | None
    delivery_target: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AnalysisCase:
    case_id: str
    scope_type: str
    scope_key: str
    title: str
    status: str
    primary_codex_thread_id: str | None
    latest_codex_thread_id: str | None
    latest_slot_id: str | None
    summary: str | None
    bound_lark_chat_id: str | None
    bound_lark_chat_name: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class JiraIssueRecord:
    issue_key: str
    project_key: str
    summary: str
    reporter: str | None
    assignee: str | None
    slot_id: str | None
    status: str | None
    jira_status_name: str | None
    jira_status_id: str | None
    jira_status_category_key: str | None
    jira_created_at: str
    jira_updated_at: str
    observed_at: str


@dataclass(frozen=True)
class JiraIssueObservation:
    issue_key: str
    inserted: bool
    previous_status_name: str | None
    current_status_name: str | None
    current_status_id: str | None
    current_status_category_key: str | None

    @property
    def status_changed(self) -> bool:
        return bool(
            self.previous_status_name
            and self.current_status_name
            and self.previous_status_name.casefold() != self.current_status_name.casefold()
        )


@dataclass(frozen=True)
class JiraCommentRecord:
    comment_id: str
    issue_key: str
    project_key: str
    author: str
    body: str
    slot_id: str | None
    status: str | None
    jira_created_at: str
    jira_updated_at: str
    observed_at: str


@dataclass(frozen=True)
class JiraAutomationRecord:
    slot_id: str
    source: str
    issue_key: str
    case_id: str | None
    trigger_key: str | None
    subject_user: str | None
    subject_role: str | None
    jira_comment_id: str | None
    comment_verified_at: str | None
    report_status: str
    missing_required_material: bool | None
    has_clear_resolution: bool | None
    reason: str | None
    details: dict[str, Any]
    raw_report: dict[str, Any]
    error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class GitHubIssueRecord:
    repo_full_name: str
    issue_number: int
    title: str
    author: str
    state: str
    html_url: str
    slot_id: str | None
    status: str | None
    github_created_at: str
    github_updated_at: str
    observed_at: str


@dataclass(frozen=True)
class LarkMessageRecord:
    message_id: str
    event_id: str | None
    chat_id: str
    chat_type: str | None
    chat_name: str | None
    sender_id: str | None
    message_type: str
    content: str
    mentioned_bot: bool
    handled_slot_id: str | None
    observed_at: str


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._event_key = str(self.path.expanduser().resolve())
        _EVENT_STREAMS.setdefault(self._event_key, deque(maxlen=EVENT_STREAM_LIMIT))
        _EVENT_IDS.setdefault(self._event_key, 0)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS slots (
                  slot_id TEXT PRIMARY KEY,
                  source TEXT NOT NULL,
                  lark_event_id TEXT UNIQUE,
                  lark_message_id TEXT,
                  chat_id TEXT,
                  chat_type TEXT,
                  sender_id TEXT,
                  incoming_text TEXT NOT NULL,
                  status TEXT NOT NULL,
                  codex_status TEXT NOT NULL,
                  reply_status TEXT NOT NULL,
                  reply_text TEXT,
                  lark_reply_message_id TEXT,
                  codex_attempts INTEGER NOT NULL DEFAULT 0,
                  codex_thread_id TEXT,
                  codex_turn_id TEXT,
                  trigger_key TEXT UNIQUE,
                  jira_issue_key TEXT,
                  jira_comment_id TEXT,
                  github_repo TEXT,
                  github_issue_number INTEGER,
                  case_id TEXT,
                  delivery_target TEXT,
                  error TEXT,
                  raw_json TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  slot_id TEXT,
                  component TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  message TEXT NOT NULL,
                  data_json TEXT,
                  created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_slots_status ON slots(status);
                CREATE INDEX IF NOT EXISTS idx_slots_reply_status ON slots(reply_status);
                CREATE INDEX IF NOT EXISTS idx_events_slot ON events(slot_id);

                CREATE TABLE IF NOT EXISTS jira_state (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS state (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jira_issues (
                  issue_key TEXT PRIMARY KEY,
                  project_key TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  reporter TEXT,
                  assignee TEXT,
                  status_name TEXT,
                  status_id TEXT,
                  status_category_key TEXT,
                  jira_created_at TEXT NOT NULL,
                  jira_updated_at TEXT NOT NULL,
                  slot_id TEXT UNIQUE,
                  raw_json TEXT NOT NULL,
                  observed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jira_comments (
                  comment_id TEXT PRIMARY KEY,
                  issue_key TEXT NOT NULL,
                  project_key TEXT NOT NULL,
                  author TEXT NOT NULL,
                  body TEXT NOT NULL,
                  jira_created_at TEXT NOT NULL,
                  jira_updated_at TEXT NOT NULL,
                  slot_id TEXT UNIQUE,
                  raw_json TEXT NOT NULL,
                  observed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jira_issues_observed ON jira_issues(observed_at);
                CREATE INDEX IF NOT EXISTS idx_jira_comments_observed ON jira_comments(observed_at);
                CREATE INDEX IF NOT EXISTS idx_jira_comments_issue ON jira_comments(issue_key);

                CREATE TABLE IF NOT EXISTS jira_automation_records (
                  slot_id TEXT PRIMARY KEY,
                  source TEXT NOT NULL,
                  issue_key TEXT NOT NULL,
                  case_id TEXT,
                  trigger_key TEXT,
                  subject_user TEXT,
                  subject_role TEXT,
                  jira_comment_id TEXT,
                  comment_verified_at TEXT,
                  report_status TEXT NOT NULL DEFAULT 'pending',
                  missing_required_material INTEGER,
                  has_clear_resolution INTEGER,
                  reason TEXT,
                  details_json TEXT NOT NULL DEFAULT '{}',
                  raw_report_json TEXT NOT NULL DEFAULT '{}',
                  error TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jira_automation_source_created
                  ON jira_automation_records(source, created_at);
                CREATE INDEX IF NOT EXISTS idx_jira_automation_issue
                  ON jira_automation_records(issue_key);
                CREATE INDEX IF NOT EXISTS idx_jira_automation_report_status
                  ON jira_automation_records(report_status);

                CREATE TABLE IF NOT EXISTS github_issues (
                  repo_full_name TEXT NOT NULL,
                  issue_number INTEGER NOT NULL,
                  node_id TEXT NOT NULL,
                  title TEXT NOT NULL,
                  body TEXT NOT NULL,
                  author TEXT NOT NULL,
                  state TEXT NOT NULL,
                  html_url TEXT NOT NULL,
                  labels_json TEXT NOT NULL,
                  github_created_at TEXT NOT NULL,
                  github_updated_at TEXT NOT NULL,
                  slot_id TEXT UNIQUE,
                  raw_json TEXT NOT NULL,
                  observed_at TEXT NOT NULL,
                  PRIMARY KEY(repo_full_name, issue_number)
                );

                CREATE INDEX IF NOT EXISTS idx_github_issues_observed ON github_issues(observed_at);
                CREATE INDEX IF NOT EXISTS idx_github_issues_slot_id ON github_issues(slot_id);

                CREATE TABLE IF NOT EXISTS analysis_cases (
                  case_id TEXT PRIMARY KEY,
                  scope_type TEXT NOT NULL,
                  scope_key TEXT NOT NULL,
                  title TEXT NOT NULL,
                  status TEXT NOT NULL,
                  primary_codex_thread_id TEXT,
                  latest_codex_thread_id TEXT,
                  latest_slot_id TEXT,
                  summary TEXT,
                  bound_lark_chat_id TEXT,
                  bound_lark_chat_name TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(scope_type, scope_key)
                );

                CREATE TABLE IF NOT EXISTS case_slots (
                  case_id TEXT NOT NULL,
                  slot_id TEXT NOT NULL UNIQUE,
                  relation_type TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(case_id, slot_id)
                );

                CREATE TABLE IF NOT EXISTS case_aliases (
                  alias_type TEXT NOT NULL,
                  alias_key TEXT NOT NULL,
                  case_id TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(alias_type, alias_key)
                );

                CREATE INDEX IF NOT EXISTS idx_cases_scope ON analysis_cases(scope_type, scope_key);
                CREATE INDEX IF NOT EXISTS idx_cases_status ON analysis_cases(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_case_slots_case ON case_slots(case_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_case_aliases_case ON case_aliases(case_id);

                CREATE TABLE IF NOT EXISTS lark_messages (
                  message_id TEXT PRIMARY KEY,
                  event_id TEXT UNIQUE,
                  chat_id TEXT NOT NULL,
                  chat_type TEXT,
                  chat_name TEXT,
                  sender_id TEXT,
                  message_type TEXT NOT NULL,
                  content TEXT NOT NULL,
                  mentioned_bot INTEGER NOT NULL DEFAULT 0,
                  handled_slot_id TEXT,
                  raw_json TEXT NOT NULL,
                  observed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_lark_messages_chat_observed ON lark_messages(chat_id, observed_at);
                CREATE INDEX IF NOT EXISTS idx_lark_messages_slot ON lark_messages(handled_slot_id);
                """
            )
            self._ensure_column(conn, "slots", "trigger_key", "TEXT")
            self._ensure_column(conn, "slots", "jira_issue_key", "TEXT")
            self._ensure_column(conn, "slots", "jira_comment_id", "TEXT")
            self._ensure_column(conn, "slots", "github_repo", "TEXT")
            self._ensure_column(conn, "slots", "github_issue_number", "INTEGER")
            self._ensure_column(conn, "slots", "case_id", "TEXT")
            self._ensure_column(conn, "slots", "delivery_target", "TEXT")
            self._ensure_column(conn, "jira_issues", "slot_id", "TEXT")
            self._ensure_column(conn, "jira_issues", "status_name", "TEXT")
            self._ensure_column(conn, "jira_issues", "status_id", "TEXT")
            self._ensure_column(conn, "jira_issues", "status_category_key", "TEXT")
            self._ensure_column(conn, "jira_issues", "reporter", "TEXT")
            self._ensure_column(conn, "jira_issues", "assignee", "TEXT")
            self._ensure_column(conn, "jira_comments", "slot_id", "TEXT")
            self._ensure_column(conn, "analysis_cases", "bound_lark_chat_id", "TEXT")
            self._ensure_column(conn, "analysis_cases", "bound_lark_chat_name", "TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_slots_case_id ON slots(case_id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_slots_trigger_key ON slots(trigger_key) WHERE trigger_key IS NOT NULL")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jira_issues_slot_id ON jira_issues(slot_id) WHERE slot_id IS NOT NULL")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jira_comments_slot_id ON jira_comments(slot_id) WHERE slot_id IS NOT NULL")
            conn.execute(
                """
                UPDATE slots
                SET source=?
                WHERE source=?
                  AND trigger_key LIKE 'jira-issue:%'
                  AND jira_comment_id IS NULL
                """,
                (JIRA_ISSUE_AUTO_ANALYZE_SOURCE, JIRA_ANALYZE_SOURCE),
            )
            conn.execute("INSERT OR IGNORE INTO state(key, value, updated_at) SELECT key, value, updated_at FROM jira_state")
            conn.execute("DELETE FROM events")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _link_slot_to_case(self, conn: sqlite3.Connection, case_id: str, slot_id: str, relation_type: str = "request") -> None:
        now = utc_now()
        conn.execute(
            """
            INSERT OR IGNORE INTO case_slots(case_id, slot_id, relation_type, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (case_id, slot_id, relation_type, now),
        )
        conn.execute(
            """
            UPDATE analysis_cases
            SET latest_slot_id=?, updated_at=?
            WHERE case_id=?
            """,
            (slot_id, now, case_id),
        )

    def _register_case_alias(self, conn: sqlite3.Connection, alias_type: str, alias_key: str, case_id: str) -> None:
        if not alias_key:
            return
        conn.execute(
            """
            INSERT OR IGNORE INTO case_aliases(alias_type, alias_key, case_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (alias_type, alias_key, case_id, utc_now()),
        )

    def _ensure_case(self, conn: sqlite3.Connection, *, scope_type: str, scope_key: str, title: str | None = None) -> AnalysisCase:
        case_id = case_id_for_scope(scope_type, scope_key)
        now = utc_now()
        conn.execute(
            """
            INSERT INTO analysis_cases(case_id, scope_type, scope_key, title, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'idle', ?, ?)
            ON CONFLICT(scope_type, scope_key) DO NOTHING
            """,
            (case_id, scope_type, scope_key, title or scope_key, now, now),
        )
        row = conn.execute(
            "SELECT * FROM analysis_cases WHERE scope_type=? AND scope_key=?",
            (scope_type, scope_key),
        ).fetchone()
        assert row is not None
        case = _row_to_case(row)
        if scope_type == "jira_issue":
            self._register_case_alias(conn, "jira_issue_key", scope_key, case.case_id)
        if scope_type == "github_issue":
            self._register_case_alias(conn, "github_issue", scope_key, case.case_id)
        if scope_type == "lark_chat":
            self._register_case_alias(conn, "lark_chat", scope_key, case.case_id)
        self._register_case_alias(conn, "case_id", case.case_id, case.case_id)
        return case

    def _ensure_jira_automation_record_for_slot(self, conn: sqlite3.Connection, slot: Slot) -> None:
        if not requires_jira_automation_report(slot.source) or not slot.jira_issue_key:
            return
        issue = conn.execute("SELECT * FROM jira_issues WHERE issue_key=?", (slot.jira_issue_key,)).fetchone()
        subject_role = "reporter" if slot.source == JIRA_ISSUE_AUTO_ANALYZE_SOURCE else "assignee"
        subject_user = None
        if issue is not None:
            subject_user = issue["reporter"] if subject_role == "reporter" else issue["assignee"]
            if not subject_user:
                issue_raw = _json_loads_dict(issue["raw_json"])
                fields = _json_loads_dict(issue_raw.get("fields"))
                subject_user = _jira_user_display(fields.get(subject_role))
        now = utc_now()
        conn.execute(
            """
            INSERT INTO jira_automation_records(
              slot_id, source, issue_key, case_id, trigger_key, subject_user, subject_role,
              report_status, details_json, raw_report_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '{}', '{}', ?, ?)
            ON CONFLICT(slot_id) DO UPDATE SET
              source=excluded.source,
              issue_key=excluded.issue_key,
              case_id=COALESCE(jira_automation_records.case_id, excluded.case_id),
              trigger_key=COALESCE(jira_automation_records.trigger_key, excluded.trigger_key),
              subject_user=COALESCE(excluded.subject_user, jira_automation_records.subject_user),
              subject_role=COALESCE(excluded.subject_role, jira_automation_records.subject_role),
              updated_at=excluded.updated_at
            """,
            (
                slot.slot_id,
                slot.source,
                slot.jira_issue_key,
                slot.case_id,
                slot.trigger_key,
                subject_user,
                subject_role,
                now,
                now,
            ),
        )

    def log_event(self, component: str, event_type: str, message: str, slot_id: str | None = None, **data: Any) -> None:
        _EVENT_IDS[self._event_key] += 1
        data_json = json.dumps(data, ensure_ascii=False)
        event = {
            "id": _EVENT_IDS[self._event_key],
            "slot_id": slot_id,
            "component": component,
            "event_type": event_type,
            "message": message,
            "data": data,
            "created_at": utc_now(),
        }
        _EVENT_STREAMS[self._event_key].appendleft(event)
        LOG.info(
            "event %s.%s slot=%s message=%s data=%s",
            component,
            event_type,
            slot_id,
            message,
            data_json,
            extra={"component": component, "slot_id": slot_id},
        )

    def create_slot(
        self,
        *,
        source: str,
        incoming_text: str,
        lark_event_id: str | None = None,
        lark_message_id: str | None = None,
        chat_id: str | None = None,
        chat_type: str | None = None,
        sender_id: str | None = None,
        raw: dict[str, Any] | None = None,
        trigger_key: str | None = None,
        jira_issue_key: str | None = None,
        jira_comment_id: str | None = None,
        github_repo: str | None = None,
        github_issue_number: int | None = None,
        case_id: str | None = None,
        delivery_target: str | None = None,
    ) -> Slot:
        slot_id = f"slot_{uuid.uuid4().hex[:12]}"
        now = utc_now()
        raw_json = json.dumps(raw or {}, ensure_ascii=False)
        with self.connect() as conn:
            if trigger_key:
                existing = conn.execute("SELECT * FROM slots WHERE trigger_key = ?", (trigger_key,)).fetchone()
                if existing:
                    return _row_to_slot(existing)
            if lark_event_id:
                existing = conn.execute("SELECT * FROM slots WHERE lark_event_id = ?", (lark_event_id,)).fetchone()
                if existing:
                    return _row_to_slot(existing)
            conn.execute(
                """
                INSERT INTO slots(
                  slot_id, source, lark_event_id, lark_message_id, chat_id, chat_type, sender_id,
                  incoming_text, status, codex_status, reply_status, raw_json, trigger_key, jira_issue_key,
                  jira_comment_id, github_repo, github_issue_number, case_id, delivery_target, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', 'none', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slot_id,
                    source,
                    lark_event_id,
                    lark_message_id,
                    chat_id,
                    chat_type,
                    sender_id,
                    incoming_text,
                    raw_json,
                    trigger_key,
                    jira_issue_key,
                    jira_comment_id,
                    github_repo,
                    github_issue_number,
                    case_id,
                    delivery_target,
                    now,
                    now,
                ),
            )
            if case_id:
                self._link_slot_to_case(conn, case_id, slot_id)
                self._register_case_alias(conn, "slot_id", slot_id, case_id)
                if lark_message_id:
                    self._register_case_alias(conn, "lark_message_id", lark_message_id, case_id)
            row = conn.execute("SELECT * FROM slots WHERE slot_id = ?", (slot_id,)).fetchone()
            assert row is not None
            return _row_to_slot(row)

    def get_slot(self, slot_id: str) -> Slot | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM slots WHERE slot_id = ?", (slot_id,)).fetchone()
            return _row_to_slot(row) if row else None

    def get_slot_raw(self, slot_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT raw_json FROM slots WHERE slot_id = ?", (slot_id,)).fetchone()
            return _json_loads_dict(row["raw_json"]) if row else {}

    def list_slots(self, limit: int = 20) -> list[Slot]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM slots ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [_row_to_slot(row) for row in rows]

    def list_slots_by_ids(self, slot_ids: Iterable[str]) -> list[Slot]:
        ids = sorted(set(slot_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM slots WHERE slot_id IN ({placeholders}) ORDER BY created_at ASC",
                ids,
            ).fetchall()
            return [_row_to_slot(row) for row in rows]

    def open_jira_analyze_slots(self, limit: int = 200) -> list[Slot]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM slots
                WHERE (
                    source IN (?, ?, ?)
                    AND reply_status != 'sent'
                    AND status != 'replied'
                  )
                  OR (
                    source=?
                    AND codex_status != 'completed'
                    AND status != 'failed'
                  )
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (
                    JIRA_ANALYZE_SOURCE,
                    JIRA_ISSUE_AUTO_ANALYZE_SOURCE,
                    JIRA_STATUS_SUMMARY_SOURCE,
                    LARK_JIRA_ANALYZE_SOURCE,
                    limit,
                ),
            ).fetchall()
            return [_row_to_slot(row) for row in rows]

    def recent_lark_slots(self, limit: int = 100) -> list[Slot]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM slots
                WHERE source IN (
                  'lark',
                  'lark_case_followup',
                  'lark_case_resolution',
                  'lark_case_binding_conflict',
                  'lark_jira_analyze'
                )
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [_row_to_slot(row) for row in rows]

    def record_lark_message(
        self,
        *,
        message_id: str,
        event_id: str | None,
        chat_id: str,
        chat_type: str | None,
        chat_name: str | None,
        sender_id: str | None,
        message_type: str,
        content: str,
        mentioned_bot: bool,
        raw: dict[str, Any],
        handled_slot_id: str | None = None,
    ) -> None:
        raw_json = json.dumps(raw or {}, ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO lark_messages(
                  message_id, event_id, chat_id, chat_type, chat_name, sender_id, message_type,
                  content, mentioned_bot, handled_slot_id, raw_json, observed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                  chat_name=CASE WHEN excluded.chat_name IS NOT NULL AND excluded.chat_name != ''
                    THEN excluded.chat_name ELSE lark_messages.chat_name END,
                  mentioned_bot=MAX(lark_messages.mentioned_bot, excluded.mentioned_bot),
                  handled_slot_id=COALESCE(excluded.handled_slot_id, lark_messages.handled_slot_id)
                """,
                (
                    message_id,
                    event_id,
                    chat_id,
                    chat_type,
                    chat_name,
                    sender_id,
                    message_type,
                    content,
                    1 if mentioned_bot else 0,
                    handled_slot_id,
                    raw_json,
                    utc_now(),
                ),
            )

    def link_lark_message_to_slot(self, message_id: str, slot_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE lark_messages SET handled_slot_id=COALESCE(handled_slot_id, ?) WHERE message_id=?",
                (slot_id, message_id),
            )

    def recent_lark_messages(self, limit: int = 100) -> list[LarkMessageRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM lark_messages
                ORDER BY observed_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [_row_to_lark_message(row) for row in rows]

    def get_lark_message_raw(self, message_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT raw_json FROM lark_messages WHERE message_id = ?", (message_id,)).fetchone()
            return _json_loads_dict(row["raw_json"]) if row else {}

    def lark_context_since_last_handled(
        self,
        *,
        chat_id: str,
        case_id: str | None = None,
        chat_type: str | None = None,
        sender_id: str | None = None,
        limit: int = 15,
    ) -> list[LarkMessageRecord]:
        with self.connect() as conn:
            private_sender_id = sender_id if chat_type != "group" and sender_id else None
            if case_id:
                if private_sender_id:
                    last = conn.execute(
                        """
                        SELECT MAX(lm.observed_at) AS observed_at
                        FROM lark_messages lm
                        JOIN slots s ON s.slot_id=lm.handled_slot_id
                        WHERE lm.chat_id=? AND s.case_id=? AND lm.sender_id=?
                        """,
                        (chat_id, case_id, private_sender_id),
                    ).fetchone()
                else:
                    last = conn.execute(
                        """
                        SELECT MAX(lm.observed_at) AS observed_at
                        FROM lark_messages lm
                        JOIN slots s ON s.slot_id=lm.handled_slot_id
                        WHERE lm.chat_id=? AND s.case_id=?
                        """,
                        (chat_id, case_id),
                    ).fetchone()
            elif private_sender_id:
                last = conn.execute(
                    """
                    SELECT MAX(lm.observed_at) AS observed_at
                    FROM lark_messages lm
                    JOIN slots s ON s.slot_id=lm.handled_slot_id
                    WHERE lm.chat_id=? AND lm.sender_id=?
                    """,
                    (chat_id, private_sender_id),
                ).fetchone()
            else:
                last = conn.execute(
                    """
                    SELECT MAX(lm.observed_at) AS observed_at
                    FROM lark_messages lm
                    JOIN slots s ON s.slot_id=lm.handled_slot_id
                    WHERE lm.chat_id=?
                    """,
                    (chat_id,),
                ).fetchone()
            after = str(last["observed_at"] or "")
            if private_sender_id:
                rows = conn.execute(
                    """
                    SELECT * FROM (
                      SELECT * FROM lark_messages
                      WHERE chat_id=? AND sender_id=? AND observed_at > ?
                      ORDER BY observed_at DESC
                      LIMIT ?
                    )
                    ORDER BY observed_at ASC
                    """,
                    (chat_id, private_sender_id, after, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM (
                      SELECT * FROM lark_messages
                      WHERE chat_id=? AND observed_at > ?
                      ORDER BY observed_at DESC
                      LIMIT ?
                    )
                    ORDER BY observed_at ASC
                    """,
                    (chat_id, after, limit),
                ).fetchall()
            return [_row_to_lark_message(row) for row in rows]

    def ensure_case(self, *, scope_type: str, scope_key: str, title: str | None = None) -> AnalysisCase:
        with self.connect() as conn:
            return self._ensure_case(conn, scope_type=scope_type, scope_key=scope_key, title=title)

    def ensure_lark_chat_case(
        self,
        *,
        chat_id: str,
        chat_type: str | None,
        chat_name: str | None = None,
        sender_id: str | None = None,
    ) -> AnalysisCase:
        scope_key = lark_chat_scope_key(chat_id=chat_id, chat_type=chat_type, sender_id=sender_id)
        title = chat_name or scope_key
        with self.connect() as conn:
            case = self._ensure_case(conn, scope_type="lark_chat", scope_key=scope_key, title=title)
            if chat_type == "group" or not sender_id:
                self._register_case_alias(conn, "lark_chat_id", chat_id, case.case_id)
            if chat_type != "group" and sender_id:
                self._register_case_alias(conn, "lark_sender_id", sender_id, case.case_id)
            if chat_type == "group":
                conn.execute(
                    """
                    UPDATE analysis_cases
                    SET bound_lark_chat_id=COALESCE(bound_lark_chat_id, ?),
                        bound_lark_chat_name=COALESCE(NULLIF(bound_lark_chat_name, ''), ?),
                        updated_at=?
                    WHERE case_id=?
                    """,
                    (chat_id, chat_name, utc_now(), case.case_id),
                )
            self._attach_unbound_lark_slots_to_case(
                conn,
                chat_id=chat_id,
                chat_type=chat_type,
                sender_id=sender_id,
                case_id=case.case_id,
            )
            row = conn.execute("SELECT * FROM analysis_cases WHERE case_id=?", (case.case_id,)).fetchone()
            assert row is not None
            return _row_to_case(row)

    def backfill_lark_chat_cases(self) -> int:
        """Attach historical uncased Lark slots to durable chat-level cases."""

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  s.chat_id,
                  COALESCE(NULLIF(s.chat_type, ''), 'chat') AS chat_type,
                  s.sender_id,
                  (
                    SELECT lm.chat_name
                    FROM lark_messages lm
                    WHERE lm.chat_id=s.chat_id
                      AND lm.chat_name IS NOT NULL
                      AND lm.chat_name != ''
                    ORDER BY lm.observed_at DESC
                    LIMIT 1
                  ) AS chat_name
                FROM slots s
                WHERE s.chat_id IS NOT NULL
                  AND s.case_id IS NULL
                  AND s.source='lark'
                  AND s.status != 'failed'
                ORDER BY s.created_at ASC
                """
            ).fetchall()
            adopted = 0
            seen_scope_keys: set[str] = set()
            for row in rows:
                chat_id = str(row["chat_id"])
                chat_type = str(row["chat_type"] or "chat")
                sender_id = str(row["sender_id"]) if row["sender_id"] else None
                chat_name = str(row["chat_name"]) if row["chat_name"] else None
                scope_key = lark_chat_scope_key(chat_id=chat_id, chat_type=chat_type, sender_id=sender_id)
                if scope_key in seen_scope_keys:
                    continue
                seen_scope_keys.add(scope_key)
                case = self._ensure_case(conn, scope_type="lark_chat", scope_key=scope_key, title=chat_name or scope_key)
                if chat_type == "group" or not sender_id:
                    self._register_case_alias(conn, "lark_chat_id", chat_id, case.case_id)
                if chat_type != "group" and sender_id:
                    self._register_case_alias(conn, "lark_sender_id", sender_id, case.case_id)
                if chat_type == "group":
                    conn.execute(
                        """
                        UPDATE analysis_cases
                        SET bound_lark_chat_id=COALESCE(bound_lark_chat_id, ?),
                            bound_lark_chat_name=COALESCE(NULLIF(bound_lark_chat_name, ''), ?),
                            updated_at=?
                        WHERE case_id=?
                        """,
                        (chat_id, chat_name, utc_now(), case.case_id),
                    )
                adopted += self._attach_unbound_lark_slots_to_case(
                    conn,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    sender_id=sender_id,
                    case_id=case.case_id,
                )
            return adopted

    def _attach_unbound_lark_slots_to_case(
        self,
        conn: sqlite3.Connection,
        *,
        chat_id: str,
        chat_type: str | None,
        sender_id: str | None,
        case_id: str,
    ) -> int:
        if chat_type != "group" and sender_id:
            rows = conn.execute(
                """
                SELECT * FROM slots
                WHERE sender_id=?
                  AND COALESCE(NULLIF(chat_type, ''), 'chat') != 'group'
                  AND case_id IS NULL
                  AND source='lark'
                  AND status != 'failed'
                ORDER BY created_at ASC
                """,
                (sender_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM slots
                WHERE chat_id=?
                  AND case_id IS NULL
                  AND source='lark'
                  AND status != 'failed'
                ORDER BY created_at ASC
                """,
                (chat_id,),
            ).fetchall()
        for row in rows:
            slot = _row_to_slot(row)
            now = utc_now()
            conn.execute(
                """
                UPDATE slots
                SET case_id=?,
                    delivery_target=COALESCE(delivery_target, 'lark_reply'),
                    updated_at=?
                WHERE slot_id=?
                """,
                (case_id, now, slot.slot_id),
            )
            self._link_slot_to_case(conn, case_id, slot.slot_id, relation_type="history")
            self._register_case_alias(conn, "slot_id", slot.slot_id, case_id)
            if slot.lark_message_id:
                self._register_case_alias(conn, "lark_message_id", slot.lark_message_id, case_id)
            if slot.codex_thread_id and _lark_history_thread_is_adoptable(slot):
                self._register_case_alias(conn, "codex_thread_id", slot.codex_thread_id, case_id)
                conn.execute(
                    """
                    UPDATE analysis_cases
                    SET primary_codex_thread_id=COALESCE(primary_codex_thread_id, ?),
                        latest_codex_thread_id=?,
                        latest_slot_id=?,
                        updated_at=?
                    WHERE case_id=?
                    """,
                    (slot.codex_thread_id, slot.codex_thread_id, slot.slot_id, now, case_id),
                )
        return len(rows)

    def get_case(self, case_id: str) -> AnalysisCase | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM analysis_cases WHERE case_id=?", (case_id,)).fetchone()
            return _row_to_case(row) if row else None

    def bind_case_lark_group(self, case_id: str, chat_id: str, chat_name: str | None) -> AnalysisCase | None:
        """Bind a case to one Lark group, idempotently updating the display name."""

        if not chat_id:
            return self.get_case(case_id)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM analysis_cases WHERE case_id=?", (case_id,)).fetchone()
            if row is None:
                return None
            existing_chat_id = row["bound_lark_chat_id"]
            if existing_chat_id and existing_chat_id != chat_id:
                return _row_to_case(row)
            display_name = chat_name or row["bound_lark_chat_name"]
            conn.execute(
                """
                UPDATE analysis_cases
                SET bound_lark_chat_id=?,
                    bound_lark_chat_name=?,
                    updated_at=?
                WHERE case_id=?
                """,
                (chat_id, display_name, utc_now(), case_id),
            )
            refreshed = conn.execute("SELECT * FROM analysis_cases WHERE case_id=?", (case_id,)).fetchone()
            return _row_to_case(refreshed) if refreshed else None

    def find_case_by_alias(self, alias_type: str, alias_key: str) -> AnalysisCase | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT c.* FROM case_aliases a
                JOIN analysis_cases c ON c.case_id=a.case_id
                WHERE a.alias_type=? AND a.alias_key=?
                """,
                (alias_type, alias_key),
            ).fetchone()
            return _row_to_case(row) if row else None

    def register_case_alias(self, alias_type: str, alias_key: str, case_id: str) -> None:
        with self.connect() as conn:
            self._register_case_alias(conn, alias_type, alias_key, case_id)

    def list_cases(self, limit: int = 25) -> list[AnalysisCase]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM analysis_cases
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [_row_to_case(row) for row in rows]

    def case_timelines(self, limit: int = 25, slots_per_case: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            case_rows = conn.execute(
                """
                SELECT * FROM analysis_cases
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            cases = [_row_to_case(row) for row in case_rows]
            if not cases:
                return []

            case_ids = [case.case_id for case in cases]
            placeholders = ",".join("?" for _ in case_ids)
            aliases_by_case: dict[str, list[dict[str, str]]] = {case_id: [] for case_id in case_ids}
            alias_rows = conn.execute(
                f"""
                SELECT case_id, alias_type, alias_key, created_at
                FROM case_aliases
                WHERE case_id IN ({placeholders})
                ORDER BY alias_type ASC, alias_key ASC
                """,
                case_ids,
            ).fetchall()
            for row in alias_rows:
                aliases_by_case[str(row["case_id"])].append(
                    {
                        "alias_type": str(row["alias_type"]),
                        "alias_key": str(row["alias_key"]),
                        "created_at": str(row["created_at"]),
                    }
                )

            count_rows = conn.execute(
                f"""
                SELECT case_id, COUNT(*) AS count
                FROM case_slots
                WHERE case_id IN ({placeholders})
                GROUP BY case_id
                """,
                case_ids,
            ).fetchall()
            slot_counts = {str(row["case_id"]): int(row["count"]) for row in count_rows}

            slots_by_case: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in case_ids}
            slot_rows = conn.execute(
                f"""
                SELECT * FROM (
                  SELECT s.*, cs.case_id AS timeline_case_id,
                         ROW_NUMBER() OVER (PARTITION BY cs.case_id ORDER BY cs.created_at DESC) AS rn
                  FROM case_slots cs
                  JOIN slots s ON s.slot_id=cs.slot_id
                  WHERE cs.case_id IN ({placeholders})
                )
                WHERE rn <= ?
                ORDER BY timeline_case_id ASC, created_at ASC
                """,
                [*case_ids, slots_per_case],
            ).fetchall()
            for row in slot_rows:
                slot = _row_to_slot(row)
                slots_by_case[str(row["timeline_case_id"])].append(slot.__dict__)

            return [
                {
                    "case": case.__dict__,
                    "aliases": aliases_by_case.get(case.case_id, []),
                    "slot_count": slot_counts.get(case.case_id, 0),
                    "slots": slots_by_case.get(case.case_id, []),
                }
                for case in cases
            ]

    def list_case_slots(self, case_id: str) -> list[Slot]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.* FROM case_slots cs
                JOIN slots s ON s.slot_id=cs.slot_id
                WHERE cs.case_id=?
                ORDER BY cs.created_at ASC
                """,
                (case_id,),
            ).fetchall()
            return [_row_to_slot(row) for row in rows]

    def list_case_aliases(self, case_id: str) -> list[dict[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT alias_type, alias_key, created_at
                FROM case_aliases
                WHERE case_id=?
                ORDER BY alias_type ASC, alias_key ASC
                """,
                (case_id,),
            ).fetchall()
            return [
                {
                    "alias_type": str(row["alias_type"]),
                    "alias_key": str(row["alias_key"]),
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ]

    def update_case_summary(self, case_id: str, summary: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE analysis_cases SET summary=?, updated_at=? WHERE case_id=?",
                (summary, utc_now(), case_id),
            )

    def count_slots_by_status(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM slots GROUP BY status").fetchall()
            return {str(row["status"]): int(row["count"]) for row in rows}

    def recent_codex_thread_ids(self, limit: int = 100) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT codex_thread_id
                FROM slots
                WHERE codex_thread_id IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [str(row["codex_thread_id"]) for row in rows]

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(_EVENT_STREAMS[self._event_key])[:limit]

    def recoverable_slots(self) -> list[Slot]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM slots
                WHERE codex_status IN ('queued', 'running')
                  AND (reply_status != 'sent' OR source='lark_jira_analyze')
                  AND status != 'failed'
                ORDER BY created_at ASC
                """
            ).fetchall()
            return [_row_to_slot(row) for row in rows]

    def get_state(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO state(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, utc_now()),
            )

    def insert_jira_issue(
        self,
        *,
        issue_key: str,
        project_key: str,
        summary: str,
        jira_created_at: str,
        jira_updated_at: str,
        raw: dict[str, Any],
        reporter: str | None = None,
        assignee: str | None = None,
        status_name: str | None = None,
        status_id: str | None = None,
        status_category_key: str | None = None,
    ) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO jira_issues(
                  issue_key, project_key, summary, reporter, assignee, status_name, status_id, status_category_key,
                  jira_created_at, jira_updated_at, raw_json, observed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(issue_key).upper(),
                    str(project_key).upper(),
                    summary,
                    reporter,
                    assignee,
                    status_name,
                    status_id,
                    status_category_key,
                    jira_created_at,
                    jira_updated_at,
                    json.dumps(raw, ensure_ascii=False),
                    utc_now(),
                ),
            )
            return cur.rowcount == 1

    def observe_jira_issue(
        self,
        *,
        issue_key: str,
        project_key: str,
        summary: str,
        jira_created_at: str,
        jira_updated_at: str,
        raw: dict[str, Any],
        status_name: str | None = None,
        status_id: str | None = None,
        status_category_key: str | None = None,
        reporter: str | None = None,
        assignee: str | None = None,
    ) -> JiraIssueObservation:
        normalized_issue_key = str(issue_key).upper()
        normalized_project_key = str(project_key).upper()
        raw_json = json.dumps(raw, ensure_ascii=False)
        now = utc_now()
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT status_name FROM jira_issues WHERE issue_key=?",
                (normalized_issue_key,),
            ).fetchone()
            inserted = previous is None
            conn.execute(
                """
                INSERT INTO jira_issues(
                  issue_key, project_key, summary, reporter, assignee, status_name, status_id, status_category_key,
                  jira_created_at, jira_updated_at, raw_json, observed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(issue_key) DO UPDATE SET
                  project_key=excluded.project_key,
                  summary=excluded.summary,
                  reporter=excluded.reporter,
                  assignee=excluded.assignee,
                  status_name=excluded.status_name,
                  status_id=excluded.status_id,
                  status_category_key=excluded.status_category_key,
                  jira_updated_at=excluded.jira_updated_at,
                  raw_json=excluded.raw_json,
                  observed_at=excluded.observed_at
                """,
                (
                    normalized_issue_key,
                    normalized_project_key,
                    summary,
                    reporter,
                    assignee,
                    status_name,
                    status_id,
                    status_category_key,
                    jira_created_at,
                    jira_updated_at,
                    raw_json,
                    now,
                ),
            )
            return JiraIssueObservation(
                issue_key=normalized_issue_key,
                inserted=inserted,
                previous_status_name=str(previous["status_name"]) if previous and previous["status_name"] else None,
                current_status_name=status_name,
                current_status_id=status_id,
                current_status_category_key=status_category_key,
            )

    def insert_jira_comment(
        self,
        *,
        comment_id: str,
        issue_key: str,
        project_key: str,
        author: str,
        body: str,
        jira_created_at: str,
        jira_updated_at: str,
        raw: dict[str, Any],
    ) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO jira_comments(
                  comment_id, issue_key, project_key, author, body, jira_created_at, jira_updated_at, raw_json, observed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comment_id,
                    issue_key,
                    project_key,
                    author,
                    body,
                    jira_created_at,
                    jira_updated_at,
                    json.dumps(raw, ensure_ascii=False),
                    utc_now(),
                ),
            )
            return cur.rowcount == 1

    def create_jira_analyze_slot_for_comment(self, comment_id: str) -> Slot | None:
        slot, _created = self.ensure_jira_analyze_slot_for_comment(comment_id)
        return slot

    def create_jira_issue_analyze_slot_for_issue(self, issue_key: str) -> Slot | None:
        slot, _created = self.ensure_jira_issue_analyze_slot_for_issue(issue_key)
        return slot

    def create_jira_status_summary_slot_for_issue(
        self,
        issue_key: str,
        *,
        previous_status_name: str | None,
        status_name: str,
        jira_updated_at: str,
    ) -> Slot | None:
        slot, _created = self.ensure_jira_status_summary_slot_for_issue(
            issue_key,
            previous_status_name=previous_status_name,
            status_name=status_name,
            jira_updated_at=jira_updated_at,
        )
        return slot

    def ensure_jira_issue_analyze_slot_for_issue(self, issue_key: str) -> tuple[Slot | None, bool]:
        now = utc_now()
        normalized_issue_key = str(issue_key).upper()
        trigger_key = f"jira-issue:{normalized_issue_key}"
        with self.connect() as conn:
            issue = conn.execute("SELECT * FROM jira_issues WHERE issue_key=?", (normalized_issue_key,)).fetchone()
            if issue is None:
                return None, False
            case = self._ensure_case(conn, scope_type="jira_issue", scope_key=normalized_issue_key, title=normalized_issue_key)
            if issue["slot_id"]:
                row = conn.execute("SELECT * FROM slots WHERE slot_id=?", (issue["slot_id"],)).fetchone()
                if row:
                    slot = _row_to_slot(row)
                    if not slot.case_id:
                        conn.execute(
                            """
                            UPDATE slots
                            SET case_id=COALESCE(case_id, ?),
                                delivery_target=COALESCE(delivery_target, 'jira_comment'),
                                jira_issue_key=COALESCE(jira_issue_key, ?),
                                updated_at=?
                            WHERE slot_id=?
                            """,
                            (case.case_id, normalized_issue_key, now, slot.slot_id),
                        )
                        self._link_slot_to_case(conn, case.case_id, slot.slot_id)
                        self._register_case_alias(conn, "slot_id", slot.slot_id, case.case_id)
                    refreshed = conn.execute("SELECT * FROM slots WHERE slot_id=?", (slot.slot_id,)).fetchone()
                    refreshed_slot = _row_to_slot(refreshed)
                    self._ensure_jira_automation_record_for_slot(conn, refreshed_slot)
                    return refreshed_slot, False

            existing = conn.execute("SELECT * FROM slots WHERE trigger_key=?", (trigger_key,)).fetchone()
            if existing:
                slot = _row_to_slot(existing)
                conn.execute("UPDATE jira_issues SET slot_id=? WHERE issue_key=?", (slot.slot_id, normalized_issue_key))
                if not slot.case_id:
                    conn.execute(
                        """
                        UPDATE slots
                        SET case_id=COALESCE(case_id, ?),
                            delivery_target=COALESCE(delivery_target, 'jira_comment'),
                            jira_issue_key=COALESCE(jira_issue_key, ?),
                            updated_at=?
                        WHERE slot_id=?
                        """,
                        (case.case_id, normalized_issue_key, now, slot.slot_id),
                    )
                self._link_slot_to_case(conn, case.case_id, slot.slot_id)
                self._register_case_alias(conn, "slot_id", slot.slot_id, case.case_id)
                refreshed = conn.execute("SELECT * FROM slots WHERE slot_id=?", (slot.slot_id,)).fetchone()
                refreshed_slot = _row_to_slot(refreshed)
                self._ensure_jira_automation_record_for_slot(conn, refreshed_slot)
                return refreshed_slot, False

            slot_id = f"slot_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO slots(
                  slot_id, source, incoming_text, status, codex_status, reply_status, trigger_key,
                  jira_issue_key, case_id, delivery_target, raw_json, created_at, updated_at
                )
                VALUES (?, ?, ?, 'queued', 'queued', 'none', ?, ?, ?, 'jira_comment', ?, ?, ?)
                """,
                (
                    slot_id,
                    JIRA_ISSUE_AUTO_ANALYZE_SOURCE,
                    "",
                    trigger_key,
                    normalized_issue_key,
                    case.case_id,
                    issue["raw_json"],
                    now,
                    now,
                ),
            )
            conn.execute("UPDATE jira_issues SET slot_id=? WHERE issue_key=?", (slot_id, normalized_issue_key))
            self._link_slot_to_case(conn, case.case_id, slot_id)
            self._register_case_alias(conn, "slot_id", slot_id, case.case_id)
            row = conn.execute("SELECT * FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
            assert row is not None
            slot = _row_to_slot(row)
            self._ensure_jira_automation_record_for_slot(conn, slot)
            return slot, True

    def ensure_jira_status_summary_slot_for_issue(
        self,
        issue_key: str,
        *,
        previous_status_name: str | None,
        status_name: str,
        jira_updated_at: str,
    ) -> tuple[Slot | None, bool]:
        now = utc_now()
        normalized_issue_key = str(issue_key).upper()
        normalized_status = _trigger_fragment(status_name)
        normalized_updated_at = _trigger_fragment(jira_updated_at or now)
        trigger_key = f"jira-status:{normalized_issue_key}:{normalized_status}:{normalized_updated_at}"
        with self.connect() as conn:
            issue = conn.execute("SELECT * FROM jira_issues WHERE issue_key=?", (normalized_issue_key,)).fetchone()
            if issue is None:
                return None, False
            case = self._ensure_case(conn, scope_type="jira_issue", scope_key=normalized_issue_key, title=normalized_issue_key)
            existing = conn.execute("SELECT * FROM slots WHERE trigger_key=?", (trigger_key,)).fetchone()
            if existing:
                slot = _row_to_slot(existing)
                if not slot.case_id:
                    conn.execute(
                        """
                        UPDATE slots
                        SET case_id=COALESCE(case_id, ?),
                            delivery_target=COALESCE(delivery_target, 'jira_comment'),
                            jira_issue_key=COALESCE(jira_issue_key, ?),
                            updated_at=?
                        WHERE slot_id=?
                        """,
                        (case.case_id, normalized_issue_key, now, slot.slot_id),
                    )
                self._link_slot_to_case(conn, case.case_id, slot.slot_id)
                self._register_case_alias(conn, "slot_id", slot.slot_id, case.case_id)
                refreshed = conn.execute("SELECT * FROM slots WHERE slot_id=?", (slot.slot_id,)).fetchone()
                refreshed_slot = _row_to_slot(refreshed)
                self._ensure_jira_automation_record_for_slot(conn, refreshed_slot)
                return refreshed_slot, False

            slot_id = f"slot_{uuid.uuid4().hex[:12]}"
            raw = _json_loads_dict(issue["raw_json"])
            raw["breakwater_status_transition"] = {
                "previous_status_name": previous_status_name,
                "status_name": status_name,
                "jira_updated_at": jira_updated_at,
                "issue_summary": str(issue["summary"]),
            }
            conn.execute(
                """
                INSERT INTO slots(
                  slot_id, source, incoming_text, status, codex_status, reply_status, trigger_key,
                  jira_issue_key, case_id, delivery_target, raw_json, created_at, updated_at
                )
                VALUES (?, ?, ?, 'queued', 'queued', 'none', ?, ?, ?, 'jira_comment', ?, ?, ?)
                """,
                (
                    slot_id,
                    JIRA_STATUS_SUMMARY_SOURCE,
                    "",
                    trigger_key,
                    normalized_issue_key,
                    case.case_id,
                    json.dumps(raw, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self._link_slot_to_case(conn, case.case_id, slot_id)
            self._register_case_alias(conn, "slot_id", slot_id, case.case_id)
            row = conn.execute("SELECT * FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
            assert row is not None
            slot = _row_to_slot(row)
            self._ensure_jira_automation_record_for_slot(conn, slot)
            return slot, True

    def ensure_jira_analyze_slot_for_comment(self, comment_id: str) -> tuple[Slot | None, bool]:
        now = utc_now()
        trigger_key = f"jira-comment:{comment_id}"
        with self.connect() as conn:
            comment = conn.execute("SELECT * FROM jira_comments WHERE comment_id=?", (comment_id,)).fetchone()
            if comment is None:
                return None, False
            issue_key = str(comment["issue_key"])
            case = self._ensure_case(conn, scope_type="jira_issue", scope_key=issue_key, title=issue_key)
            self._register_case_alias(conn, "jira_comment_id", comment_id, case.case_id)
            if comment["slot_id"]:
                row = conn.execute("SELECT * FROM slots WHERE slot_id=?", (comment["slot_id"],)).fetchone()
                if row:
                    slot = _row_to_slot(row)
                    if not slot.case_id:
                        conn.execute(
                            "UPDATE slots SET case_id=COALESCE(case_id, ?), delivery_target=COALESCE(delivery_target, 'jira_comment'), updated_at=? WHERE slot_id=?",
                            (case.case_id, now, slot.slot_id),
                        )
                        self._link_slot_to_case(conn, case.case_id, slot.slot_id)
                        self._register_case_alias(conn, "slot_id", slot.slot_id, case.case_id)
                    refreshed = conn.execute("SELECT * FROM slots WHERE slot_id=?", (slot.slot_id,)).fetchone()
                    return _row_to_slot(refreshed), False

            existing = conn.execute("SELECT * FROM slots WHERE trigger_key=?", (trigger_key,)).fetchone()
            if existing:
                slot = _row_to_slot(existing)
                conn.execute("UPDATE jira_comments SET slot_id=? WHERE comment_id=?", (slot.slot_id, comment_id))
                if not slot.case_id:
                    conn.execute(
                        "UPDATE slots SET case_id=COALESCE(case_id, ?), delivery_target=COALESCE(delivery_target, 'jira_comment'), updated_at=? WHERE slot_id=?",
                        (case.case_id, now, slot.slot_id),
                    )
                self._link_slot_to_case(conn, case.case_id, slot.slot_id)
                self._register_case_alias(conn, "slot_id", slot.slot_id, case.case_id)
                refreshed = conn.execute("SELECT * FROM slots WHERE slot_id=?", (slot.slot_id,)).fetchone()
                return _row_to_slot(refreshed), False

            slot_id = f"slot_{uuid.uuid4().hex[:12]}"
            incoming_text = str(comment["body"])
            conn.execute(
                """
                INSERT INTO slots(
                  slot_id, source, incoming_text, status, codex_status, reply_status, trigger_key,
                  jira_issue_key, jira_comment_id, case_id, delivery_target, raw_json, created_at, updated_at
                )
                VALUES (?, 'jira_analyze', ?, 'queued', 'queued', 'none', ?, ?, ?, ?, 'jira_comment', ?, ?, ?)
                """,
                (
                    slot_id,
                    incoming_text,
                    trigger_key,
                    issue_key,
                    comment_id,
                    case.case_id,
                    comment["raw_json"],
                    now,
                    now,
                ),
            )
            conn.execute("UPDATE jira_comments SET slot_id=? WHERE comment_id=?", (slot_id, comment_id))
            self._link_slot_to_case(conn, case.case_id, slot_id)
            self._register_case_alias(conn, "slot_id", slot_id, case.case_id)
            row = conn.execute("SELECT * FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
            assert row is not None
            return _row_to_slot(row), True

    def jira_analyze_slots_needing_queue(self) -> list[Slot]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.* FROM slots s
                WHERE s.source IN (?, ?, ?)
                  AND s.codex_status IN ('queued', 'running')
                  AND s.reply_status != 'sent'
                  AND s.status != 'failed'
                ORDER BY s.created_at ASC
                """,
                (JIRA_ANALYZE_SOURCE, JIRA_ISSUE_AUTO_ANALYZE_SOURCE, JIRA_STATUS_SUMMARY_SOURCE),
            ).fetchall()
            return [_row_to_slot(row) for row in rows]

    def insert_github_issue(
        self,
        *,
        repo_full_name: str,
        issue_number: int,
        node_id: str,
        title: str,
        body: str,
        author: str,
        state: str,
        html_url: str,
        labels: list[dict[str, Any]],
        github_created_at: str,
        github_updated_at: str,
        raw: dict[str, Any],
    ) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO github_issues(
                  repo_full_name, issue_number, node_id, title, body, author, state, html_url,
                  labels_json, github_created_at, github_updated_at, raw_json, observed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo_full_name,
                    issue_number,
                    node_id,
                    title,
                    body,
                    author,
                    state,
                    html_url,
                    json.dumps(labels, ensure_ascii=False),
                    github_created_at,
                    github_updated_at,
                    json.dumps(raw, ensure_ascii=False),
                    utc_now(),
                ),
            )
            return cur.rowcount == 1

    def create_github_issue_slot_for_issue(self, repo_full_name: str, issue_number: int) -> Slot | None:
        slot, _created = self.ensure_github_issue_slot_for_issue(repo_full_name, issue_number)
        return slot

    def ensure_github_issue_slot_for_issue(self, repo_full_name: str, issue_number: int) -> tuple[Slot | None, bool]:
        now = utc_now()
        repo = normalize_repo_full_name(repo_full_name)
        trigger_key = f"github-issue:{repo}:{issue_number}"
        scope_key = f"{repo}#{issue_number}"
        with self.connect() as conn:
            issue = conn.execute(
                "SELECT * FROM github_issues WHERE repo_full_name=? AND issue_number=?",
                (repo, issue_number),
            ).fetchone()
            if issue is None:
                return None, False
            case = self._ensure_case(conn, scope_type="github_issue", scope_key=scope_key, title=f"{scope_key} {issue['title']}")
            self._register_case_alias(conn, "github_issue", scope_key, case.case_id)
            self._register_case_alias(conn, "github_issue_url", str(issue["html_url"]), case.case_id)
            if issue["slot_id"]:
                row = conn.execute("SELECT * FROM slots WHERE slot_id=?", (issue["slot_id"],)).fetchone()
                if row:
                    slot = _row_to_slot(row)
                    if not slot.case_id:
                        conn.execute(
                            """
                            UPDATE slots
                            SET case_id=COALESCE(case_id, ?),
                                delivery_target=COALESCE(delivery_target, 'github_issue_comment'),
                                github_repo=COALESCE(github_repo, ?),
                                github_issue_number=COALESCE(github_issue_number, ?),
                                updated_at=?
                            WHERE slot_id=?
                            """,
                            (case.case_id, repo, issue_number, now, slot.slot_id),
                        )
                        self._link_slot_to_case(conn, case.case_id, slot.slot_id)
                        self._register_case_alias(conn, "slot_id", slot.slot_id, case.case_id)
                    refreshed = conn.execute("SELECT * FROM slots WHERE slot_id=?", (slot.slot_id,)).fetchone()
                    return _row_to_slot(refreshed), False

            existing = conn.execute("SELECT * FROM slots WHERE trigger_key=?", (trigger_key,)).fetchone()
            if existing:
                slot = _row_to_slot(existing)
                conn.execute(
                    "UPDATE github_issues SET slot_id=? WHERE repo_full_name=? AND issue_number=?",
                    (slot.slot_id, repo, issue_number),
                )
                if not slot.case_id:
                    conn.execute(
                        """
                        UPDATE slots
                        SET case_id=COALESCE(case_id, ?),
                            delivery_target=COALESCE(delivery_target, 'github_issue_comment'),
                            github_repo=COALESCE(github_repo, ?),
                            github_issue_number=COALESCE(github_issue_number, ?),
                            updated_at=?
                        WHERE slot_id=?
                        """,
                        (case.case_id, repo, issue_number, now, slot.slot_id),
                    )
                self._link_slot_to_case(conn, case.case_id, slot.slot_id)
                self._register_case_alias(conn, "slot_id", slot.slot_id, case.case_id)
                refreshed = conn.execute("SELECT * FROM slots WHERE slot_id=?", (slot.slot_id,)).fetchone()
                return _row_to_slot(refreshed), False

            slot_id = f"slot_{uuid.uuid4().hex[:12]}"
            incoming_text = format_github_issue_input(issue)
            conn.execute(
                """
                INSERT INTO slots(
                  slot_id, source, incoming_text, status, codex_status, reply_status, trigger_key,
                  github_repo, github_issue_number, case_id, delivery_target, raw_json, created_at, updated_at
                )
                VALUES (?, 'github_issue_analyze', ?, 'queued', 'queued', 'none', ?, ?, ?, ?, 'github_issue_comment', ?, ?, ?)
                """,
                (
                    slot_id,
                    incoming_text,
                    trigger_key,
                    repo,
                    issue_number,
                    case.case_id,
                    issue["raw_json"],
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE github_issues SET slot_id=? WHERE repo_full_name=? AND issue_number=?",
                (slot_id, repo, issue_number),
            )
            self._link_slot_to_case(conn, case.case_id, slot_id)
            self._register_case_alias(conn, "slot_id", slot_id, case.case_id)
            row = conn.execute("SELECT * FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
            assert row is not None
            return _row_to_slot(row), True

    def github_issue_slots_needing_queue(self) -> list[Slot]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.* FROM slots s
                WHERE s.source='github_issue_analyze'
                  AND s.codex_status IN ('queued', 'running')
                  AND s.reply_status != 'sent'
                  AND s.status != 'failed'
                ORDER BY s.created_at ASC
                """
            ).fetchall()
            return [_row_to_slot(row) for row in rows]

    def github_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            issue_count = conn.execute("SELECT COUNT(*) AS count FROM github_issues").fetchone()["count"]
            analyzed_count = conn.execute("SELECT COUNT(*) AS count FROM github_issues WHERE slot_id IS NOT NULL").fetchone()["count"]
            return {"issues": int(issue_count), "analyzed_issues": int(analyzed_count)}

    def recent_github_issues(self, limit: int = 10) -> list[GitHubIssueRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT g.repo_full_name, g.issue_number, g.title, g.author, g.state, g.html_url, g.slot_id,
                       s.status AS status,
                       g.github_created_at, g.github_updated_at, g.observed_at
                FROM github_issues g
                LEFT JOIN slots s ON s.slot_id = g.slot_id
                ORDER BY g.observed_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                GitHubIssueRecord(
                    repo_full_name=row["repo_full_name"],
                    issue_number=int(row["issue_number"]),
                    title=row["title"],
                    author=row["author"],
                    state=row["state"],
                    html_url=row["html_url"],
                    slot_id=row["slot_id"],
                    status=row["status"],
                    github_created_at=row["github_created_at"],
                    github_updated_at=row["github_updated_at"],
                    observed_at=row["observed_at"],
                )
                for row in rows
            ]

    def jira_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            issue_count = conn.execute("SELECT COUNT(*) AS count FROM jira_issues").fetchone()["count"]
            analyzed_count = conn.execute("SELECT COUNT(*) AS count FROM jira_issues WHERE slot_id IS NOT NULL").fetchone()["count"]
            comment_count = conn.execute("SELECT COUNT(*) AS count FROM jira_comments").fetchone()["count"]
            return {"issues": int(issue_count), "analyzed_issues": int(analyzed_count), "analyze_comments": int(comment_count)}

    def record_jira_automation_comment(self, slot: Slot, comment_id: str) -> None:
        if not requires_jira_automation_report(slot.source) or not slot.jira_issue_key:
            return
        now = utc_now()
        with self.connect() as conn:
            self._ensure_jira_automation_record_for_slot(conn, slot)
            conn.execute(
                """
                UPDATE jira_automation_records
                SET jira_comment_id=?, comment_verified_at=?, updated_at=?
                WHERE slot_id=?
                """,
                (comment_id, now, now, slot.slot_id),
            )

    def record_jira_automation_report(self, slot_id: str, report: dict[str, Any]) -> tuple[bool, str]:
        slot = self.get_slot(slot_id)
        if slot is None:
            return False, f"slot not found: {slot_id}"
        if not requires_jira_automation_report(slot.source):
            return False, f"slot source does not accept automation reports: {slot.source}"
        ok, message, normalized = _validate_jira_automation_report(slot.source, report)
        with self.connect() as conn:
            self._ensure_jira_automation_record_for_slot(conn, slot)
            existing = conn.execute("SELECT report_status FROM jira_automation_records WHERE slot_id=?", (slot_id,)).fetchone()
            if not ok and existing and existing["report_status"] == "reported":
                return False, message
            conn.execute(
                """
                UPDATE jira_automation_records
                SET report_status=?,
                    missing_required_material=?,
                    has_clear_resolution=?,
                    reason=?,
                    details_json=?,
                    raw_report_json=?,
                    error=?,
                    updated_at=?
                WHERE slot_id=?
                """,
                (
                    "reported" if ok else "invalid",
                    _bool_to_db(normalized.get("missing_required_material")),
                    _bool_to_db(normalized.get("has_clear_resolution")),
                    normalized.get("reason"),
                    json.dumps(normalized.get("details") or {}, ensure_ascii=False),
                    json.dumps(report, ensure_ascii=False),
                    None if ok else message,
                    utc_now(),
                    slot_id,
                ),
            )
        return ok, message

    def jira_automation_report_ready(self, slot_id: str) -> bool:
        record = self.get_jira_automation_record(slot_id)
        if record is None or record.report_status != "reported":
            return False
        if record.source == JIRA_ISSUE_AUTO_ANALYZE_SOURCE:
            return record.missing_required_material is not None
        if record.source == JIRA_STATUS_SUMMARY_SOURCE:
            return record.has_clear_resolution is not None
        return False

    def get_jira_automation_record(self, slot_id: str) -> JiraAutomationRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jira_automation_records WHERE slot_id=?", (slot_id,)).fetchone()
            return _row_to_jira_automation_record(row) if row else None

    def list_jira_automation_records(
        self,
        *,
        source: str | None = None,
        limit: int | None = 100,
        attention_only: bool = False,
        created_after: str | None = None,
    ) -> list[JiraAutomationRecord]:
        with self.connect() as conn:
            where_clauses: list[str] = []
            params: list[object] = []
            if source:
                where_clauses.append("source=?")
                params.append(source)
            if created_after:
                where_clauses.append("julianday(created_at)>=julianday(?)")
                params.append(created_after)
            if attention_only:
                if source == JIRA_ISSUE_AUTO_ANALYZE_SOURCE:
                    where_clauses.append("missing_required_material=1")
                elif source == JIRA_STATUS_SUMMARY_SOURCE:
                    where_clauses.append("has_clear_resolution=0")
                elif source is None:
                    where_clauses.append(
                        "((source=? AND missing_required_material=1) OR (source=? AND has_clear_resolution=0))"
                    )
                    params.extend([JIRA_ISSUE_AUTO_ANALYZE_SOURCE, JIRA_STATUS_SUMMARY_SOURCE])
                else:
                    where_clauses.append("0")
            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            limit_sql = "LIMIT ?" if limit is not None else ""
            query_params = (*params, limit) if limit is not None else tuple(params)
            rows = conn.execute(
                f"""
                SELECT * FROM jira_automation_records
                {where_sql}
                ORDER BY julianday(created_at) DESC
                {limit_sql}
                """,
                query_params,
            ).fetchall()
            return [_row_to_jira_automation_record(row) for row in rows]

    def recent_jira_issues(self, limit: int = 10) -> list[JiraIssueRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT i.issue_key, i.project_key, i.summary, i.reporter, i.assignee, i.slot_id,
                       s.status AS status,
                       i.status_name, i.status_id, i.status_category_key,
                       i.jira_created_at, i.jira_updated_at, i.observed_at
                FROM jira_issues i
                LEFT JOIN slots s ON s.slot_id = i.slot_id
                ORDER BY i.observed_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                JiraIssueRecord(
                    issue_key=row["issue_key"],
                    project_key=row["project_key"],
                    summary=row["summary"],
                    reporter=row["reporter"],
                    assignee=row["assignee"],
                    slot_id=row["slot_id"],
                    status=row["status"],
                    jira_status_name=row["status_name"],
                    jira_status_id=row["status_id"],
                    jira_status_category_key=row["status_category_key"],
                    jira_created_at=row["jira_created_at"],
                    jira_updated_at=row["jira_updated_at"],
                    observed_at=row["observed_at"],
                )
                for row in rows
            ]

    def recent_jira_comments(self, limit: int = 10) -> list[JiraCommentRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT c.comment_id, c.issue_key, c.project_key, c.author, c.body, c.slot_id,
                       s.status AS status,
                       c.jira_created_at, c.jira_updated_at, c.observed_at
                FROM jira_comments c
                LEFT JOIN slots s ON s.slot_id = c.slot_id
                ORDER BY c.observed_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                JiraCommentRecord(
                    comment_id=row["comment_id"],
                    issue_key=row["issue_key"],
                    project_key=row["project_key"],
                    author=row["author"],
                    body=row["body"],
                    slot_id=row["slot_id"],
                    status=row["status"],
                    jira_created_at=row["jira_created_at"],
                    jira_updated_at=row["jira_updated_at"],
                    observed_at=row["observed_at"],
                )
                for row in rows
            ]

    def mark_codex_started(self, slot_id: str, attempt: int, thread_id: str | None = None, turn_id: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE slots
                SET status='running', codex_status='running', codex_attempts=?, codex_thread_id=COALESCE(?, codex_thread_id),
                    codex_turn_id=COALESCE(?, codex_turn_id), updated_at=?
                WHERE slot_id=?
                """,
                (attempt, thread_id, turn_id, utc_now(), slot_id),
            )
            row = conn.execute("SELECT case_id FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
            if row and row["case_id"]:
                conn.execute(
                    "UPDATE analysis_cases SET status='running', latest_slot_id=?, updated_at=? WHERE case_id=?",
                    (slot_id, utc_now(), row["case_id"]),
                )

    def mark_codex_session(self, slot_id: str, thread_id: str | None = None, turn_id: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE slots
                SET codex_thread_id=COALESCE(?, codex_thread_id),
                    codex_turn_id=COALESCE(?, codex_turn_id),
                    updated_at=?
                WHERE slot_id=?
                """,
                (thread_id, turn_id, utc_now(), slot_id),
            )
            row = conn.execute("SELECT case_id FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
            if row and row["case_id"] and thread_id:
                now = utc_now()
                conn.execute(
                    """
                    UPDATE analysis_cases
                    SET primary_codex_thread_id=COALESCE(primary_codex_thread_id, ?),
                        latest_codex_thread_id=?,
                        latest_slot_id=?,
                        updated_at=?
                    WHERE case_id=?
                    """,
                    (thread_id, thread_id, slot_id, now, row["case_id"]),
                )
                self._register_case_alias(conn, "codex_thread_id", thread_id, row["case_id"])

    def mark_codex_turn(self, slot_id: str, thread_id: str, turn_id: str) -> None:
        self.mark_codex_session(slot_id, thread_id, turn_id)

    def mark_codex_completed(self, slot_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE slots
                SET codex_status='completed',
                    status=CASE WHEN reply_status IN ('pending','sent') THEN status ELSE 'codex_completed_no_reply' END,
                    updated_at=?
                WHERE slot_id=?
                """,
                (utc_now(), slot_id),
            )
            row = conn.execute("SELECT case_id FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
            if row and row["case_id"]:
                conn.execute("UPDATE analysis_cases SET status='idle', latest_slot_id=?, updated_at=? WHERE case_id=?", (slot_id, utc_now(), row["case_id"]))

    def mark_codex_skipped(self, slot_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE slots
                SET codex_status='skipped', updated_at=?
                WHERE slot_id=? AND codex_status='queued'
                """,
                (utc_now(), slot_id),
            )

    def mark_codex_failed(self, slot_id: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE slots SET status='failed', codex_status='failed', error=?, updated_at=? WHERE slot_id=?",
                (error, utc_now(), slot_id),
            )
            row = conn.execute("SELECT case_id FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
            if row and row["case_id"]:
                conn.execute("UPDATE analysis_cases SET status='failed', latest_slot_id=?, updated_at=? WHERE case_id=?", (slot_id, utc_now(), row["case_id"]))

    def record_reply_request(self, slot_id: str, message: str) -> None:
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE slots
                SET reply_text=?, reply_status='pending', status='reply_pending', updated_at=?
                WHERE slot_id=? AND reply_status != 'sent'
                """,
                (message, utc_now(), slot_id),
            )
            if cur.rowcount == 0:
                row = conn.execute("SELECT slot_id, reply_status FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
                if row is None:
                    raise KeyError(f"slot not found: {slot_id}")

    def pending_replies(self) -> list[Slot]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM slots
                WHERE reply_status='pending'
                ORDER BY updated_at ASC
                """
            ).fetchall()
            return [_row_to_slot(row) for row in rows]

    def mark_reply_sent(self, slot_id: str, lark_reply_message_id: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE slots
                SET reply_status='sent', status='replied', lark_reply_message_id=?, updated_at=?
                WHERE slot_id=?
                """,
                (lark_reply_message_id, utc_now(), slot_id),
            )
            if lark_reply_message_id:
                row = conn.execute("SELECT case_id FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
                if row and row["case_id"]:
                    self._register_case_alias(conn, "lark_message_id", lark_reply_message_id, row["case_id"])

    def mark_reply_failed(self, slot_id: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE slots
                SET reply_status='failed',
                    status='reply_failed',
                    error=CASE
                      WHEN error IS NULL OR error='' THEN ?
                      ELSE error || char(10) || ?
                    END,
                    updated_at=?
                WHERE slot_id=? AND reply_status != 'sent'
                """,
                (error, error, utc_now(), slot_id),
            )

    def has_reply(self, slot_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT reply_status FROM slots WHERE slot_id=?", (slot_id,)).fetchone()
            return bool(row and row["reply_status"] in {"pending", "sent"})

    def iter_events(self, limit: int = 50) -> Iterable[dict[str, Any]]:
        yield from self.recent_events(limit)


def _row_to_slot(row: sqlite3.Row) -> Slot:
    return Slot(
        slot_id=row["slot_id"],
        source=row["source"],
        lark_event_id=row["lark_event_id"],
        lark_message_id=row["lark_message_id"],
        chat_id=row["chat_id"],
        chat_type=row["chat_type"],
        sender_id=row["sender_id"],
        incoming_text=row["incoming_text"],
        status=row["status"],
        codex_status=row["codex_status"],
        reply_status=row["reply_status"],
        reply_text=row["reply_text"],
        codex_attempts=row["codex_attempts"],
        codex_thread_id=row["codex_thread_id"],
        codex_turn_id=row["codex_turn_id"],
        trigger_key=row["trigger_key"],
        jira_issue_key=row["jira_issue_key"],
        jira_comment_id=row["jira_comment_id"],
        github_repo=row["github_repo"],
        github_issue_number=row["github_issue_number"],
        case_id=row["case_id"],
        delivery_target=row["delivery_target"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_case(row: sqlite3.Row) -> AnalysisCase:
    return AnalysisCase(
        case_id=row["case_id"],
        scope_type=row["scope_type"],
        scope_key=row["scope_key"],
        title=row["title"],
        status=row["status"],
        primary_codex_thread_id=row["primary_codex_thread_id"],
        latest_codex_thread_id=row["latest_codex_thread_id"],
        latest_slot_id=row["latest_slot_id"],
        summary=row["summary"],
        bound_lark_chat_id=row["bound_lark_chat_id"],
        bound_lark_chat_name=row["bound_lark_chat_name"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_lark_message(row: sqlite3.Row) -> LarkMessageRecord:
    return LarkMessageRecord(
        message_id=row["message_id"],
        event_id=row["event_id"],
        chat_id=row["chat_id"],
        chat_type=row["chat_type"],
        chat_name=row["chat_name"],
        sender_id=row["sender_id"],
        message_type=row["message_type"],
        content=row["content"],
        mentioned_bot=bool(row["mentioned_bot"]),
        handled_slot_id=row["handled_slot_id"],
        observed_at=row["observed_at"],
    )


def _row_to_jira_automation_record(row: sqlite3.Row) -> JiraAutomationRecord:
    return JiraAutomationRecord(
        slot_id=row["slot_id"],
        source=row["source"],
        issue_key=row["issue_key"],
        case_id=row["case_id"],
        trigger_key=row["trigger_key"],
        subject_user=row["subject_user"],
        subject_role=row["subject_role"],
        jira_comment_id=row["jira_comment_id"],
        comment_verified_at=row["comment_verified_at"],
        report_status=row["report_status"],
        missing_required_material=_db_to_bool(row["missing_required_material"]),
        has_clear_resolution=_db_to_bool(row["has_clear_resolution"]),
        reason=row["reason"],
        details=_json_loads_dict(row["details_json"]),
        raw_report=_json_loads_dict(row["raw_report_json"]),
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _validate_jira_automation_report(source: str, report: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    kind = str(report.get("kind") or "")
    if kind != source:
        return False, f"report kind must be {source}, got {kind or '<missing>'}", {"details": {}}
    normalized: dict[str, Any] = {
        "reason": str(report.get("reason") or "").strip() or None,
        "details": {},
    }
    if source == JIRA_ISSUE_AUTO_ANALYZE_SOURCE:
        value = report.get("missing_required_material")
        if not isinstance(value, bool):
            return False, "missing_required_material must be a JSON boolean", normalized
        normalized["missing_required_material"] = value
        normalized["details"] = {
            "missing_materials": _string_list(report.get("missing_materials")),
            "confidence": str(report.get("confidence") or "").strip() or None,
        }
        return True, "automation report accepted", normalized
    if source == JIRA_STATUS_SUMMARY_SOURCE:
        value = report.get("has_clear_resolution")
        if not isinstance(value, bool):
            return False, "has_clear_resolution must be a JSON boolean", normalized
        normalized["has_clear_resolution"] = value
        normalized["details"] = {
            "resolution_gaps": _string_list(report.get("resolution_gaps")),
            "confidence": str(report.get("confidence") or "").strip() or None,
        }
        return True, "automation report accepted", normalized
    return False, f"unsupported automation source: {source}", normalized


def _string_list(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def _bool_to_db(value: object) -> int | None:
    return int(value) if isinstance(value, bool) else None


def _db_to_bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


def case_id_for_scope(scope_type: str, scope_key: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", scope_key.lower()).strip("_")
    normalized = normalized or uuid.uuid4().hex[:12]
    return f"case_{scope_type}_{normalized}"[:80]


def normalize_repo_full_name(repo_full_name: str) -> str:
    return str(repo_full_name).strip().strip("/").lower()


def format_github_issue_input(issue: sqlite3.Row) -> str:
    labels = []
    try:
        labels = [str(item.get("name") or "") for item in json.loads(str(issue["labels_json"] or "[]")) if item.get("name")]
    except (json.JSONDecodeError, AttributeError):
        labels = []
    parts = [
        f"GitHub issue: {issue['repo_full_name']}#{issue['issue_number']}",
        f"Title: {issue['title']}",
        f"URL: {issue['html_url']}",
        f"Author: {issue['author']}",
        f"State: {issue['state']}",
        f"Created: {issue['github_created_at']}",
        f"Updated: {issue['github_updated_at']}",
    ]
    if labels:
        parts.append(f"Labels: {', '.join(labels)}")
    body = str(issue["body"] or "").strip()
    parts.extend(["", "Issue body:", body or "(empty)"])
    return "\n".join(parts)


def _trigger_fragment(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value).strip()).strip("_")
    return normalized.lower() or "unknown"


def _json_loads_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        decoded = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _jira_user_display(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("displayName", "name", "emailAddress", "key", "accountId"):
        text = str(value.get(key) or "").strip()
        if text:
            return text
    return None


def lark_chat_scope_key(*, chat_id: str, chat_type: str | None, sender_id: str | None = None) -> str:
    normalized_type = (chat_type or "chat").lower()
    if normalized_type != "group" and sender_id:
        return f"{normalized_type}:user:{sender_id}"
    return f"{normalized_type}:{chat_id}"


def _lark_history_thread_is_adoptable(slot: Slot) -> bool:
    fallback_reply = "上一轮任务已完成"
    if slot.codex_attempts > 1 and fallback_reply in str(slot.reply_text or ""):
        return False
    return True
