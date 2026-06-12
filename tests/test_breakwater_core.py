from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import shlex
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import breakwater.admin_web as admin_web
from breakwater.cases import (
    ActiveCaseRegistry,
    CaseResolver,
    CodexSlotPlanner,
    extract_lark_reference_message_ids,
)
from breakwater.admin_web import BreakwaterAdminServer, hash_admin_password
from breakwater.cli import build_config, cmd_automation_report, cmd_case, cmd_github_comment, unique_log_file
from breakwater.codex_app_server import CodexAppServerClient, CodexCompactResult, CodexInputAttachment, CodexRunResult, CodexTurnSnapshot
from breakwater.config import AppConfig, CodexConfig, GitHubConfig, JiraConfig, ProxyConfig
from breakwater.db import Database
from breakwater.github_adapter import GitHubRestClient, gh_auth_token
from breakwater.github_monitor import GitHubMonitor
from breakwater.jira_monitor import JiraMonitor
from breakwater.lark_adapter import LarkEvent, extract_lark_chat_name, lark_message_mentions_bot
from breakwater.lark_content import normalize_lark_content
from breakwater.mentions import jira_comment_mentions_target
from breakwater.prompts import PromptLibrary
from breakwater.runtime import proxy_environment, recent_codex_threads
from breakwater.service import BreakwaterService
from breakwater.web import BreakwaterWebServer, render_status_page


TEST_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
    b"\xff?\x00\x05\xfe\x02\xfeA\xe2\xde\xfc\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeCodexClient:
    def __init__(self, db: Database, delay: float = 0.05):
        self.db = db
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []
        self.prompt_names: list[str] = []
        self.prompt_contexts: list[dict[str, object] | None] = []

    async def run_slot(
        self,
        *,
        slot_id: str,
        incoming_text: str,
        retry_prompt: str | None = None,
        prompt_name: str = "lark_initial",
        prompt_context: dict[str, object] | None = None,
        on_session_started=None,
        resume_thread_id: str | None = None,
    ) -> CodexRunResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls.append(slot_id)
        self.prompt_names.append(prompt_name)
        self.prompt_contexts.append(prompt_context)
        try:
            await asyncio.sleep(self.delay)
            self.db.record_reply_request(slot_id, f"reply for {incoming_text}")
            return CodexRunResult(
                thread_id=f"thread-{slot_id}",
                turn_id=f"turn-{slot_id}",
                status="completed",
                answer="done",
                duration_ms=1,
            )
        finally:
            self.active -= 1

    def retry_prompt(self, slot_id: str) -> str:
        return f"retry {slot_id}"


class NoopCodexServer:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class LiveSessionCodexClient:
    def __init__(self, db: Database, delay: float = 0.15):
        self.db = db
        self.delay = delay

    async def run_slot(
        self,
        *,
        slot_id: str,
        incoming_text: str,
        retry_prompt: str | None = None,
        prompt_name: str = "lark_initial",
        prompt_context: dict[str, object] | None = None,
        on_session_started=None,
        resume_thread_id: str | None = None,
    ) -> CodexRunResult:
        if on_session_started:
            await on_session_started("thread-live", None)
            await on_session_started("thread-live", "turn-live")
        await asyncio.sleep(self.delay)
        self.db.record_reply_request(slot_id, "live reply")
        return CodexRunResult(
            thread_id="thread-live",
            turn_id="turn-live",
            status="completed",
            answer="done",
            duration_ms=1,
        )

    def retry_prompt(self, slot_id: str) -> str:
        return f"retry {slot_id}"


class MissingReplyOnceCodexClient:
    def __init__(self, db: Database):
        self.db = db
        self.calls = 0
        self.resume_thread_ids: list[str | None] = []

    async def run_slot(
        self,
        *,
        slot_id: str,
        incoming_text: str,
        retry_prompt: str | None = None,
        prompt_name: str = "lark_initial",
        prompt_context: dict[str, object] | None = None,
        on_session_started=None,
        resume_thread_id: str | None = None,
    ) -> CodexRunResult:
        self.calls += 1
        self.resume_thread_ids.append(resume_thread_id)
        if self.calls == 2:
            self.db.record_reply_request(slot_id, "second attempt reply")
        thread_id = resume_thread_id or f"thread-{self.calls}"
        return CodexRunResult(
            thread_id=thread_id,
            turn_id=f"turn-{self.calls}",
            status="completed",
            answer="done",
            duration_ms=1,
        )

    def retry_prompt(self, slot_id: str) -> str:
        return f"retry {slot_id}"


class NoReplyCodexClient:
    def __init__(self) -> None:
        self.calls = 0
        self.prompt_names: list[str] = []
        self.prompt_contexts: list[dict[str, object] | None] = []
        self.retry_prompts: list[str | None] = []

    async def run_slot(
        self,
        *,
        slot_id: str,
        incoming_text: str,
        retry_prompt: str | None = None,
        prompt_name: str = "lark_initial",
        prompt_context: dict[str, object] | None = None,
        on_session_started=None,
        resume_thread_id: str | None = None,
    ) -> CodexRunResult:
        self.calls += 1
        self.prompt_names.append(prompt_name)
        self.prompt_contexts.append(prompt_context)
        self.retry_prompts.append(retry_prompt)
        return CodexRunResult(
            thread_id=f"thread-{self.calls}",
            turn_id=f"turn-{self.calls}",
            status="completed",
            answer="done",
            duration_ms=1,
        )

    def retry_prompt(self, slot_id: str) -> str:
        return f"retry {slot_id}"

    def jira_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"jira retry {slot_id} {prompt_context['jira_marker']}"

    def jira_issue_auto_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"jira auto retry {slot_id} {prompt_context['jira_marker']} automation-report"

    def jira_status_summary_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"jira status retry {slot_id} {prompt_context['jira_marker']}"

    def lark_jira_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"lark jira retry {slot_id} {prompt_context['jira_marker']}"

    def github_issue_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"github retry {slot_id} {prompt_context['github_marker']}"


class AutomationReportCodexClient(NoReplyCodexClient):
    def __init__(self, db: Database, report: dict[str, object]):
        super().__init__()
        self.db = db
        self.report = report

    async def run_slot(
        self,
        *,
        slot_id: str,
        incoming_text: str,
        retry_prompt: str | None = None,
        prompt_name: str = "lark_initial",
        prompt_context: dict[str, object] | None = None,
        on_session_started=None,
        resume_thread_id: str | None = None,
    ) -> CodexRunResult:
        result = await super().run_slot(
            slot_id=slot_id,
            incoming_text=incoming_text,
            retry_prompt=retry_prompt,
            prompt_name=prompt_name,
            prompt_context=prompt_context,
            on_session_started=on_session_started,
            resume_thread_id=resume_thread_id,
        )
        ok, message = self.db.record_jira_automation_report(slot_id, self.report)
        if not ok:
            raise AssertionError(message)
        return result


class FailingCodexClient(NoReplyCodexClient):
    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.error = error or RuntimeError("codex websocket dropped")
        self.resume_thread_ids: list[str | None] = []

    async def run_slot(
        self,
        *,
        slot_id: str,
        incoming_text: str,
        retry_prompt: str | None = None,
        prompt_name: str = "lark_initial",
        prompt_context: dict[str, object] | None = None,
        on_session_started=None,
        resume_thread_id: str | None = None,
    ) -> CodexRunResult:
        self.calls += 1
        self.prompt_names.append(prompt_name)
        self.prompt_contexts.append(prompt_context)
        self.retry_prompts.append(retry_prompt)
        self.resume_thread_ids.append(resume_thread_id)
        if on_session_started:
            await on_session_started(resume_thread_id or "thread-failed", None)
            await on_session_started(resume_thread_id or "thread-failed", f"turn-{self.calls}")
        raise self.error


class RunningAfterTransportErrorCodexClient(FailingCodexClient):
    def __init__(self) -> None:
        super().__init__(RuntimeError("websocket message too big"))
        self.read_turn_calls: list[tuple[str, str]] = []

    async def read_turn(self, thread_id: str, turn_id: str) -> CodexTurnSnapshot:
        self.read_turn_calls.append((thread_id, turn_id))
        return CodexTurnSnapshot(thread_id=thread_id, turn_id=turn_id, status="inProgress")


class TerminalAfterTransportErrorCodexClient(NoReplyCodexClient):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self.read_turn_calls: list[tuple[str, str]] = []
        self.resume_thread_ids: list[str | None] = []

    async def run_slot(
        self,
        *,
        slot_id: str,
        incoming_text: str,
        retry_prompt: str | None = None,
        prompt_name: str = "lark_initial",
        prompt_context: dict[str, object] | None = None,
        on_session_started=None,
        resume_thread_id: str | None = None,
    ) -> CodexRunResult:
        self.calls += 1
        self.prompt_names.append(prompt_name)
        self.prompt_contexts.append(prompt_context)
        self.retry_prompts.append(retry_prompt)
        self.resume_thread_ids.append(resume_thread_id)
        if self.calls == 1:
            if on_session_started:
                await on_session_started("thread-transport", None)
                await on_session_started("thread-transport", "turn-transport")
            raise RuntimeError("websocket message too big")
        thread_id = resume_thread_id or "thread-transport"
        if on_session_started:
            await on_session_started(thread_id, "turn-retry")
        return CodexRunResult(
            thread_id=thread_id,
            turn_id="turn-retry",
            status="completed",
            answer="done",
            duration_ms=1,
        )

    async def read_turn(self, thread_id: str, turn_id: str) -> CodexTurnSnapshot:
        self.read_turn_calls.append((thread_id, turn_id))
        return CodexTurnSnapshot(thread_id=thread_id, turn_id=turn_id, status="interrupted")


class ContinueRecordingCodexClient:
    def __init__(self, db: Database):
        self.db = db
        self.retry_prompts: list[str | None] = []
        self.resume_thread_ids: list[str | None] = []

    async def run_slot(
        self,
        *,
        slot_id: str,
        incoming_text: str,
        retry_prompt: str | None = None,
        prompt_name: str = "lark_initial",
        prompt_context: dict[str, object] | None = None,
        on_session_started=None,
        resume_thread_id: str | None = None,
    ) -> CodexRunResult:
        self.retry_prompts.append(retry_prompt)
        self.resume_thread_ids.append(resume_thread_id)
        if on_session_started:
            await on_session_started(resume_thread_id or "thread-new", "turn-continue")
        self.db.record_reply_request(slot_id, "continued")
        return CodexRunResult(
            thread_id=resume_thread_id or "thread-new",
            turn_id="turn-continue",
            status="completed",
            answer="done",
            duration_ms=1,
        )

    def retry_prompt(self, slot_id: str) -> str:
        return f"retry {slot_id}"


class CaseRecordingCodexClient:
    def __init__(self, db: Database, *, reply: bool = False):
        self.db = db
        self.reply = reply
        self.prompt_names: list[str] = []
        self.prompt_contexts: list[dict[str, object] | None] = []
        self.resume_thread_ids: list[str | None] = []
        self.retry_prompts: list[str | None] = []
        self.incoming_texts: list[str] = []
        self.input_attachments: list[list[CodexInputAttachment]] = []

    async def run_slot(
        self,
        *,
        slot_id: str,
        incoming_text: str,
        input_attachments: list[CodexInputAttachment] | None = None,
        retry_prompt: str | None = None,
        prompt_name: str = "lark_initial",
        prompt_context: dict[str, object] | None = None,
        on_session_started=None,
        resume_thread_id: str | None = None,
    ) -> CodexRunResult:
        self.prompt_names.append(prompt_name)
        self.prompt_contexts.append(prompt_context)
        self.resume_thread_ids.append(resume_thread_id)
        self.retry_prompts.append(retry_prompt)
        self.incoming_texts.append(incoming_text)
        self.input_attachments.append(list(input_attachments or []))
        thread_id = resume_thread_id or "thread-case-new"
        if on_session_started:
            await on_session_started(thread_id, "turn-case")
        if self.reply:
            self.db.record_reply_request(slot_id, "case followup reply")
        return CodexRunResult(
            thread_id=thread_id,
            turn_id="turn-case",
            status="completed",
            answer="done",
            duration_ms=1,
        )

    def retry_prompt(self, slot_id: str) -> str:
        return f"retry {slot_id}"

    def jira_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"jira retry {slot_id}"

    def jira_issue_auto_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"jira auto retry {slot_id}"

    def jira_status_summary_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"jira status retry {slot_id}"

    def lark_jira_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"lark jira retry {slot_id} {prompt_context['jira_marker']}"

    def github_issue_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"github retry {slot_id}"


class ResumeFailsOnceCodexClient:
    def __init__(self, db: Database):
        self.db = db
        self.resume_thread_ids: list[str | None] = []
        self.prompt_names: list[str] = []

    async def run_slot(
        self,
        *,
        slot_id: str,
        incoming_text: str,
        retry_prompt: str | None = None,
        prompt_name: str = "lark_initial",
        prompt_context: dict[str, object] | None = None,
        on_session_started=None,
        resume_thread_id: str | None = None,
    ) -> CodexRunResult:
        self.resume_thread_ids.append(resume_thread_id)
        self.prompt_names.append(prompt_name)
        if resume_thread_id:
            raise RuntimeError("thread not found")
        if on_session_started:
            await on_session_started("thread-recreated", "turn-recreated")
        self.db.record_reply_request(slot_id, "recovered reply")
        return CodexRunResult(
            thread_id="thread-recreated",
            turn_id="turn-recreated",
            status="completed",
            answer="done after recreate",
            duration_ms=1,
        )

    def retry_prompt(self, slot_id: str) -> str:
        return f"retry {slot_id}"

    def jira_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"jira retry {slot_id}"

    def jira_issue_auto_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"jira auto retry {slot_id}"

    def jira_status_summary_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"jira status retry {slot_id}"

    def lark_jira_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"lark jira retry {slot_id}"

    def github_issue_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"github retry {slot_id}"


class ContextWindowThenCompactSuccessCodexClient:
    def __init__(self, db: Database):
        self.db = db
        self.resume_thread_ids: list[str | None] = []
        self.retry_prompts: list[str | None] = []
        self.compact_thread_ids: list[str] = []
        self.calls = 0

    async def run_slot(
        self,
        *,
        slot_id: str,
        incoming_text: str,
        retry_prompt: str | None = None,
        prompt_name: str = "lark_initial",
        prompt_context: dict[str, object] | None = None,
        on_session_started=None,
        resume_thread_id: str | None = None,
    ) -> CodexRunResult:
        self.calls += 1
        self.resume_thread_ids.append(resume_thread_id)
        self.retry_prompts.append(retry_prompt)
        if self.calls == 1:
            if on_session_started:
                await on_session_started(resume_thread_id, "turn-context")
            return CodexRunResult(
                thread_id=resume_thread_id or "thread-full",
                turn_id="turn-context",
                status="failed",
                answer="",
                duration_ms=1,
                error_message="Codex ran out of room in the model's context window. Start a new thread or clear earlier history before retrying.",
                error_info="contextWindowExceeded",
            )
        if on_session_started:
            await on_session_started(resume_thread_id or "thread-full", "turn-after-compact")
        self.db.record_reply_request(slot_id, "recovered after context window")
        return CodexRunResult(
            thread_id=resume_thread_id or "thread-full",
            turn_id="turn-after-compact",
            status="completed",
            answer="done after compact",
            duration_ms=1,
        )

    async def compact_thread(self, thread_id: str) -> CodexCompactResult:
        self.compact_thread_ids.append(thread_id)
        return CodexCompactResult(
            thread_id=thread_id,
            turn_id="turn-compact",
            status="completed",
            duration_ms=1,
        )

    def retry_prompt(self, slot_id: str) -> str:
        return f"retry {slot_id}"

    def jira_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"jira retry {slot_id}"

    def jira_issue_auto_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"jira auto retry {slot_id}"

    def jira_status_summary_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"jira status retry {slot_id}"

    def lark_jira_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"lark jira retry {slot_id}"

    def github_issue_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return f"github retry {slot_id}"


class FakeJiraClient:
    def __init__(self) -> None:
        self.search_calls: list[str] = []
        self.comment_calls: list[str] = []

    def search_all_issues(self, jql: str, fields: list[str]) -> list[dict]:
        self.search_calls.append(jql)
        if "created >=" in jql:
            return [
                {
                    "key": "APP-1",
                    "fields": {
                        "project": {"key": "APP"},
                        "summary": "new issue",
                        "created": "2026-05-21T17:30:30.000+0800",
                        "updated": "2026-05-21T17:30:30.000+0800",
                    },
                }
            ]
        return [
            {
                "key": "APP-1",
                "fields": {
                    "project": {"key": "APP"},
                    "summary": "changed issue",
                    "updated": "2026-05-21T17:31:00.000+0800",
                },
            }
        ]

    def list_comments(self, issue_key: str) -> list[dict]:
        self.comment_calls.append(issue_key)
        return [
            {
                "id": "c1",
                "body": "/analyze please",
                "created": "2026-05-21T17:31:00.000+0800",
                "updated": "2026-05-21T17:31:00.000+0800",
                "author": {"displayName": "Alice"},
            },
            {
                "id": "c2",
                "body": "  /ANALYZE edited later",
                "created": "2026-05-21T17:00:00.000+0800",
                "updated": "2026-05-21T17:31:10.000+0800",
                "author": {"displayName": "Bob"},
            },
            {
                "id": "c3",
                "body": "normal comment",
                "created": "2026-05-21T17:31:30.000+0800",
                "updated": "2026-05-21T17:31:30.000+0800",
                "author": {"displayName": "Carol"},
            },
            {
                "id": "c4",
                "body": "@Breakwater please analyze",
                "created": "2026-05-21T17:31:40.000+0800",
                "updated": "2026-05-21T17:31:40.000+0800",
                "author": {"displayName": "Dave"},
            },
            {
                "id": "c5",
                "body": "[~breakwater-bot] please analyze",
                "created": "2026-05-21T17:31:50.000+0800",
                "updated": "2026-05-21T17:31:50.000+0800",
                "author": {"displayName": "Eve"},
            },
        ]


class FakeJiraBreakwaterAnalysisOutputClient:
    def __init__(self) -> None:
        self.search_calls: list[str] = []
        self.comment_calls: list[str] = []

    def search_all_issues(self, jql: str, fields: list[str]) -> list[dict]:
        self.search_calls.append(jql)
        if "updated >=" not in jql:
            return []
        return [
            {
                "key": "OPS-20399",
                "fields": {
                    "project": {"key": "OPS"},
                    "summary": "changed issue",
                    "updated": "2026-05-21T17:31:00.000+0800",
                },
            }
        ]

    def list_comments(self, issue_key: str) -> list[dict]:
        self.comment_calls.append(issue_key)
        return [
            {
                "id": "c-self",
                "body": (
                    "Breakwater-Analysis-Slot: slot_prev\n\n"
                    "新触发评论内容为：`[~breakwater-bot] 快速返回结果`"
                ),
                "created": "2026-05-21T17:31:00.000+0800",
                "updated": "2026-05-21T17:31:00.000+0800",
                "author": {"displayName": "breakwater", "name": "breakwater"},
            }
        ]


class FakeJiraMultiProjectClient:
    def __init__(self) -> None:
        self.search_calls: list[str] = []
        self.comment_calls: list[str] = []

    def search_all_issues(self, jql: str, fields: list[str]) -> list[dict]:
        self.search_calls.append(jql)
        if "created >=" in jql:
            return [
                {
                    "key": "OPS-1",
                    "fields": {
                        "project": {"key": "OPS"},
                        "summary": "ops new issue",
                        "created": "2026-05-21T17:30:30.000+0800",
                        "updated": "2026-05-21T17:30:30.000+0800",
                    },
                },
                {
                    "key": "APP-1",
                    "fields": {
                        "project": {"key": "APP"},
                        "summary": "app new issue",
                        "created": "2026-05-21T17:30:40.000+0800",
                        "updated": "2026-05-21T17:30:40.000+0800",
                    },
                },
            ]
        return []

    def list_comments(self, issue_key: str) -> list[dict]:
        self.comment_calls.append(issue_key)
        return []


class FakeJiraNoAnalyzeClient:
    def __init__(self) -> None:
        self.search_calls: list[str] = []
        self.field_calls: list[list[str]] = []
        self.comment_calls: list[str] = []

    def search_all_issues(self, jql: str, fields: list[str]) -> list[dict]:
        self.search_calls.append(jql)
        self.field_calls.append(fields)
        if "created >=" not in jql:
            return []
        return [
            {
                "key": "OPS-2",
                "fields": {
                    "project": {"key": "OPS"},
                    "summary": "routine new issue",
                    "description": "operator explicitly wrote /NO-ANALYZE in the card body",
                    "created": "2026-05-21T17:30:30.000+0800",
                    "updated": "2026-05-21T17:30:30.000+0800",
                },
            }
        ]

    def list_comments(self, issue_key: str) -> list[dict]:
        self.comment_calls.append(issue_key)
        return []


def fake_jira_status(name: str) -> dict:
    status_id = {"Done": "10001", "Backlog": "10003", "In Progress": "3"}.get(name, "10000")
    category_key = "done" if name == "Done" else ("new" if name == "Backlog" else "indeterminate")
    return {
        "name": name,
        "id": status_id,
        "statusCategory": {"key": category_key, "name": name},
    }


class FakeJiraStatusTransitionClient:
    def __init__(self, statuses: list[str], *, description: str = "status summary candidate") -> None:
        self.statuses = statuses
        self.description = description
        self.search_calls: list[str] = []
        self.field_calls: list[list[str]] = []
        self.comment_calls: list[str] = []
        self.status_calls = 0

    def search_all_issues(self, jql: str, fields: list[str]) -> list[dict]:
        self.search_calls.append(jql)
        self.field_calls.append(fields)
        if "status" not in fields:
            return []
        index = min(self.status_calls, len(self.statuses) - 1)
        self.status_calls += 1
        status = self.statuses[index]
        return [
            {
                "key": "OPS-300",
                "fields": {
                    "project": {"key": "OPS"},
                    "summary": "status summary issue",
                    "description": self.description,
                    "status": fake_jira_status(status),
                    "created": "2026-05-21T17:00:00.000+0800",
                    "updated": f"2026-05-21T17:3{index}:00.000+0800",
                },
            }
        ]

    def list_comments(self, issue_key: str) -> list[dict]:
        self.comment_calls.append(issue_key)
        return []


def fake_github_issue_payload(
    repo_full_name: str,
    issue_number: int,
    title: str,
    *,
    created_at: str = "2026-05-21T09:31:30Z",
    updated_at: str | None = None,
    author: str = "alice",
    labels: list[dict[str, object]] | None = None,
    body: str | None = None,
    state: str = "open",
    **extra: object,
) -> dict[str, object]:
    updated = updated_at or created_at
    payload: dict[str, object] = {
        "id": issue_number,
        "node_id": f"I_{issue_number}",
        "url": f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}",
        "repository_url": f"https://api.github.com/repos/{repo_full_name}",
        "labels_url": f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}/labels{{/name}}",
        "comments_url": f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}/comments",
        "events_url": f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}/events",
        "html_url": f"https://github.com/{repo_full_name}/issues/{issue_number}",
        "number": issue_number,
        "state": state,
        "title": title,
        "body": body if body is not None else f"{title} body",
        "user": {
            "login": author,
            "id": issue_number * 10,
            "node_id": f"U_{author}",
            "avatar_url": f"https://avatars.githubusercontent.com/u/{issue_number * 10}?v=4",
            "html_url": f"https://github.com/{author}",
            "type": "User",
            "site_admin": False,
        },
        "labels": labels
        if labels is not None
        else [
            {
                "id": issue_number * 100,
                "node_id": f"LA_{issue_number}",
                "url": f"https://api.github.com/repos/{repo_full_name}/labels/bug",
                "name": "bug",
                "color": "d73a4a",
                "default": True,
                "description": "Something is not working",
            }
        ],
        "comments": 0,
        "created_at": created_at,
        "updated_at": updated,
        "closed_at": None,
        "author_association": "NONE",
        "active_lock_reason": None,
        "draft": False,
        "pull_request": None,
    }
    payload.update(extra)
    return payload


def fake_github_pull_request_payload(
    repo_full_name: str,
    issue_number: int,
    title: str,
    *,
    created_at: str = "2026-05-21T09:31:35Z",
) -> dict[str, object]:
    payload = fake_github_issue_payload(repo_full_name, issue_number, title, created_at=created_at)
    payload["pull_request"] = {
        "url": f"https://api.github.com/repos/{repo_full_name}/pulls/{issue_number}",
        "html_url": f"https://github.com/{repo_full_name}/pull/{issue_number}",
        "diff_url": f"https://github.com/{repo_full_name}/pull/{issue_number}.diff",
        "patch_url": f"https://github.com/{repo_full_name}/pull/{issue_number}.patch",
        "merged_at": None,
    }
    return payload


class FakeGitHubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def list_repository_issues_since(self, repo_full_name: str, since_iso: str) -> list[dict]:
        self.calls.append((repo_full_name, since_iso))
        return [
            fake_github_issue_payload(repo_full_name, 10, "[Bug] new issue"),
            fake_github_pull_request_payload(repo_full_name, 11, "[bug] pull request should be ignored"),
            fake_github_issue_payload(
                repo_full_name,
                12,
                "[BUG] old updated issue",
                created_at="2026-05-21T09:00:00Z",
                updated_at="2026-05-21T09:31:40Z",
                author="bob",
                labels=[],
            ),
        ]


class FakeGitHubMixedTitleClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def list_repository_issues_since(self, repo_full_name: str, since_iso: str) -> list[dict]:
        self.calls.append((repo_full_name, since_iso))
        return [
            fake_github_issue_payload(repo_full_name, 20, "[BUG] uppercase bug title"),
            fake_github_issue_payload(repo_full_name, 21, "[Bug] mixed case bug title", author="bob"),
            fake_github_issue_payload(repo_full_name, 22, "ordinary issue title", author="carol"),
            fake_github_issue_payload(repo_full_name, 23, "[bugfix] not the exact bug prefix", author="dave"),
            fake_github_issue_payload(repo_full_name, 24, "prefix [bug] appears later", author="erin"),
            fake_github_pull_request_payload(repo_full_name, 25, "[bug] pull request title"),
        ]


class FakeGitHubBootstrapOverlapClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def list_repository_issues_since(self, repo_full_name: str, since_iso: str) -> list[dict]:
        self.calls.append((repo_full_name, since_iso))
        return [
            fake_github_issue_payload(
                repo_full_name,
                8,
                "[bug] pre-bootstrap issue",
                body="should not be backfilled after bootstrap",
                created_at="2026-05-21T09:29:30Z",
                updated_at="2026-05-21T09:31:30Z",
                labels=[],
            )
        ]


class FakeGitHubVerifier:
    def __init__(self, comment_batches: list[list[dict]]):
        self.comment_batches = comment_batches
        self.calls: list[tuple[str, int]] = []

    def list_issue_comments(self, repo_full_name: str, issue_number: int) -> list[dict]:
        self.calls.append((repo_full_name, issue_number))
        index = min(len(self.calls) - 1, len(self.comment_batches) - 1)
        return self.comment_batches[index]


class FakeGitHubCommentClient:
    def __init__(self, *, token: str | None = None, api_url: str = "https://api.github.com", page_size: int = 100):
        self.token = token
        self.api_url = api_url
        self.page_size = page_size
        self.created: list[tuple[str, int, str]] = []

    def create_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict:
        self.created.append((repo_full_name, issue_number, body))
        return {"id": 991}


class FakeJiraVerifier:
    def __init__(self, comment_batches: list[list[dict]]):
        self.comment_batches = comment_batches
        self.calls: list[str] = []

    def list_comments(self, issue_key: str) -> list[dict]:
        self.calls.append(issue_key)
        index = min(len(self.calls) - 1, len(self.comment_batches) - 1)
        return self.comment_batches[index]


class FakeLarkReplyClient:
    app_id = "cli_bot"

    def __init__(
        self,
        chat_names: dict[str, str] | None = None,
        mentioned_message_ids: set[str] | None = None,
        image_bytes_by_key: dict[str, bytes] | None = None,
    ) -> None:
        self.replies: list[tuple[str, str]] = []
        self.chat_names = chat_names or {}
        self.mentioned_message_ids = mentioned_message_ids or set()
        self.image_bytes_by_key = image_bytes_by_key or {}
        self.image_downloads: list[tuple[str, str, Path]] = []

    def reply_text(self, message_id: str, text: str) -> str:
        self.replies.append((message_id, text))
        return f"reply-{len(self.replies)}"

    def get_chat_name(self, chat_id: str) -> str:
        return self.chat_names.get(chat_id, "")

    def message_mentions_bot(self, message_id: str) -> bool:
        return message_id in self.mentioned_message_ids

    def download_message_image(self, *, message_id: str, file_key: str, destination: Path) -> Path:
        self.image_downloads.append((message_id, file_key, destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        path = destination.with_suffix(".png")
        path.write_bytes(self.image_bytes_by_key.get(file_key, TEST_PNG_BYTES))
        return path


class FailingLarkReplyClient(FakeLarkReplyClient):
    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error

    def reply_text(self, message_id: str, text: str) -> str:
        self.replies.append((message_id, text))
        raise RuntimeError(self.error)


def feishu_post_content(*, image_key: str = "img_v3_real_key", prefix: str = "版本是4.1.0的存算分离模式") -> str:
    return json.dumps(
        {
            "title": "",
            "content": [
                [{"tag": "text", "text": prefix, "style": []}],
                [{"tag": "text", "text": "主动增量预热任务一直没结束", "style": []}],
                [{"tag": "img", "image_key": image_key, "width": 1800, "height": 150}],
                [{"tag": "text", "text": "周期同步看起来也卡住了", "style": []}],
            ],
        },
        ensure_ascii=False,
    )


def feishu_post_raw(
    *,
    event_id: str,
    message_id: str,
    chat_id: str,
    chat_type: str,
    sender_id: str,
    content: str,
) -> dict[str, object]:
    return {
        "schema": "2.0",
        "type": "im.message.receive_v1",
        "event_id": event_id,
        "event": {
            "sender": {"sender_id": {"open_id": sender_id}},
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": "post",
                "body": {"content": content},
            },
        },
        "data": {
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": "post",
                "body": {"content": content},
            }
        },
    }


class BreakwaterCoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "breakwater.db"
        self.db = Database(self.db_path)
        self.db.init()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def config(self, *, concurrency: int = 5, retries: int = 0) -> AppConfig:
        return AppConfig(
            db_path=self.db_path,
            codex=CodexConfig(start_server=False, max_reply_retries=retries),
            codex_concurrency=concurrency,
            web_enabled=False,
        )

    def test_service_injects_configured_tokens_into_codex_server_and_tasks(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            service = BreakwaterService(
                AppConfig(
                    db_path=self.db_path,
                    codex=CodexConfig(start_server=False),
                    jira=JiraConfig(token="config-token"),
                    github=GitHubConfig(token="github-token"),
                    proxy=ProxyConfig(enabled=False),
                    web_enabled=False,
                )
            )

        self.assertEqual(service.codex_server.env_overrides["JIRA_TOKEN"], "config-token")
        self.assertEqual(service.codex_server.env_overrides["GITHUB_TOKEN"], "github-token")
        self.assertEqual(service.codex_client.task_env["JIRA_TOKEN"], "config-token")
        self.assertEqual(service.codex_client.task_env["GITHUB_TOKEN"], "github-token")

    def test_event_stream_is_process_local_not_persisted_to_sqlite(self) -> None:
        self.db.log_event("test", "started", "process-only event", "slot-test", detail="visible")

        self.assertEqual(self.db.recent_events(limit=1)[0]["event_type"], "started")
        with self.db.connect() as conn:
            persisted = conn.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
        self.assertEqual(persisted, 0)

    async def test_recovered_running_slot_resumes_existing_thread_with_continue(self) -> None:
        service = BreakwaterService(self.config(concurrency=1))
        slot = service.db.create_slot(source="local", incoming_text="original work")
        service.db.mark_codex_started(slot.slot_id, 1, "thread-old", "turn-old")
        recovered = service.db.get_slot(slot.slot_id)
        assert recovered is not None
        fake = ContinueRecordingCodexClient(service.db)
        service.codex_client = fake  # type: ignore[assignment]

        service._recover_queued_slots()
        await service._run_codex_for_slot(recovered)

        self.assertEqual(fake.retry_prompts, ["继续"])
        self.assertEqual(fake.resume_thread_ids, ["thread-old"])
        refreshed = service.db.get_slot(slot.slot_id)
        assert refreshed is not None
        self.assertEqual(refreshed.reply_status, "pending")
        self.assertEqual(refreshed.codex_status, "completed")

    async def test_codex_queue_limits_concurrent_runs_and_leaves_excess_queued(self) -> None:
        service = BreakwaterService(self.config(concurrency=2))
        fake = FakeCodexClient(service.db, delay=0.08)
        service.codex_client = fake  # type: ignore[assignment]
        service._start_codex_workers()
        slots = [service.db.create_slot(source="local", incoming_text=f"task {i}") for i in range(5)]

        for slot in slots:
            service.enqueue_slot(slot)
        await asyncio.sleep(0.02)

        snapshot = service.status_snapshot()
        self.assertEqual(snapshot["queue"]["running"], 2)
        self.assertEqual(snapshot["queue"]["queued"], 3)

        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            if all((service.db.get_slot(slot.slot_id) or slot).codex_status == "completed" for slot in slots):
                break
            await asyncio.sleep(0.02)
        await service.stop()

        self.assertLessEqual(fake.max_active, 2)
        self.assertEqual(len(fake.calls), 5)

    async def test_lark_listener_supervisor_restarts_consumer_and_handles_next_message(self) -> None:
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FakeLarkReplyClient()  # type: ignore[assignment]
        service._lark_listener_retry_base_seconds = 0.01
        service._lark_listener_retry_max_seconds = 0.01
        processed_after_restart = asyncio.Event()

        event_after_restart = LarkEvent(
            event_id="event-after-listener-restart",
            message_id="message-after-listener-restart",
            chat_id="direct-restart-chat",
            chat_type="p2p",
            message_type="text",
            sender_id="ou_restart_user",
            content="先分析这个非 Jira 问题",
            raw={
                "schema": "2.0",
                "type": "im.message.receive_v1",
                "event_id": "event-after-listener-restart",
                "event": {
                    "sender": {"sender_id": {"open_id": "ou_restart_user"}},
                    "message": {
                        "message_id": "message-after-listener-restart",
                        "chat_id": "direct-restart-chat",
                        "chat_type": "p2p",
                        "message_type": "text",
                        "body": {"content": "先分析这个非 Jira 问题"},
                    },
                },
            },
        )

        class FakeLarkEventConsumer:
            def __init__(self, *, error: str | None = None, events: list[LarkEvent] | None = None) -> None:
                self.error = error
                self.events_to_emit = events or []
                self.is_running = True
                self.stopped = False
                self._blocker = asyncio.Event()

            async def events(self):
                if self.error:
                    raise RuntimeError(self.error)
                for item in self.events_to_emit:
                    yield item
                if self.events_to_emit:
                    processed_after_restart.set()
                await self._blocker.wait()

            async def stop(self) -> None:
                self.stopped = True
                self.is_running = False
                self._blocker.set()

        consumers: list[FakeLarkEventConsumer] = []

        def factory() -> FakeLarkEventConsumer:
            if not consumers:
                consumer = FakeLarkEventConsumer(error="lark-cli event consumer exited code=1")
            else:
                consumer = FakeLarkEventConsumer(events=[event_after_restart])
            consumers.append(consumer)
            return consumer

        service._new_lark_consumer = factory  # type: ignore[method-assign]
        task = asyncio.create_task(service._lark_listener_supervisor_loop(), name="test-lark-listener")
        try:
            await asyncio.wait_for(processed_after_restart.wait(), timeout=2)
            snapshot = service.status_snapshot()
        finally:
            service._stop.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        self.assertGreaterEqual(len(consumers), 2)
        self.assertTrue(consumers[0].stopped)
        self.assertTrue(consumers[1].stopped)
        self.assertEqual(service._lark_listener_restart_count, 1)
        self.assertIn("lark-cli event consumer exited code=1", service._lark_listener_last_error or "")
        self.assertIsNotNone(service._lark_listener_last_event_at)
        self.assertTrue(snapshot["lark_listener"]["running"])
        self.assertTrue(snapshot["lark_listener"]["consumer_running"])
        self.assertEqual(snapshot["lark_listener"]["restart_count"], 1)

        message = next(
            item
            for item in self.db.recent_lark_messages(limit=5)
            if item.message_id == "message-after-listener-restart"
        )
        self.assertEqual(message.chat_type, "p2p")
        self.assertEqual(message.sender_id, "ou_restart_user")
        self.assertEqual(message.content, "先分析这个非 Jira 问题")
        self.assertIsNotNone(message.handled_slot_id)
        assert message.handled_slot_id is not None
        self.assertIn(message.handled_slot_id, service._queued_slot_ids)
        events = self.db.recent_events(limit=10)
        self.assertIn("listener.failed", {event["event_type"] for event in events})
        self.assertGreaterEqual(sum(1 for event in events if event["event_type"] == "listener.started"), 2)

    async def test_lark_consumer_keeps_stream_after_single_message_handler_failure(self) -> None:
        service = BreakwaterService(self.config(concurrency=1))
        handled_message_ids: list[str] = []
        bad = LarkEvent(
            event_id="event-handler-bad",
            message_id="message-handler-bad",
            chat_id="chat-handler",
            chat_type="p2p",
            message_type="text",
            sender_id="ou_user",
            content="bad",
            raw={"event_id": "event-handler-bad"},
        )
        good = LarkEvent(
            event_id="event-handler-good",
            message_id="message-handler-good",
            chat_id="chat-handler",
            chat_type="p2p",
            message_type="text",
            sender_id="ou_user",
            content="good",
            raw={"event_id": "event-handler-good"},
        )

        async def handle(event: LarkEvent) -> None:
            handled_message_ids.append(event.message_id)
            if event.message_id == "message-handler-bad":
                raise RuntimeError("bad lark event")

        class TwoEventConsumer:
            async def events(self):
                yield bad
                yield good

        service._handle_lark_event = handle  # type: ignore[method-assign]

        await service._consume_lark_events(TwoEventConsumer())  # type: ignore[arg-type]

        self.assertEqual(handled_message_ids, ["message-handler-bad", "message-handler-good"])
        self.assertIsNotNone(service._lark_listener_last_event_at)
        failed_events = [event for event in self.db.recent_events(limit=5) if event["event_type"] == "message.handle_failed"]
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0]["data"]["message_id"], "message-handler-bad")

    async def test_case_slots_run_serially_even_with_multiple_workers(self) -> None:
        service = BreakwaterService(self.config(concurrency=2))
        fake = FakeCodexClient(service.db, delay=0.08)
        service.codex_client = fake  # type: ignore[assignment]
        case = service.db.ensure_case(scope_type="jira_issue", scope_key="OPS-77", title="OPS-77")
        slots = [
            service.db.create_slot(source="lark_case_followup", incoming_text=f"follow {i}", case_id=case.case_id, delivery_target="lark_reply")
            for i in range(2)
        ]
        service._start_codex_workers()
        for slot in slots:
            service.enqueue_slot(slot)

        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            if all((service.db.get_slot(slot.slot_id) or slot).codex_status == "completed" for slot in slots):
                break
            await asyncio.sleep(0.02)
        await service.stop()

        self.assertEqual(fake.max_active, 1)
        self.assertEqual(len(fake.calls), 2)

    async def test_running_slot_exposes_codex_session_id_before_turn_completion(self) -> None:
        service = BreakwaterService(self.config(concurrency=1))
        service.codex_client = LiveSessionCodexClient(service.db, delay=0.2)  # type: ignore[assignment]
        service._start_codex_workers()
        slot = service.db.create_slot(source="local", incoming_text="long running task")

        service.enqueue_slot(slot)
        deadline = asyncio.get_running_loop().time() + 1
        refreshed = service.db.get_slot(slot.slot_id)
        while asyncio.get_running_loop().time() < deadline:
            refreshed = service.db.get_slot(slot.slot_id)
            if refreshed and refreshed.codex_thread_id == "thread-live" and refreshed.codex_turn_id == "turn-live":
                break
            await asyncio.sleep(0.01)

        snapshot = service.status_snapshot()
        await service.stop()

        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.codex_status, "running")
        self.assertEqual(refreshed.codex_thread_id, "thread-live")
        self.assertEqual(refreshed.codex_turn_id, "turn-live")
        running_slot = next(slot for slot in snapshot["running_slots"] if slot["slot_id"] == refreshed.slot_id)
        self.assertEqual(running_slot["codex_thread_id"], "thread-live")

    async def test_missing_reply_triggers_limited_retry_until_reply_is_recorded(self) -> None:
        service = BreakwaterService(self.config(concurrency=1, retries=1))
        fake = MissingReplyOnceCodexClient(service.db)
        service.codex_client = fake  # type: ignore[assignment]
        slot = service.db.create_slot(source="local", incoming_text="needs retry")

        await service._run_codex_for_slot(slot)
        refreshed = service.db.get_slot(slot.slot_id)

        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(fake.calls, 2)
        self.assertEqual(fake.resume_thread_ids, [None, "thread-1"])
        self.assertEqual(refreshed.codex_thread_id, "thread-1")
        self.assertEqual(refreshed.codex_attempts, 2)
        self.assertEqual(refreshed.codex_status, "completed")
        self.assertEqual(refreshed.reply_status, "pending")

    def test_lark_event_id_is_deduplicated_to_one_slot(self) -> None:
        first = self.db.create_slot(
            source="lark",
            incoming_text="first",
            lark_event_id="event-1",
            lark_message_id="message-1",
        )
        second = self.db.create_slot(
            source="lark",
            incoming_text="duplicate",
            lark_event_id="event-1",
            lark_message_id="message-2",
        )

        self.assertEqual(first.slot_id, second.slot_id)
        self.assertEqual(len(self.db.list_slots()), 1)

    def test_sent_reply_cannot_be_overwritten_by_late_cli_call(self) -> None:
        slot = self.db.create_slot(source="local", incoming_text="hello")
        self.db.record_reply_request(slot.slot_id, "first")
        self.db.mark_reply_sent(slot.slot_id, None)

        self.db.record_reply_request(slot.slot_id, "late overwrite")
        refreshed = self.db.get_slot(slot.slot_id)

        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.reply_text, "first")
        self.assertEqual(refreshed.reply_status, "sent")

    def test_group_lark_reply_mentions_original_sender(self) -> None:
        service = BreakwaterService(self.config())
        slot = service.db.create_slot(
            source="lark",
            incoming_text="@bot help",
            lark_message_id="message-1",
            chat_type="group",
            sender_id="ou_sender",
        )
        service.db.record_reply_request(slot.slot_id, "answer")
        refreshed = service.db.get_slot(slot.slot_id)
        assert refreshed is not None

        formatted = service._format_lark_reply(refreshed)

        self.assertEqual(formatted, '<at user_id="ou_sender">用户</at> answer')

    def test_github_comment_cli_posts_comment_and_leaves_slot_for_verification(self) -> None:
        self.db.insert_github_issue(
            repo_full_name="apache/doris",
            issue_number=7,
            node_id="I_7",
            title="needs analysis",
            body="body",
            author="alice",
            state="open",
            html_url="https://github.com/apache/doris/issues/7",
            labels=[],
            github_created_at="2026-05-21T09:31:00Z",
            github_updated_at="2026-05-21T09:31:00Z",
            raw={"number": 7},
        )
        slot = self.db.create_github_issue_slot_for_issue("apache/doris", 7)
        assert slot is not None
        fake_client = FakeGitHubCommentClient()

        args = argparse.Namespace(
            db=self.db_path,
            slot_id=slot.slot_id,
            body="analysis\nBreakwater-GitHub-Analysis-Slot: test",
            body_file=None,
            body_words=[],
            github_token="token",
            api_url="https://api.github.com",
        )
        with patch("breakwater.cli.GitHubRestClient", return_value=fake_client):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cmd_github_comment(args)

        self.assertEqual(code, 0)
        self.assertEqual(fake_client.created, [("apache/doris", 7, "analysis\nBreakwater-GitHub-Analysis-Slot: test")])
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.reply_status, "pending")
        self.assertEqual(refreshed.reply_text, "analysis\nBreakwater-GitHub-Analysis-Slot: test")
        self.assertIn("github comment recorded", out.getvalue())

    def test_case_resolver_resolves_supported_explicit_references(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-101", title="OPS-101")
        slot = self.db.create_slot(source="jira_analyze", incoming_text="/analyze", case_id=case.case_id)
        thread_id = "019e4e87-b7dc-7350-9094-e48f123e0a90"
        self.db.mark_codex_turn(slot.slot_id, thread_id, "turn-1")
        self.db.mark_reply_sent(slot.slot_id, "om_reply_1")
        resolver = CaseResolver(self.db)

        cases = [
            resolver.resolve_lark_text("继续看 OPS-101"),
            resolver.resolve_lark_text(f"继续看 {slot.slot_id}"),
            resolver.resolve_lark_text(f"继续看 {case.case_id}"),
            resolver.resolve_lark_text(f"继续看 {thread_id}"),
            resolver.resolve_lark_text("引用回复", lark_message_ids=["om_reply_1"]),
        ]

        self.assertTrue(all(resolution is not None for resolution in cases))
        self.assertEqual({resolution.case.case_id for resolution in cases if resolution}, {case.case_id})

    def test_case_resolver_resolves_github_issue_references(self) -> None:
        case = self.db.ensure_case(scope_type="github_issue", scope_key="apache/doris#42", title="apache/doris#42")
        resolver = CaseResolver(self.db)

        cases = [
            resolver.resolve_lark_text("继续看 apache/doris#42"),
            resolver.resolve_lark_text("继续看 https://github.com/apache/doris/issues/42"),
        ]

        self.assertTrue(all(resolution is not None for resolution in cases))
        self.assertEqual({resolution.case.case_id for resolution in cases if resolution}, {case.case_id})

    def test_case_resolver_rejects_missing_or_ambiguous_references(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-102", title="OPS-102")
        other = self.db.ensure_case(scope_type="jira_issue", scope_key="APP-102", title="APP-102")
        self.assertNotEqual(case.case_id, other.case_id)
        slot = self.db.create_slot(source="jira_analyze", incoming_text="/analyze", case_id=case.case_id)
        resolver = CaseResolver(self.db)

        same_case = resolver.resolve_lark_text(f"继续 OPS-102 {slot.slot_id}")
        missing_only = resolver.resolve_lark_text("继续 APP-999999")
        known_plus_unknown = resolver.resolve_lark_text("继续 OPS-102 APP-999999")
        conflicting = resolver.resolve_lark_text("比较 OPS-102 和 APP-102")

        self.assertIsNotNone(same_case)
        assert same_case is not None
        self.assertEqual(same_case.case.case_id, case.case_id)
        self.assertIsNone(missing_only)
        self.assertIsNone(known_plus_unknown)
        self.assertIsNone(conflicting)

        unknown_result = resolver.inspect_lark_text("继续 APP-999999")
        ambiguous_result = resolver.inspect_lark_text("比较 OPS-102 和 APP-102")
        self.assertEqual(unknown_result.status, "unknown")
        self.assertEqual(ambiguous_result.status, "ambiguous")
        self.assertEqual({candidate.case_id for candidate in ambiguous_result.candidates}, {case.case_id, other.case_id})

    def test_case_resolver_does_not_treat_doris_version_as_jira_key(self) -> None:
        resolver = CaseResolver(self.db)

        result = resolver.inspect_lark_text("我已经把 batch_size 设置为 1 了，版本是 doris-2.1.9")

        self.assertEqual(result.status, "no_reference")
        self.assertEqual(result.references, ())

    def test_case_resolver_uses_chat_name_jira_key_as_first_priority(self) -> None:
        chat_case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-104", title="OPS-104")
        text_case = self.db.ensure_case(scope_type="jira_issue", scope_key="APP-104", title="APP-104")
        self.assertNotEqual(chat_case.case_id, text_case.case_id)
        resolver = CaseResolver(self.db)

        result = resolver.inspect_lark_text("正文里提到了 APP-104", chat_name="OPS-104 现场群")

        self.assertTrue(result.is_resolved)
        self.assertIsNotNone(result.case)
        assert result.case is not None
        self.assertEqual(result.case.case_id, chat_case.case_id)
        self.assertEqual(len(result.references), 1)
        self.assertEqual(result.references[0].alias_type, "jira_issue_key")
        self.assertEqual(result.references[0].alias_key, "OPS-104")

    def test_extract_lark_reference_message_ids_from_nested_raw_event(self) -> None:
        raw = {
            "event": {
                "message": {
                    "parent_id": "om_parent",
                    "root_id": "om_root",
                }
            },
            "data": {"message": {"reply_to_message_id": "om_reply_to"}},
            "parent_id": "om_parent",
        }

        self.assertEqual(
            extract_lark_reference_message_ids(raw),
            ("om_parent", "om_root", "om_reply_to"),
        )

    def test_extract_lark_chat_name_from_common_event_shapes(self) -> None:
        self.assertEqual(extract_lark_chat_name({"chat_name": "OPS-105 现场群"}), "OPS-105 现场群")
        self.assertEqual(
            extract_lark_chat_name({"event": {"message": {"chat": {"name": "APP-105 现场群"}}}}),
            "APP-105 现场群",
        )

    def test_lark_message_mentions_bot_uses_structured_mentions(self) -> None:
        raw = {"event": {"message": {"mentions": [{"id": {"open_id": "cli_bot"}}]}}}
        sdk_raw = {"mentions": [{"id": "cli_bot", "id_type": "app_id", "name": "Renamed Bot"}]}

        self.assertTrue(lark_message_mentions_bot(raw, "hello", "cli_bot"))
        self.assertTrue(lark_message_mentions_bot(sdk_raw, "hello", "cli_bot"))
        self.assertFalse(lark_message_mentions_bot(raw, "hello", "other_bot"))
        self.assertFalse(lark_message_mentions_bot({}, "@bot hello", "cli_bot"))
        self.assertFalse(lark_message_mentions_bot({}, "@Renamed Bot hello", "cli_bot"))

    def test_lark_post_content_is_flattened_from_real_body_structure(self) -> None:
        content = feishu_post_content(image_key="img_v3_0212h_real")
        raw = feishu_post_raw(
            event_id="event-post",
            message_id="om_post",
            chat_id="oc_chat",
            chat_type="p2p",
            sender_id="ou_user",
            content=content,
        )

        normalized = normalize_lark_content("post", "", raw)

        self.assertIn("版本是4.1.0的存算分离模式", normalized.text)
        self.assertIn("主动增量预热任务一直没结束", normalized.text)
        self.assertIn("[Image: img_v3_0212h_real, 1800x150]", normalized.text)
        self.assertIn("周期同步看起来也卡住了", normalized.text)
        self.assertEqual(len(normalized.images), 1)
        self.assertEqual(normalized.images[0].file_key, "img_v3_0212h_real")
        self.assertEqual(normalized.images[0].width, 1800)
        self.assertEqual(normalized.images[0].height, 150)

    def test_lark_post_content_extracts_image_from_lark_cli_placeholder(self) -> None:
        content = "[Image: img_v3_0212h_03d2c494-2c69-4593-94bf-27b6a5634e9g]\n这张图说了啥？"
        raw = {
            "type": "im.message.receive_v1",
            "event_id": "910d887afff0d0474c901ceb6a5ad324",
            "timestamp": "1781098812728",
            "id": "om_x100b6da2dd08bca0c38b3a598ad7a9c",
            "message_id": "om_x100b6da2dd08bca0c38b3a598ad7a9c",
            "create_time": "1781098812427",
            "chat_id": "oc_7d1fe6e9e0d8a92df66418f0f690a451",
            "chat_type": "p2p",
            "message_type": "post",
            "sender_id": "ou_test_user_001",
            "content": content,
        }

        normalized = normalize_lark_content("post", content, raw)

        self.assertEqual(normalized.text, content)
        self.assertEqual(len(normalized.images), 1)
        self.assertEqual(normalized.images[0].file_key, "img_v3_0212h_03d2c494-2c69-4593-94bf-27b6a5634e9g")
        self.assertIsNone(normalized.images[0].width)
        self.assertIsNone(normalized.images[0].height)

    def test_jira_comment_mentions_target_requires_real_mention_markup_or_metadata(self) -> None:
        self.assertTrue(jira_comment_mentions_target({"body": "[~breakwater-bot] please"}, ("breakwater-bot",)))
        self.assertTrue(jira_comment_mentions_target({"body": "[~accountid:acct-123] please"}, ("acct-123",)))
        self.assertTrue(
            jira_comment_mentions_target(
                {"body": {"type": "doc", "content": [{"type": "mention", "attrs": {"id": "acct-123", "text": "@Breakwater"}}]}},
                ("acct-123",),
            )
        )
        self.assertFalse(jira_comment_mentions_target({"body": "@Breakwater please"}, ("Breakwater",)))
        self.assertFalse(jira_comment_mentions_target({"body": "normal comment"}, ("breakwater-bot",)))

    def test_codex_slot_planner_selects_prompt_and_resume_context(self) -> None:
        planner = CodexSlotPlanner()
        local = self.db.create_slot(source="local", incoming_text="hello")
        local_plan = planner.plan(local, None, jira_marker="marker-local")

        case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-103", title="OPS-103")
        first_jira = self.db.create_slot(
            source="jira_analyze",
            incoming_text="/analyze full body",
            case_id=case.case_id,
            jira_issue_key="OPS-103",
            jira_comment_id="comment-first",
        )
        first_plan = planner.plan(first_jira, case, jira_marker="Breakwater-Analysis-Slot: first")
        self.db.mark_codex_turn(first_jira.slot_id, "thread-ops-103", "turn-first")
        refreshed_case = self.db.get_case(case.case_id)
        assert refreshed_case is not None
        second_jira = self.db.create_slot(
            source="jira_analyze",
            incoming_text="/analyze second full body",
            case_id=case.case_id,
            jira_issue_key="OPS-103",
            jira_comment_id="comment-second",
        )
        second_plan = planner.plan(second_jira, refreshed_case, jira_marker="Breakwater-Analysis-Slot: second")
        lark = self.db.create_slot(source="lark_case_followup", incoming_text="follow up", case_id=case.case_id)
        lark_plan = planner.plan(lark, refreshed_case, jira_marker="unused")

        self.assertEqual(local_plan.prompt_name, "lark_initial")
        self.assertIsNone(local_plan.resume_thread_id)
        self.assertEqual(local_plan.prompt_context["case_id"], "")
        self.assertEqual(first_plan.prompt_name, "jira_analyze")
        self.assertIsNone(first_plan.resume_thread_id)
        self.assertEqual(first_plan.prompt_context["jira_comment_body"], "/analyze full body")
        self.assertEqual(second_plan.prompt_name, "jira_analyze_continue")
        self.assertEqual(second_plan.resume_thread_id, "thread-ops-103")
        self.assertEqual(second_plan.prompt_context["jira_comment_id"], "comment-second")
        self.assertEqual(lark_plan.prompt_name, "lark_case_followup")
        self.assertEqual(lark_plan.resume_thread_id, "thread-ops-103")
        self.assertEqual(lark_plan.prompt_context["jira_issue_key"], "OPS-103")
        new_case = self.db.ensure_case(scope_type="jira_issue", scope_key="APP-26000", title="APP-26000")
        lark_jira = self.db.create_slot(
            source="lark_jira_analyze",
            incoming_text="please analyze from lark",
            case_id=new_case.case_id,
            jira_issue_key="APP-26000",
            lark_message_id="message-new-jira",
        )
        lark_jira_plan = planner.plan(lark_jira, new_case, jira_marker="Breakwater-Analysis-Slot: lark")
        self.assertEqual(lark_jira_plan.prompt_name, "lark_jira_analyze")
        self.assertIsNone(lark_jira_plan.resume_thread_id)
        self.assertEqual(lark_jira_plan.prompt_context["jira_issue_key"], "APP-26000")
        self.assertEqual(lark_jira_plan.prompt_context["lark_message_id"], "message-new-jira")
        auto_case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-200", title="OPS-200")
        auto_jira = self.db.create_slot(
            source="jira_issue_auto_analyze",
            incoming_text="",
            case_id=auto_case.case_id,
            jira_issue_key="OPS-200",
        )
        auto_plan = planner.plan(auto_jira, auto_case, jira_marker="Breakwater-Analysis-Slot: auto")
        self.assertEqual(auto_plan.prompt_name, "jira_issue_auto_analyze")
        self.assertEqual(auto_plan.prompt_context["jira_comment_id"], "")
        self.assertEqual(auto_plan.prompt_context["jira_issue_key"], "OPS-200")
        status_case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-300", title="OPS-300")
        status_slot = self.db.create_slot(
            source="jira_status_summary",
            incoming_text="",
            case_id=status_case.case_id,
            jira_issue_key="OPS-300",
            delivery_target="jira_comment",
        )
        status_plan = planner.plan(status_slot, status_case, jira_marker="Breakwater-Analysis-Slot: status")
        self.assertEqual(status_plan.prompt_name, "jira_status_summary")
        self.assertEqual(status_plan.prompt_context["jira_issue_key"], "OPS-300")
        self.assertEqual(status_plan.prompt_context["jira_comment_body"], "")

    def test_db_migrates_legacy_jira_issue_auto_slots_to_explicit_source(self) -> None:
        slot = self.db.create_slot(
            source="jira_analyze",
            incoming_text="Jira issue: OPS-LEGACY",
            trigger_key="jira-issue:OPS-LEGACY",
            jira_issue_key="OPS-LEGACY",
            delivery_target="jira_comment",
        )

        self.db.init()

        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.source, "jira_issue_auto_analyze")

    def test_active_case_registry_enforces_case_level_serialization(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-104", title="OPS-104")
        other = self.db.ensure_case(scope_type="jira_issue", scope_key="APP-104", title="APP-104")
        first = self.db.create_slot(source="lark_case_followup", incoming_text="first", case_id=case.case_id)
        second = self.db.create_slot(source="lark_case_followup", incoming_text="second", case_id=case.case_id)
        different_case = self.db.create_slot(source="lark_case_followup", incoming_text="other", case_id=other.case_id)
        local = self.db.create_slot(source="local", incoming_text="local")
        registry = ActiveCaseRegistry()

        self.assertTrue(registry.claim(first))
        self.assertFalse(registry.claim(second))
        self.assertTrue(registry.claim(different_case))
        self.assertTrue(registry.claim(local))
        registry.release(first)
        self.assertTrue(registry.claim(second))
        registry.release(first)
        registry.release(second)
        registry.release(different_case)
        registry.release(local)
        self.assertEqual(registry.active_case_ids, set())
        self.assertEqual(registry.active_slot_ids, set())

    def test_case_cli_prints_case_timeline_by_alias(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-105", title="OPS-105")
        first = self.db.create_slot(source="jira_analyze", incoming_text="/analyze first", case_id=case.case_id)
        second = self.db.create_slot(source="lark_case_followup", incoming_text="follow up", case_id=case.case_id)
        self.db.mark_codex_turn(first.slot_id, "thread-ops-105", "turn-first")

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = cmd_case(argparse.Namespace(db=self.db_path, identifier="OPS-105", json=True))

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["case"]["case_id"], case.case_id)
        self.assertEqual([slot["slot_id"] for slot in payload["slots"]], [first.slot_id, second.slot_id])
        self.assertIn(
            {"alias_type": "jira_issue_key", "alias_key": "OPS-105"},
            [{key: alias[key] for key in ("alias_type", "alias_key")} for alias in payload["aliases"]],
        )

    def test_web_status_page_and_api_expose_live_snapshot(self) -> None:
        slot = self.db.create_slot(source="local", incoming_text="visible")

        def provider() -> dict[str, object]:
            return {
                "started_at": "2026-05-26T12:34:56+00:00",
                "queue": {"concurrency": 5, "queued": 0, "running": 0, "queued_slot_ids": [], "active_slot_ids": []},
                "status_counts": self.db.count_slots_by_status(),
                "slots": [slot.__dict__ for slot in self.db.list_slots()],
                "jira": {
                    "enabled": False,
                    "projects": ["OPS", "APP"],
                    "poll_interval_seconds": 0,
                    "overlap_seconds": 0,
                    "cursor": self.db.get_state("jira.last_success_at"),
                    "counts": self.db.jira_counts(),
                    "recent_issues": [issue.__dict__ for issue in self.db.recent_jira_issues()],
                    "recent_analyze_comments": [comment.__dict__ for comment in self.db.recent_jira_comments()],
                },
                "events": self.db.recent_events(),
            }

        self.assertIn("/api/status", render_status_page())
        server = BreakwaterWebServer("127.0.0.1", 0, provider)
        server.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/api/status", timeout=2) as response:
                body = response.read().decode("utf-8")
        finally:
            server.stop()

        self.assertIn("visible", body)
        self.assertIn('"started_at": "2026-05-26T12:34:56+00:00"', body)
        self.assertIn('"queued": 0', body)
        self.assertIn("startup-banner", render_status_page())
        self.assertIn("Service started", render_status_page())
        self.assertIn("timestamp(data.started_at)", render_status_page())
        self.assertIn(".slice(0, 20)", render_status_page())
        self.assertIn("Active Tasks", render_status_page())
        self.assertIn("Open Jira Analysis", render_status_page())
        self.assertIn("Lark Messages", render_status_page())
        self.assertIn("Lark Listener", render_status_page())
        self.assertIn("Codex App Server", render_status_page())
        self.assertIn("const sessionText = (slot) => slot.codex_thread_id || 'session pending';", render_status_page())
        self.assertIn("const detailsState = new Map();", render_status_page())
        self.assertIn("const scrollState = new Map();", render_status_page())
        self.assertIn("rememberUiState();", render_status_page())
        self.assertIn("restoreUiState();", render_status_page())
        self.assertIn('data-state-key="thread:${esc(thread.id)}:prompt"', render_status_page())
        self.assertIn('data-preserve-scroll data-state-key="thread:${esc(thread.id)}:prompt-scroll"', render_status_page())
        self.assertIn("case-timeline", render_status_page())
        self.assertIn('data-preserve-scroll data-state-key="cases"', render_status_page())

    def test_recent_codex_threads_filters_to_breakwater_service_name(self) -> None:
        codex_home = Path(self.tempdir.name) / "codex-home"
        codex_home.mkdir()
        state = codex_home / "state_5.sqlite"
        workspace = str(Path(self.tempdir.name) / "workspace")
        with sqlite3.connect(state) as conn:
            conn.execute(
                """
                CREATE TABLE threads (
                  id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, reasoning_effort TEXT,
                  cwd TEXT NOT NULL, approval_mode TEXT NOT NULL, sandbox_policy TEXT NOT NULL,
                  title TEXT NOT NULL, preview TEXT NOT NULL, first_user_message TEXT NOT NULL,
                  thread_source TEXT, tokens_used INTEGER NOT NULL, created_at_ms INTEGER,
                  updated_at_ms INTEGER, archived INTEGER NOT NULL, service_name TEXT
                )
                """
            )
            rows = [
                ("bw", "vscode", "gpt-5.5", "xhigh", workspace, "never", "danger-full-access", "Breakwater", "preview", "plain app-server prompt", None, 1, 1, 3, 0, "breakwater"),
                ("old-prompt", "vscode", "gpt-5.5", "xhigh", workspace, "never", "danger-full-access", "Breakwater", "preview", "你是 Breakwater on-call agent\nslot_id: slot_old", None, 1, 1, 2, 0, None),
                ("other-service", "vscode", "gpt-5.5", "xhigh", workspace, "never", "danger-full-access", "Local", "preview", "local codex prompt", None, 1, 1, 1, 0, "other"),
            ]
            conn.executemany(
                """
                INSERT INTO threads(id, source, model, reasoning_effort, cwd, approval_mode, sandbox_policy,
                  title, preview, first_user_message, thread_source, tokens_used, created_at_ms, updated_at_ms, archived, service_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            threads = recent_codex_threads(limit=10, cwd=workspace, breakwater_only=True)

        self.assertEqual([thread["id"] for thread in threads], ["bw"])
        self.assertEqual(threads[0]["first_user_message"], "plain app-server prompt")

    def test_recent_codex_threads_can_use_slot_thread_ids_when_service_name_is_absent(self) -> None:
        codex_home = Path(self.tempdir.name) / "codex-home-no-service"
        codex_home.mkdir()
        state = codex_home / "state_5.sqlite"
        workspace = str(Path(self.tempdir.name) / "workspace")
        with sqlite3.connect(state) as conn:
            conn.execute(
                """
                CREATE TABLE threads (
                  id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, reasoning_effort TEXT,
                  cwd TEXT NOT NULL, approval_mode TEXT NOT NULL, sandbox_policy TEXT NOT NULL,
                  title TEXT NOT NULL, preview TEXT NOT NULL, first_user_message TEXT NOT NULL,
                  thread_source TEXT, tokens_used INTEGER NOT NULL, created_at_ms INTEGER,
                  updated_at_ms INTEGER, archived INTEGER NOT NULL
                )
                """
            )
            rows = [
                ("local", "cli", "gpt-5.5", "xhigh", workspace, "never", "danger-full-access", "Local", "local preview", "local codex prompt", "user", 1, 1, 5, 0),
                ("thread-active", "vscode", "gpt-5.5", "xhigh", workspace, "never", "danger-full-access", "Breakwater", "active preview", "current breakwater task", None, 1, 1, 4, 0),
            ]
            conn.executemany(
                """
                INSERT INTO threads(id, source, model, reasoning_effort, cwd, approval_mode, sandbox_policy,
                  title, preview, first_user_message, thread_source, tokens_used, created_at_ms, updated_at_ms, archived)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            threads = recent_codex_threads(
                limit=10,
                cwd=workspace,
                breakwater_only=True,
                thread_ids=["thread-active"],
            )

        self.assertEqual([thread["id"] for thread in threads], ["thread-active"])

    def test_unique_log_file_rotates_every_start(self) -> None:
        base = Path(self.tempdir.name) / "breakwater.log"
        first = unique_log_file(base)
        second = unique_log_file(base)

        self.assertNotEqual(first, base)
        self.assertTrue(first.name.startswith("breakwater-"))
        self.assertEqual(first.suffix, ".log")
        self.assertEqual(first.parent, base.parent)
        self.assertTrue(second.name.startswith("breakwater-"))
        self.assertNotEqual(first, second)

    async def test_status_snapshot_exposes_active_jira_analyze_and_lark_views(self) -> None:
        codex_home = Path(self.tempdir.name) / "codex-home-status"
        codex_home.mkdir()
        workspace = Path(self.tempdir.name) / "workspace"
        with sqlite3.connect(codex_home / "state_5.sqlite") as conn:
            conn.execute(
                """
                CREATE TABLE threads (
                  id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, reasoning_effort TEXT,
                  cwd TEXT NOT NULL, approval_mode TEXT NOT NULL, sandbox_policy TEXT NOT NULL,
                  title TEXT NOT NULL, preview TEXT NOT NULL, first_user_message TEXT NOT NULL,
                  thread_source TEXT, tokens_used INTEGER NOT NULL, created_at_ms INTEGER,
                  updated_at_ms INTEGER, archived INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO threads(id, source, model, reasoning_effort, cwd, approval_mode, sandbox_policy,
                  title, preview, first_user_message, thread_source, tokens_used, created_at_ms, updated_at_ms, archived)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "thread-status",
                    "vscode",
                    "gpt-5.5",
                    "xhigh",
                    str(workspace),
                    "never",
                    "danger-full-access",
                    "Breakwater",
                    "active preview",
                    "active task prompt",
                    None,
                    1,
                    1,
                    9,
                    0,
                ),
            )

        service = BreakwaterService(
            AppConfig(
                db_path=self.db_path,
                codex=CodexConfig(start_server=False),
                codex_workspace=workspace,
                codex_concurrency=1,
                web_enabled=False,
            )
        )
        running = self.db.create_slot(source="local", incoming_text="running task")
        self.db.mark_codex_started(running.slot_id, 1, "thread-status", "turn-status")
        self.assertTrue(service.active_runs.claim(running))
        lark = self.db.create_slot(
            source="lark",
            incoming_text="lark question",
            lark_event_id="event-status",
            lark_message_id="message-status",
            chat_type="group",
            sender_id="ou_user",
        )
        self.db.insert_jira_comment(
            comment_id="comment-status",
            issue_key="APP-99",
            project_key="APP",
            author="Dana",
            body="/analyze status page",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"id": "comment-status"},
        )
        jira_slot = self.db.create_jira_analyze_slot_for_comment("comment-status")
        assert jira_slot is not None

        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            snapshot = service.status_snapshot()

        self.assertIn("started_at", snapshot)
        self.assertEqual([slot["slot_id"] for slot in snapshot["running_slots"]], [running.slot_id])
        self.assertFalse(snapshot["lark_listener"]["running"])
        self.assertFalse(snapshot["lark_listener"]["consumer_running"])
        self.assertEqual(snapshot["lark_listener"]["restart_count"], 0)
        self.assertIsNone(snapshot["lark_listener"]["last_error"])
        self.assertEqual([thread["id"] for thread in snapshot["codex"]["recent_threads"]], ["thread-status"])
        self.assertIn(jira_slot.slot_id, {slot["slot_id"] for slot in snapshot["open_jira_analyze_slots"]})
        self.assertIn(lark.slot_id, {slot["slot_id"] for slot in snapshot["lark_messages"]})
        self.assertTrue(snapshot["case_timelines"])
        self.assertIn("aliases", snapshot["case_timelines"][0])
        self.assertIn("slots", snapshot["case_timelines"][0])
        comments = snapshot["jira"]["recent_analyze_comments"]
        self.assertEqual(comments[0]["status"], "queued")

    def test_status_snapshot_limits_recent_analyze_comments_to_twenty(self) -> None:
        service = BreakwaterService(self.config())
        for index in range(25):
            self.db.insert_jira_comment(
                comment_id=f"comment-{index}",
                issue_key=f"APP-{index}",
                project_key="APP",
                author="Dana",
                body=f"/analyze status page {index}",
                jira_created_at="2026-05-21T17:31:00.000+0800",
                jira_updated_at="2026-05-21T17:31:00.000+0800",
                raw={"id": f"comment-{index}"},
            )

        snapshot = service.status_snapshot()

        self.assertEqual(len(snapshot["jira"]["recent_analyze_comments"]), 20)

    def test_case_timelines_are_batched_and_bound_per_case(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-94", title="OPS-94")
        slots = [
            self.db.create_slot(source="lark_case_followup", incoming_text=f"turn {index}", case_id=case.case_id)
            for index in range(5)
        ]

        timelines = self.db.case_timelines(limit=10, slots_per_case=3)

        timeline = next(item for item in timelines if item["case"]["case_id"] == case.case_id)
        self.assertEqual(timeline["slot_count"], 5)
        self.assertEqual(len(timeline["slots"]), 3)
        self.assertTrue({slot["slot_id"] for slot in timeline["slots"]}.issubset({slot.slot_id for slot in slots}))

    def test_jira_poll_bootstraps_cursor_without_backfilling_history(self) -> None:
        fake = FakeJiraClient()
        monitor = JiraMonitor(
            self.db,
            JiraConfig(projects=("OPS", "APP"), overlap_seconds=60, record_new_issues=True),
            Path(self.tempdir.name),
            client=fake,
        )

        result = monitor.poll_once(now=datetime(2026, 5, 21, 9, 30, tzinfo=timezone.utc))

        self.assertTrue(result.bootstrapped)
        self.assertEqual(fake.search_calls, [])
        self.assertEqual(self.db.jira_counts(), {"issues": 0, "analyzed_issues": 0, "analyze_comments": 0})
        self.assertEqual(self.db.get_state("jira.last_success_at"), "2026-05-21T09:30:00+00:00")

    def test_jira_poll_records_new_issues_and_analyze_comments_once(self) -> None:
        fake = FakeJiraClient()
        self.db.set_state("jira.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = JiraMonitor(
            self.db,
            JiraConfig(projects=("OPS", "APP"), overlap_seconds=60, record_new_issues=True),
            Path(self.tempdir.name),
            client=fake,
        )

        first = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))
        second = monitor.poll_once(now=datetime(2026, 5, 21, 9, 33, tzinfo=timezone.utc))

        self.assertEqual(first.new_issues, 1)
        self.assertEqual(first.analyze_comments, 2)
        self.assertEqual(first.analyze_slots, 2)
        self.assertEqual(second.new_issues, 0)
        self.assertEqual(second.analyze_comments, 0)
        self.assertEqual(second.analyze_slots, 0)
        self.assertEqual(self.db.jira_counts(), {"issues": 1, "analyzed_issues": 0, "analyze_comments": 2})
        comments = self.db.recent_jira_comments(limit=10)
        self.assertEqual({comment.comment_id for comment in comments}, {"c1", "c2"})
        self.assertTrue(all(comment.slot_id for comment in comments))
        jira_slots = [slot for slot in self.db.list_slots(limit=10) if slot.source == "jira_analyze"]
        self.assertEqual(len(jira_slots), 2)
        self.assertEqual({slot.jira_comment_id for slot in jira_slots}, {"c1", "c2"})
        self.assertEqual(len({slot.case_id for slot in jira_slots}), 1)
        case = self.db.find_case_by_alias("jira_issue_key", "APP-1")
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(case.scope_key, "APP-1")

    def test_jira_poll_can_disable_new_issue_recording_while_tracking_analyze_comments(self) -> None:
        fake = FakeJiraClient()
        self.db.set_state("jira.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = JiraMonitor(
            self.db,
            JiraConfig(projects=("OPS", "APP"), overlap_seconds=60, record_new_issues=False),
            Path(self.tempdir.name),
            client=fake,
        )

        result = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))

        self.assertEqual(result.new_issues, 0)
        self.assertEqual(result.analyze_comments, 2)
        self.assertEqual(self.db.jira_counts(), {"issues": 0, "analyzed_issues": 0, "analyze_comments": 2})
        self.assertFalse(any("created >=" in call for call in fake.search_calls))

    def test_jira_poll_records_real_bot_mentions_without_matching_plain_at_text(self) -> None:
        fake = FakeJiraClient()
        self.db.set_state("jira.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = JiraMonitor(
            self.db,
            JiraConfig(
                projects=("OPS", "APP"),
                overlap_seconds=60,
                record_new_issues=False,
                bot_mention_keys=("breakwater-bot",),
            ),
            Path(self.tempdir.name),
            client=fake,
        )

        result = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))

        self.assertEqual(result.analyze_comments, 3)
        self.assertEqual(self.db.jira_counts(), {"issues": 0, "analyzed_issues": 0, "analyze_comments": 3})
        comments = self.db.recent_jira_comments(limit=10)
        self.assertEqual({comment.comment_id for comment in comments}, {"c1", "c2", "c5"})
        jira_slots = [slot for slot in self.db.list_slots(limit=10) if slot.source == "jira_analyze"]
        self.assertEqual({slot.jira_comment_id for slot in jira_slots}, {"c1", "c2", "c5"})
        c5 = next(slot for slot in jira_slots if slot.jira_comment_id == "c5")
        self.assertIn("[~breakwater-bot]", c5.incoming_text)
        self.assertTrue(
            any(
                event["event_type"] == "comment.analyze" and event["data"].get("trigger") == "bot_mention"
                for event in self.db.recent_events(limit=20)
            )
        )

    def test_jira_poll_does_not_retrigger_breakwater_analysis_output_that_mentions_bot(self) -> None:
        fake = FakeJiraBreakwaterAnalysisOutputClient()
        self.db.set_state("jira.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = JiraMonitor(
            self.db,
            JiraConfig(
                projects=("OPS", "APP"),
                overlap_seconds=60,
                record_new_issues=False,
                bot_mention_keys=("breakwater-bot",),
            ),
            Path(self.tempdir.name),
            client=fake,
        )

        result = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))

        self.assertEqual(result.analyze_comments, 0)
        self.assertEqual(result.analyze_slots, 0)
        self.assertEqual(self.db.jira_counts(), {"issues": 0, "analyzed_issues": 0, "analyze_comments": 0})
        self.assertFalse([slot for slot in self.db.list_slots(limit=10) if slot.source == "jira_analyze"])
        self.assertFalse(
            [
                event for event in self.db.recent_events(limit=20)
                if event["event_type"] in {"comment.analyze", "analyze.slot_created"}
            ]
        )

    def test_jira_poll_auto_analyzes_new_issues_with_project_filter(self) -> None:
        fake = FakeJiraMultiProjectClient()
        self.db.set_state("jira.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = JiraMonitor(
            self.db,
            JiraConfig(
                projects=("OPS", "APP"),
                overlap_seconds=60,
                record_new_issues=False,
                auto_analyze_new_issues=True,
                auto_analyze_projects=("OPS",),
            ),
            Path(self.tempdir.name),
            client=fake,
        )

        first = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))
        second = monitor.poll_once(now=datetime(2026, 5, 21, 9, 33, tzinfo=timezone.utc))

        self.assertEqual(first.new_issues, 1)
        self.assertEqual(first.analyze_comments, 0)
        self.assertEqual(first.issue_analyze_slots, 1)
        self.assertEqual(first.analyze_slots, 1)
        self.assertEqual(second.new_issues, 0)
        self.assertEqual(second.issue_analyze_slots, 0)
        self.assertEqual(self.db.jira_counts(), {"issues": 1, "analyzed_issues": 1, "analyze_comments": 0})
        self.assertIn('project in ("OPS")', fake.search_calls[0])
        self.assertNotIn('"APP"', fake.search_calls[0])
        slots = [slot for slot in self.db.list_slots(limit=10) if slot.source == "jira_issue_auto_analyze"]
        self.assertEqual(len(slots), 1)
        slot = slots[0]
        self.assertEqual(slot.jira_issue_key, "OPS-1")
        self.assertIsNone(slot.jira_comment_id)
        self.assertEqual(slot.trigger_key, "jira-issue:OPS-1")
        self.assertEqual(slot.incoming_text, "")
        issue_rows = {issue.issue_key: issue for issue in self.db.recent_jira_issues(limit=10)}
        self.assertEqual(issue_rows["OPS-1"].slot_id, slot.slot_id)
        self.assertEqual(issue_rows["OPS-1"].status, "queued")
        self.assertNotIn("APP-1", issue_rows)
        case = self.db.find_case_by_alias("jira_issue_key", "OPS-1")
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(case.scope_key, "OPS-1")
        event_types = [event["event_type"] for event in self.db.recent_events(limit=10)]
        self.assertIn("issue.created", event_types)
        self.assertIn("issue_analyze.slot_created", event_types)

    def test_jira_poll_skips_auto_analyze_when_issue_contains_no_analyze_keyword(self) -> None:
        fake = FakeJiraNoAnalyzeClient()
        self.db.set_state("jira.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = JiraMonitor(
            self.db,
            JiraConfig(
                projects=("OPS", "APP"),
                overlap_seconds=60,
                record_new_issues=False,
                auto_analyze_new_issues=True,
                auto_analyze_projects=("OPS",),
            ),
            Path(self.tempdir.name),
            client=fake,
        )

        result = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))

        self.assertIn("description", fake.field_calls[0])
        self.assertEqual(result.new_issues, 1)
        self.assertEqual(result.issue_analyze_slots, 0)
        self.assertEqual(result.analyze_slots, 0)
        self.assertEqual(self.db.jira_counts(), {"issues": 1, "analyzed_issues": 0, "analyze_comments": 0})
        self.assertFalse([
            slot for slot in self.db.list_slots(limit=10)
            if slot.source == "jira_issue_auto_analyze"
        ])
        issue_rows = {issue.issue_key: issue for issue in self.db.recent_jira_issues(limit=10)}
        self.assertIn("OPS-2", issue_rows)
        self.assertIsNone(issue_rows["OPS-2"].slot_id)
        skip_events = [
            event for event in self.db.recent_events(limit=10)
            if event["event_type"] == "issue_analyze.skipped"
        ]
        self.assertEqual(len(skip_events), 1)
        self.assertEqual(
            skip_events[0]["data"],
            {
                "issue_key": "OPS-2",
                "project_key": "OPS",
                "reason": "skip_keyword",
                "keyword": "/no-analyze",
            },
        )

    def test_jira_poll_record_new_issues_still_records_non_auto_projects(self) -> None:
        fake = FakeJiraMultiProjectClient()
        self.db.set_state("jira.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = JiraMonitor(
            self.db,
            JiraConfig(
                projects=("OPS", "APP"),
                overlap_seconds=60,
                record_new_issues=True,
                auto_analyze_new_issues=True,
                auto_analyze_projects=("OPS",),
            ),
            Path(self.tempdir.name),
            client=fake,
        )

        result = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))

        self.assertEqual(result.new_issues, 2)
        self.assertEqual(result.issue_analyze_slots, 1)
        self.assertEqual(self.db.jira_counts(), {"issues": 2, "analyzed_issues": 1, "analyze_comments": 0})
        self.assertIn('"APP"', fake.search_calls[0])
        issue_rows = {issue.issue_key: issue for issue in self.db.recent_jira_issues(limit=10)}
        self.assertIsNone(issue_rows["APP-1"].slot_id)

    def test_jira_poll_auto_analyze_projects_cannot_escape_monitored_boundary(self) -> None:
        fake = FakeJiraMultiProjectClient()
        self.db.set_state("jira.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = JiraMonitor(
            self.db,
            JiraConfig(
                projects=("OPS",),
                overlap_seconds=60,
                record_new_issues=False,
                auto_analyze_new_issues=True,
                auto_analyze_projects=("APP",),
            ),
            Path(self.tempdir.name),
            client=fake,
        )

        result = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))

        self.assertEqual(result.new_issues, 0)
        self.assertEqual(result.issue_analyze_slots, 0)
        self.assertEqual(self.db.jira_counts(), {"issues": 0, "analyzed_issues": 0, "analyze_comments": 0})
        self.assertFalse(any("created >=" in call for call in fake.search_calls))
        event_types = [event["event_type"] for event in self.db.recent_events(limit=10)]
        self.assertIn("issue.poll_skipped", event_types)

    def test_jira_poll_auto_analyze_defaults_to_all_monitored_projects(self) -> None:
        fake = FakeJiraMultiProjectClient()
        self.db.set_state("jira.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = JiraMonitor(
            self.db,
            JiraConfig(
                projects=("OPS", "APP"),
                overlap_seconds=60,
                auto_analyze_new_issues=True,
            ),
            Path(self.tempdir.name),
            client=fake,
        )

        result = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))

        self.assertEqual(result.new_issues, 2)
        self.assertEqual(result.issue_analyze_slots, 2)
        slots = [slot for slot in self.db.list_slots(limit=10) if slot.source == "jira_issue_auto_analyze"]
        self.assertEqual({slot.jira_issue_key for slot in slots}, {"OPS-1", "APP-1"})
        self.assertTrue(all(slot.jira_comment_id is None for slot in slots))

    def test_jira_status_summary_triggers_only_on_observed_transition_to_configured_status(self) -> None:
        fake = FakeJiraStatusTransitionClient(["In Progress", "Done", "Done"])
        self.db.set_state("jira.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = JiraMonitor(
            self.db,
            JiraConfig(
                projects=("OPS", "APP"),
                overlap_seconds=60,
                status_summary_enabled=True,
                status_summary_projects=("OPS",),
                status_summary_target_statuses=("Done", "Backlog"),
            ),
            Path(self.tempdir.name),
            client=fake,
        )

        baseline = monitor.poll_once(now=datetime(2026, 5, 21, 9, 31, tzinfo=timezone.utc))
        transitioned = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))
        duplicate = monitor.poll_once(now=datetime(2026, 5, 21, 9, 33, tzinfo=timezone.utc))

        self.assertEqual(baseline.status_changed_issues, 0)
        self.assertEqual(baseline.status_summary_slots, 0)
        self.assertEqual(transitioned.status_changed_issues, 1)
        self.assertEqual(transitioned.status_summary_slots, 1)
        self.assertEqual(transitioned.analyze_slots, 1)
        self.assertEqual(duplicate.status_changed_issues, 0)
        self.assertEqual(duplicate.status_summary_slots, 0)
        status_jqls = [jql for jql, fields in zip(fake.search_calls, fake.field_calls) if "status" in fields]
        self.assertTrue(status_jqls)
        self.assertTrue(all('project in ("OPS")' in jql for jql in status_jqls))
        slots = [slot for slot in self.db.list_slots(limit=10) if slot.source == "jira_status_summary"]
        self.assertEqual(len(slots), 1)
        slot = slots[0]
        self.assertEqual(slot.jira_issue_key, "OPS-300")
        self.assertIsNone(slot.jira_comment_id)
        self.assertTrue(str(slot.trigger_key).startswith("jira-status:OPS-300:done:"))
        self.assertEqual(slot.incoming_text, "")
        with self.db.connect() as conn:
            raw = json.loads(str(conn.execute("SELECT raw_json FROM slots WHERE slot_id=?", (slot.slot_id,)).fetchone()["raw_json"]))
        transition = raw["breakwater_status_transition"]
        self.assertEqual(transition["previous_status_name"], "In Progress")
        self.assertEqual(transition["status_name"], "Done")
        issue = self.db.recent_jira_issues(limit=1)[0]
        self.assertEqual(issue.jira_status_name, "Done")
        events = self.db.recent_events(limit=20)
        created = [event for event in events if event["event_type"] == "status_summary.slot_created"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["data"]["previous_status"], "In Progress")
        self.assertEqual(created[0]["data"]["status"], "Done")

    def test_jira_status_summary_does_not_backfill_first_observed_target_status(self) -> None:
        fake = FakeJiraStatusTransitionClient(["Backlog"])
        self.db.set_state("jira.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = JiraMonitor(
            self.db,
            JiraConfig(
                projects=("OPS", "APP"),
                overlap_seconds=60,
                status_summary_enabled=True,
                status_summary_projects=("OPS",),
                status_summary_target_statuses=("Done", "Backlog"),
            ),
            Path(self.tempdir.name),
            client=fake,
        )

        result = monitor.poll_once(now=datetime(2026, 5, 21, 9, 31, tzinfo=timezone.utc))

        self.assertEqual(result.status_changed_issues, 0)
        self.assertEqual(result.status_summary_slots, 0)
        self.assertFalse([slot for slot in self.db.list_slots(limit=10) if slot.source == "jira_status_summary"])
        issue = self.db.recent_jira_issues(limit=1)[0]
        self.assertEqual(issue.jira_status_name, "Backlog")

    def test_jira_status_summary_respects_no_analyze_keyword(self) -> None:
        fake = FakeJiraStatusTransitionClient(["In Progress", "Done"], description="please /no-analyze this closure")
        self.db.set_state("jira.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = JiraMonitor(
            self.db,
            JiraConfig(
                projects=("OPS",),
                overlap_seconds=60,
                status_summary_enabled=True,
                status_summary_projects=("OPS",),
                status_summary_target_statuses=("Done",),
            ),
            Path(self.tempdir.name),
            client=fake,
        )

        monitor.poll_once(now=datetime(2026, 5, 21, 9, 31, tzinfo=timezone.utc))
        result = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))

        self.assertEqual(result.status_changed_issues, 1)
        self.assertEqual(result.status_summary_slots, 0)
        self.assertFalse([slot for slot in self.db.list_slots(limit=10) if slot.source == "jira_status_summary"])
        skip_events = [event for event in self.db.recent_events(limit=20) if event["event_type"] == "status_summary.skipped"]
        self.assertEqual(len(skip_events), 1)
        self.assertEqual(skip_events[0]["data"]["keyword"], "/no-analyze")

    def test_github_poll_bootstraps_cursor_without_backfilling_history(self) -> None:
        fake = FakeGitHubClient()
        monitor = GitHubMonitor(
            self.db,
            GitHubConfig(repositories=("Apache/Doris",), overlap_seconds=60, auto_analyze=True),
            client=fake,
        )

        result = monitor.poll_once(now=datetime(2026, 5, 21, 9, 30, tzinfo=timezone.utc))

        self.assertTrue(result.bootstrapped)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.db.github_counts(), {"issues": 0, "analyzed_issues": 0})
        self.assertEqual(self.db.get_state("github:apache/doris.last_success_at"), "2026-05-21T09:30:00+00:00")
        self.assertEqual(self.db.get_state("github:apache/doris.bootstrap_at"), "2026-05-21T09:30:00+00:00")

    def test_github_poll_does_not_backfill_overlap_before_bootstrap_on_second_poll(self) -> None:
        fake = FakeGitHubBootstrapOverlapClient()
        monitor = GitHubMonitor(
            self.db,
            GitHubConfig(repositories=("apache/doris",), overlap_seconds=180, auto_analyze=True),
            client=fake,
        )

        bootstrap = monitor.poll_once(now=datetime(2026, 5, 21, 9, 30, tzinfo=timezone.utc))
        second = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))

        self.assertTrue(bootstrap.bootstrapped)
        self.assertEqual(second.new_issues, 0)
        self.assertEqual(second.analyze_slots, 0)
        self.assertEqual(self.db.github_counts(), {"issues": 0, "analyzed_issues": 0})

    def test_github_poll_records_new_issues_and_analyze_slots_once(self) -> None:
        fake = FakeGitHubClient()
        self.db.set_state("github:apache/doris.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = GitHubMonitor(
            self.db,
            GitHubConfig(repositories=("Apache/Doris",), overlap_seconds=60, auto_analyze=True),
            client=fake,
        )

        first = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))
        second = monitor.poll_once(now=datetime(2026, 5, 21, 9, 33, tzinfo=timezone.utc))

        self.assertEqual(first.new_issues, 1)
        self.assertEqual(first.analyze_slots, 1)
        self.assertEqual(second.new_issues, 0)
        self.assertEqual(second.analyze_slots, 0)
        self.assertEqual(self.db.github_counts(), {"issues": 1, "analyzed_issues": 1})
        issues = self.db.recent_github_issues(limit=10)
        self.assertEqual([(issue.repo_full_name, issue.issue_number) for issue in issues], [("apache/doris", 10)])
        github_slots = [slot for slot in self.db.list_slots(limit=10) if slot.source == "github_issue_analyze"]
        self.assertEqual(len(github_slots), 1)
        self.assertEqual(github_slots[0].github_repo, "apache/doris")
        self.assertEqual(github_slots[0].github_issue_number, 10)
        case = self.db.find_case_by_alias("github_issue", "apache/doris#10")
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(case.scope_key, "apache/doris#10")

    def test_github_poll_only_tracks_bug_prefixed_issues(self) -> None:
        fake = FakeGitHubMixedTitleClient()
        self.db.set_state("github:apache/doris.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = GitHubMonitor(
            self.db,
            GitHubConfig(repositories=("apache/doris",), overlap_seconds=60, auto_analyze=True),
            client=fake,
        )

        result = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))

        self.assertEqual(result.new_issues, 2)
        self.assertEqual(result.analyze_slots, 2)
        self.assertEqual(self.db.github_counts(), {"issues": 2, "analyzed_issues": 2})
        issues = self.db.recent_github_issues(limit=10)
        self.assertEqual(sorted(issue.issue_number for issue in issues), [20, 21])
        self.assertEqual(sorted(issue.title for issue in issues), ["[BUG] uppercase bug title", "[Bug] mixed case bug title"])
        github_slots = [slot for slot in self.db.list_slots(limit=10) if slot.source == "github_issue_analyze"]
        self.assertEqual(sorted(slot.github_issue_number for slot in github_slots), [20, 21])
        self.assertIsNone(self.db.find_case_by_alias("github_issue", "apache/doris#22"))
        self.assertIsNone(self.db.find_case_by_alias("github_issue", "apache/doris#23"))
        self.assertIsNone(self.db.find_case_by_alias("github_issue", "apache/doris#24"))
        self.assertIsNone(self.db.find_case_by_alias("github_issue", "apache/doris#25"))

    def test_github_poll_can_record_without_auto_analyze(self) -> None:
        fake = FakeGitHubClient()
        self.db.set_state("github:apache/doris.last_success_at", "2026-05-21T09:30:00+00:00")
        monitor = GitHubMonitor(
            self.db,
            GitHubConfig(repositories=("apache/doris",), overlap_seconds=60, auto_analyze=False),
            client=fake,
        )

        result = monitor.poll_once(now=datetime(2026, 5, 21, 9, 32, tzinfo=timezone.utc))

        self.assertEqual(result.new_issues, 1)
        self.assertEqual(result.analyze_slots, 0)
        self.assertEqual(self.db.github_counts(), {"issues": 1, "analyzed_issues": 0})
        self.assertFalse(any(slot.source == "github_issue_analyze" for slot in self.db.list_slots(limit=10)))

    def test_github_adapter_clamps_page_size_to_github_limit(self) -> None:
        self.assertEqual(GitHubRestClient(token="token", page_size=200).page_size, 100)
        self.assertEqual(GitHubRestClient(token="token", page_size=0).page_size, 1)

    def test_github_adapter_can_use_stored_gh_auth_token_without_plaintext_config(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("breakwater.github_adapter.subprocess.run") as run:
                run.return_value.stdout = "gh-token\n"

                self.assertEqual(gh_auth_token(), "gh-token")
                self.assertEqual(GitHubRestClient().token, "gh-token")

    async def test_jira_analyze_slot_is_sent_to_codex_with_jira_prompt_context(self) -> None:
        self.db.insert_jira_comment(
            comment_id="comment-1",
            issue_key="APP-42",
            project_key="APP",
            author="Alice",
            body="/analyze memory spike",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"id": "comment-1"},
        )
        slot = self.db.create_jira_analyze_slot_for_comment("comment-1")
        assert slot is not None
        service = BreakwaterService(self.config(concurrency=1))
        marker = service._jira_analysis_marker(slot.slot_id)
        fake = NoReplyCodexClient()
        fake_jira = FakeJiraVerifier([[{"id": "reply-1", "body": f"analysis\n{marker}"}]])
        service.codex_client = fake  # type: ignore[assignment]
        service.jira_client = fake_jira  # type: ignore[assignment]

        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.prompt_names, ["jira_analyze"])
        self.assertIsNotNone(fake.prompt_contexts[0])
        assert fake.prompt_contexts[0] is not None
        self.assertEqual(fake.prompt_contexts[0]["jira_issue_key"], "APP-42")
        self.assertEqual(fake.prompt_contexts[0]["jira_comment_id"], "comment-1")
        self.assertEqual(fake.prompt_contexts[0]["jira_marker"], marker)
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.reply_status, "sent")
        event_types = [event["event_type"] for event in self.db.recent_events(limit=10)]
        self.assertIn("analysis.comment_verified", event_types)

    async def test_jira_issue_auto_analyze_slot_uses_existing_jira_delivery_contract(self) -> None:
        inserted = self.db.insert_jira_issue(
            issue_key="OPS-200",
            project_key="OPS",
            summary="new production incident",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"key": "OPS-200"},
            reporter="reporter-a",
        )
        self.assertTrue(inserted)
        slot = self.db.create_jira_issue_analyze_slot_for_issue("OPS-200")
        assert slot is not None
        self.assertEqual(slot.source, "jira_issue_auto_analyze")
        self.assertEqual(slot.incoming_text, "")
        service = BreakwaterService(self.config(concurrency=1))
        marker = service._jira_analysis_marker(slot.slot_id)
        fake = AutomationReportCodexClient(
            self.db,
            {
                "schema_version": 1,
                "kind": "jira_issue_auto_analyze",
                "missing_required_material": True,
                "missing_materials": ["profile"],
                "reason": "缺少 profile，无法确认根因",
                "confidence": "high",
            },
        )
        fake_jira = FakeJiraVerifier([[{"id": "reply-auto-issue", "body": f"auto analysis\n{marker}"}]])
        service.codex_client = fake  # type: ignore[assignment]
        service.jira_client = fake_jira  # type: ignore[assignment]

        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.prompt_names, ["jira_issue_auto_analyze"])
        self.assertIsNotNone(fake.prompt_contexts[0])
        assert fake.prompt_contexts[0] is not None
        self.assertEqual(fake.prompt_contexts[0]["jira_issue_key"], "OPS-200")
        self.assertEqual(fake.prompt_contexts[0]["jira_comment_id"], "")
        self.assertEqual(fake.prompt_contexts[0]["jira_marker"], marker)
        self.assertEqual(fake_jira.calls, ["OPS-200"])
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "replied")
        self.assertEqual(refreshed.codex_status, "completed")
        self.assertEqual(refreshed.reply_status, "sent")
        issue = self.db.recent_jira_issues(limit=1)[0]
        self.assertEqual(issue.slot_id, slot.slot_id)
        self.assertEqual(issue.status, "replied")
        record = self.db.get_jira_automation_record(slot.slot_id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.subject_role, "reporter")
        self.assertEqual(record.subject_user, "reporter-a")
        self.assertTrue(record.missing_required_material)
        self.assertEqual(record.jira_comment_id, "reply-auto-issue")

    async def test_jira_status_summary_slot_uses_existing_jira_delivery_contract(self) -> None:
        inserted = self.db.insert_jira_issue(
            issue_key="OPS-300",
            project_key="OPS",
            summary="status summary issue",
            jira_created_at="2026-05-21T17:00:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"key": "OPS-300"},
            assignee="assignee-b",
            status_name="Done",
            status_id="10001",
            status_category_key="done",
        )
        self.assertTrue(inserted)
        slot = self.db.create_jira_status_summary_slot_for_issue(
            "OPS-300",
            previous_status_name="In Progress",
            status_name="Done",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
        )
        assert slot is not None
        self.assertEqual(slot.source, "jira_status_summary")
        self.assertEqual(slot.incoming_text, "")
        service = BreakwaterService(self.config(concurrency=1))
        marker = service._jira_analysis_marker(slot.slot_id)
        fake = AutomationReportCodexClient(
            self.db,
            {
                "schema_version": 1,
                "kind": "jira_status_summary",
                "has_clear_resolution": False,
                "resolution_gaps": ["没有修复版本"],
                "reason": "缺少明确修复版本",
                "confidence": "medium",
            },
        )
        fake_jira = FakeJiraVerifier([[{"id": "reply-status-summary", "body": f"status summary\n{marker}"}]])
        service.codex_client = fake  # type: ignore[assignment]
        service.jira_client = fake_jira  # type: ignore[assignment]

        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.prompt_names, ["jira_status_summary"])
        self.assertIsNotNone(fake.prompt_contexts[0])
        assert fake.prompt_contexts[0] is not None
        self.assertEqual(fake.prompt_contexts[0]["jira_issue_key"], "OPS-300")
        self.assertEqual(fake.prompt_contexts[0]["jira_comment_body"], "")
        self.assertEqual(fake.prompt_contexts[0]["jira_marker"], marker)
        self.assertEqual(fake_jira.calls, ["OPS-300"])
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "replied")
        self.assertEqual(refreshed.codex_status, "completed")
        self.assertEqual(refreshed.reply_status, "sent")
        record = self.db.get_jira_automation_record(slot.slot_id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.subject_role, "assignee")
        self.assertEqual(record.subject_user, "assignee-b")
        self.assertFalse(record.has_clear_resolution)
        self.assertEqual(record.details["resolution_gaps"], ["没有修复版本"])

    async def test_jira_auto_analysis_requires_structured_automation_report(self) -> None:
        self.db.insert_jira_issue(
            issue_key="OPS-201",
            project_key="OPS",
            summary="new incident without report",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"key": "OPS-201"},
        )
        slot = self.db.create_jira_issue_analyze_slot_for_issue("OPS-201")
        assert slot is not None
        service = BreakwaterService(self.config(concurrency=1, retries=1))
        marker = service._jira_analysis_marker(slot.slot_id)
        fake = NoReplyCodexClient()
        fake_jira = FakeJiraVerifier(
            [
                [{"id": "reply-auto-missing-report", "body": f"auto analysis\n{marker}"}],
                [{"id": "reply-auto-missing-report", "body": f"auto analysis\n{marker}"}],
            ]
        )
        service.codex_client = fake  # type: ignore[assignment]
        service.jira_client = fake_jira  # type: ignore[assignment]

        await service._run_codex_for_slot(slot)

        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.reply_status, "none")
        self.assertEqual(refreshed.status, "failed")
        self.assertEqual(fake.retry_prompts[1], f"jira auto retry {slot.slot_id} {marker} automation-report")
        record = self.db.get_jira_automation_record(slot.slot_id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.jira_comment_id, "reply-auto-missing-report")
        self.assertEqual(record.report_status, "pending")
        event_types = [event["event_type"] for event in self.db.recent_events(limit=10)]
        self.assertIn("automation.report_missing", event_types)

    def test_automation_report_cli_validates_source_specific_payload(self) -> None:
        self.db.insert_jira_issue(
            issue_key="OPS-202",
            project_key="OPS",
            summary="new incident",
            reporter="alice",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"key": "OPS-202"},
        )
        slot = self.db.create_jira_issue_analyze_slot_for_issue("OPS-202")
        assert slot is not None
        report_path = Path(self.tempdir.name) / "report.json"
        report_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "jira_issue_auto_analyze",
                    "missing_required_material": False,
                    "missing_materials": [],
                    "reason": "issue has the necessary logs",
                    "confidence": "medium",
                }
            ),
            encoding="utf-8",
        )
        args = argparse.Namespace(db=self.db_path, slot_id=slot.slot_id, json_file=report_path)

        with redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_automation_report(args), 0)

        record = self.db.get_jira_automation_record(slot.slot_id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.report_status, "reported")
        self.assertFalse(record.missing_required_material)
        self.assertEqual(record.subject_user, "alice")

        report_path.write_text(
            json.dumps(
                {
                    "kind": "jira_issue_auto_analyze",
                    "missing_required_material": "false",
                }
            ),
            encoding="utf-8",
        )
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cmd_automation_report(args), 3)
        preserved = self.db.get_jira_automation_record(slot.slot_id)
        self.assertIsNotNone(preserved)
        assert preserved is not None
        self.assertEqual(preserved.report_status, "reported")
        self.assertIsNone(preserved.error)

        self.db.insert_jira_issue(
            issue_key="OPS-204",
            project_key="OPS",
            summary="new invalid report incident",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"key": "OPS-204"},
        )
        invalid_slot = self.db.create_jira_issue_analyze_slot_for_issue("OPS-204")
        assert invalid_slot is not None
        invalid_args = argparse.Namespace(db=self.db_path, slot_id=invalid_slot.slot_id, json_file=report_path)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cmd_automation_report(invalid_args), 3)
        invalid = self.db.get_jira_automation_record(invalid_slot.slot_id)
        self.assertIsNotNone(invalid)
        assert invalid is not None
        self.assertEqual(invalid.report_status, "invalid")
        self.assertIn("JSON boolean", invalid.error or "")

    def test_admin_web_requires_login_and_serves_automation_records(self) -> None:
        self.db.insert_jira_issue(
            issue_key="OPS-203",
            project_key="OPS",
            summary="admin visible issue",
            reporter="reporter-admin",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"key": "OPS-203"},
        )
        slot = self.db.create_jira_issue_analyze_slot_for_issue("OPS-203")
        assert slot is not None
        ok, message = self.db.record_jira_automation_report(
            slot.slot_id,
            {
                "kind": "jira_issue_auto_analyze",
                "missing_required_material": True,
                "missing_materials": ["SQL"],
                "reason": "missing SQL",
                "confidence": "high",
            },
        )
        self.assertTrue(ok, message)
        self.db.insert_jira_issue(
            issue_key="OPS-205",
            project_key="OPS",
            summary="admin hidden complete initial analysis",
            reporter="complete-reporter",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"key": "OPS-205"},
        )
        complete_slot = self.db.create_jira_issue_analyze_slot_for_issue("OPS-205")
        assert complete_slot is not None
        ok, message = self.db.record_jira_automation_report(
            complete_slot.slot_id,
            {
                "kind": "jira_issue_auto_analyze",
                "missing_required_material": False,
                "missing_materials": [],
                "reason": "materials are enough",
                "confidence": "high",
            },
        )
        self.assertTrue(ok, message)
        self.db.insert_jira_issue(
            issue_key="OPS-204",
            project_key="OPS",
            summary="admin visible status summary",
            assignee="assignee-admin",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T18:31:00.000+0800",
            raw={"key": "OPS-204"},
            status_name="Done",
        )
        status_slot = self.db.create_jira_status_summary_slot_for_issue(
            "OPS-204",
            previous_status_name="In Progress",
            status_name="Done",
            jira_updated_at="2026-05-21T18:31:00.000+0800",
        )
        assert status_slot is not None
        ok, message = self.db.record_jira_automation_report(
            status_slot.slot_id,
            {
                "kind": "jira_status_summary",
                "has_clear_resolution": False,
                "resolution_gaps": ["missing fix version"],
                "reason": "missing final resolution",
                "confidence": "medium",
            },
        )
        self.assertTrue(ok, message)
        self.db.insert_jira_issue(
            issue_key="OPS-206",
            project_key="OPS",
            summary="admin hidden complete status summary",
            assignee="complete-assignee",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T18:31:00.000+0800",
            raw={"key": "OPS-206"},
            status_name="Done",
        )
        complete_status_slot = self.db.create_jira_status_summary_slot_for_issue(
            "OPS-206",
            previous_status_name="In Progress",
            status_name="Done",
            jira_updated_at="2026-05-21T18:31:00.000+0800",
        )
        assert complete_status_slot is not None
        ok, message = self.db.record_jira_automation_report(
            complete_status_slot.slot_id,
            {
                "kind": "jira_status_summary",
                "has_clear_resolution": True,
                "resolution_gaps": [],
                "reason": "has final resolution",
                "confidence": "high",
            },
        )
        self.assertTrue(ok, message)
        self.db.insert_jira_issue(
            issue_key="OPS-207",
            project_key="OPS",
            summary="admin old visible issue",
            reporter="old-reporter",
            jira_created_at="2026-05-10T17:31:00.000+0800",
            jira_updated_at="2026-05-10T17:31:00.000+0800",
            raw={"key": "OPS-207"},
        )
        old_slot = self.db.create_jira_issue_analyze_slot_for_issue("OPS-207")
        assert old_slot is not None
        ok, message = self.db.record_jira_automation_report(
            old_slot.slot_id,
            {
                "kind": "jira_issue_auto_analyze",
                "missing_required_material": True,
                "missing_materials": ["profile"],
                "reason": "old missing profile",
                "confidence": "medium",
            },
        )
        self.assertTrue(ok, message)
        self.db.insert_jira_issue(
            issue_key="OPS-208",
            project_key="OPS",
            summary="admin old visible status summary",
            assignee="old-assignee",
            jira_created_at="2026-05-10T17:31:00.000+0800",
            jira_updated_at="2026-05-10T18:31:00.000+0800",
            raw={"key": "OPS-208"},
            status_name="Done",
        )
        old_status_slot = self.db.create_jira_status_summary_slot_for_issue(
            "OPS-208",
            previous_status_name="In Progress",
            status_name="Done",
            jira_updated_at="2026-05-10T18:31:00.000+0800",
        )
        assert old_status_slot is not None
        ok, message = self.db.record_jira_automation_report(
            old_status_slot.slot_id,
            {
                "kind": "jira_status_summary",
                "has_clear_resolution": False,
                "resolution_gaps": ["missing close note"],
                "reason": "old missing close note",
                "confidence": "medium",
            },
        )
        self.assertTrue(ok, message)
        fixed_now = datetime(2026, 6, 18, 10, 30, tzinfo=timezone(timedelta(hours=8)))
        recent_created_at = (fixed_now.astimezone(timezone.utc) - timedelta(hours=1)).isoformat()
        old_created_at = (fixed_now.astimezone(timezone.utc) - timedelta(days=8)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE jira_automation_records SET created_at=?, updated_at=? WHERE slot_id IN (?, ?)",
                (recent_created_at, recent_created_at, slot.slot_id, status_slot.slot_id),
            )
            conn.execute(
                "UPDATE jira_automation_records SET created_at=?, updated_at=? WHERE slot_id IN (?, ?)",
                (old_created_at, old_created_at, old_slot.slot_id, old_status_slot.slot_id),
            )

        class FixedAdminDateTime(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[no-untyped-def]
                return fixed_now.astimezone(tz) if tz else fixed_now

        password_hash = hash_admin_password("secret", salt=b"fixed-salt")
        server = BreakwaterAdminServer(
            "127.0.0.1",
            0,
            self.db,
            password_hash=password_hash,
            session_secret="test-secret",
            jira_base_url="https://jira.example.com",
        )
        server.start()
        assert server._httpd is not None
        base = f"http://127.0.0.1:{server._httpd.server_port}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                urllib.request.urlopen(f"{base}/api/auto-analysis")
            self.assertEqual(unauthorized.exception.code, 401)

            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            login = urllib.request.Request(
                f"{base}/login",
                data=b"password=secret",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            opener.open(login).read()
            with patch("breakwater.admin_web.datetime", FixedAdminDateTime):
                payload = json.loads(opener.open(f"{base}/api/auto-analysis").read().decode("utf-8"))
                status_payload = json.loads(opener.open(f"{base}/api/status-summaries").read().decode("utf-8"))
                week_payload = json.loads(opener.open(f"{base}/api/auto-analysis?view=week").read().decode("utf-8"))
                week_status_payload = json.loads(opener.open(f"{base}/api/status-summaries?view=week").read().decode("utf-8"))
                current_week_payload = json.loads(opener.open(f"{base}/api/auto-analysis?view=current_week").read().decode("utf-8"))
                current_week_status_payload = json.loads(opener.open(f"{base}/api/status-summaries?view=current_week").read().decode("utf-8"))
                current_month_payload = json.loads(opener.open(f"{base}/api/auto-analysis?view=current_month").read().decode("utf-8"))
                current_month_status_payload = json.loads(opener.open(f"{base}/api/status-summaries?view=current_month").read().decode("utf-8"))
                html = opener.open(f"{base}/auto-analysis").read().decode("utf-8")
                week_html = opener.open(f"{base}/auto-analysis?view=week").read().decode("utf-8")
                current_week_html = opener.open(f"{base}/auto-analysis?view=current_week").read().decode("utf-8")
                auto_week_csv_response = opener.open(f"{base}/auto-analysis.csv?view=week")
                auto_week_csv_content_type = auto_week_csv_response.headers.get("Content-Type")
                auto_week_csv_disposition = auto_week_csv_response.headers.get("Content-Disposition")
                auto_week_csv = auto_week_csv_response.read().decode("utf-8")
                status_week_csv_response = opener.open(f"{base}/status-summaries.csv?view=week")
                status_week_csv_disposition = status_week_csv_response.headers.get("Content-Disposition")
                status_week_csv = status_week_csv_response.read().decode("utf-8")
                auto_month_csv_response = opener.open(f"{base}/auto-analysis.csv?view=current_month")
                auto_month_csv_disposition = auto_month_csv_response.headers.get("Content-Disposition")
                auto_month_csv = auto_month_csv_response.read().decode("utf-8")
                auto_all_csv = opener.open(f"{base}/auto-analysis.csv?view=all").read().decode("utf-8")
                status_all_csv = opener.open(f"{base}/status-summaries.csv?view=all").read().decode("utf-8")
                with self.assertRaises(urllib.error.HTTPError) as combined_csv:
                    opener.open(f"{base}/automation.csv?view=week")
                self.assertEqual(combined_csv.exception.code, 404)
        finally:
            server.stop()

        self.assertEqual([record["slot_id"] for record in payload["records"]], [slot.slot_id, old_slot.slot_id])
        self.assertEqual(payload["records"][0]["slot_id"], slot.slot_id)
        self.assertEqual(payload["records"][0]["subject_role"], "reporter")
        self.assertTrue(payload["records"][0]["missing_required_material"])
        self.assertEqual([record["slot_id"] for record in status_payload["records"]], [status_slot.slot_id, old_status_slot.slot_id])
        self.assertFalse(status_payload["records"][0]["has_clear_resolution"])
        self.assertEqual([record["slot_id"] for record in week_payload["records"]], [slot.slot_id])
        self.assertEqual([record["slot_id"] for record in week_status_payload["records"]], [status_slot.slot_id])
        self.assertEqual([record["slot_id"] for record in current_week_payload["records"]], [slot.slot_id])
        self.assertEqual([record["slot_id"] for record in current_week_status_payload["records"]], [status_slot.slot_id])
        self.assertEqual([record["slot_id"] for record in current_month_payload["records"]], [slot.slot_id, old_slot.slot_id])
        self.assertEqual([record["slot_id"] for record in current_month_status_payload["records"]], [status_slot.slot_id, old_status_slot.slot_id])
        self.assertIn("提 Jira 时信息不全", html)
        self.assertIn("关闭 Jira 时结论不全", html)
        self.assertIn("全量", html)
        self.assertIn("近 7 天", html)
        self.assertIn("当前周", html)
        self.assertIn("当前月", html)
        self.assertIn("下载 CSV", html)
        self.assertEqual(html.count("下载 CSV"), 2)
        self.assertIn('href="/auto-analysis.csv?view=all"', html)
        self.assertIn('href="/status-summaries.csv?view=all"', html)
        self.assertIn("Reporter", html)
        self.assertIn("reporter-admin", html)
        self.assertIn("old-reporter", html)
        self.assertNotIn("old-reporter", week_html)
        self.assertNotIn("old-reporter", current_week_html)
        self.assertNotIn("complete-reporter", html)
        self.assertIn("Assignee", html)
        self.assertIn("assignee-admin", html)
        self.assertIn("old-assignee", html)
        self.assertNotIn("old-assignee", week_html)
        self.assertNotIn("complete-assignee", html)
        self.assertIn("href='https://jira.example.com/browse/OPS-203'", html)
        self.assertIn("href='https://jira.example.com/browse/OPS-204'", html)
        self.assertNotIn("href='https://jira.example.com/browse/OPS-205'", html)
        self.assertNotIn("href='https://jira.example.com/browse/OPS-206'", html)
        self.assertNotIn("<th>评论</th>", html)
        self.assertNotIn("<th>材料不全</th>", html)
        self.assertNotIn("<th>结论不全</th>", html)
        self.assertEqual(auto_week_csv_content_type, "text/csv; charset=utf-8")
        self.assertEqual(auto_week_csv_disposition, 'attachment; filename="breakwater-auto-analysis-week.csv"')
        self.assertEqual(status_week_csv_disposition, 'attachment; filename="breakwater-status-summaries-week.csv"')
        auto_week_rows = list(csv.DictReader(io.StringIO(auto_week_csv)))
        status_week_rows = list(csv.DictReader(io.StringIO(status_week_csv)))
        auto_month_rows = list(csv.DictReader(io.StringIO(auto_month_csv)))
        auto_all_rows = list(csv.DictReader(io.StringIO(auto_all_csv)))
        status_all_rows = list(csv.DictReader(io.StringIO(status_all_csv)))
        self.assertEqual({row["slot_id"] for row in auto_week_rows}, {slot.slot_id})
        self.assertEqual({row["slot_id"] for row in status_week_rows}, {status_slot.slot_id})
        self.assertEqual(auto_month_csv_disposition, 'attachment; filename="breakwater-auto-analysis-current_month.csv"')
        self.assertEqual({row["slot_id"] for row in auto_month_rows}, {slot.slot_id, old_slot.slot_id})
        self.assertEqual({row["slot_id"] for row in auto_all_rows}, {slot.slot_id, old_slot.slot_id})
        self.assertEqual({row["slot_id"] for row in status_all_rows}, {status_slot.slot_id, old_status_slot.slot_id})
        auto_week_by_slot = {row["slot_id"]: row for row in auto_week_rows}
        self.assertNotIn("category", auto_week_by_slot[slot.slot_id])
        self.assertEqual(auto_week_by_slot[slot.slot_id]["issue_key"], "OPS-203")
        self.assertEqual(auto_week_by_slot[slot.slot_id]["jira_url"], "https://jira.example.com/browse/OPS-203")
        self.assertEqual(auto_week_by_slot[slot.slot_id]["subject_role"], "reporter")
        self.assertEqual(auto_week_by_slot[slot.slot_id]["subject_user"], "reporter-admin")
        self.assertEqual(auto_week_by_slot[slot.slot_id]["report_status"], "reported")
        self.assertEqual(auto_week_by_slot[slot.slot_id]["reason"], "missing SQL")
        self.assertEqual(
            json.loads(auto_week_by_slot[slot.slot_id]["details"]),
            {"missing_materials": ["SQL"], "confidence": "high"},
        )
        self.assertNotIn(complete_slot.slot_id, {row["slot_id"] for row in auto_all_rows})
        self.assertNotIn(complete_status_slot.slot_id, {row["slot_id"] for row in status_all_rows})

    def test_admin_view_time_boundaries_are_local_and_inclusive(self) -> None:
        fixed_now = datetime(2026, 6, 18, 10, 30, 45, 123456, tzinfo=timezone(timedelta(hours=8)))
        self.assertIsNone(admin_web._created_after_for_view(admin_web.ADMIN_VIEW_ALL, now=fixed_now))
        self.assertEqual(
            admin_web._created_after_for_view(admin_web.ADMIN_VIEW_WEEK, now=fixed_now),
            "2026-06-11T02:30:45.123456+00:00",
        )
        self.assertEqual(
            admin_web._created_after_for_view(admin_web.ADMIN_VIEW_CURRENT_WEEK, now=fixed_now),
            "2026-06-14T16:00:00+00:00",
        )
        month_boundary = admin_web._created_after_for_view(admin_web.ADMIN_VIEW_CURRENT_MONTH, now=fixed_now)
        self.assertEqual(month_boundary, "2026-05-31T16:00:00+00:00")

        self.db.insert_jira_issue(
            issue_key="OPS-209",
            project_key="OPS",
            summary="boundary included",
            reporter="boundary-reporter",
            jira_created_at="2026-06-01T00:00:00.000+0800",
            jira_updated_at="2026-06-01T00:00:00.000+0800",
            raw={"key": "OPS-209"},
        )
        boundary_slot = self.db.create_jira_issue_analyze_slot_for_issue("OPS-209")
        assert boundary_slot is not None
        ok, message = self.db.record_jira_automation_report(
            boundary_slot.slot_id,
            {
                "kind": "jira_issue_auto_analyze",
                "missing_required_material": True,
                "missing_materials": ["SQL"],
                "reason": "boundary row",
                "confidence": "high",
            },
        )
        self.assertTrue(ok, message)
        self.db.insert_jira_issue(
            issue_key="OPS-210",
            project_key="OPS",
            summary="before boundary excluded",
            reporter="before-boundary-reporter",
            jira_created_at="2026-05-31T23:59:59.999+0800",
            jira_updated_at="2026-05-31T23:59:59.999+0800",
            raw={"key": "OPS-210"},
        )
        before_slot = self.db.create_jira_issue_analyze_slot_for_issue("OPS-210")
        assert before_slot is not None
        ok, message = self.db.record_jira_automation_report(
            before_slot.slot_id,
            {
                "kind": "jira_issue_auto_analyze",
                "missing_required_material": True,
                "missing_materials": ["SQL"],
                "reason": "before boundary row",
                "confidence": "high",
            },
        )
        self.assertTrue(ok, message)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE jira_automation_records SET created_at=?, updated_at=? WHERE slot_id=?",
                ("2026-06-01T00:00:00+08:00", "2026-06-01T00:00:00+08:00", boundary_slot.slot_id),
            )
            conn.execute(
                "UPDATE jira_automation_records SET created_at=?, updated_at=? WHERE slot_id=?",
                ("2026-05-31T23:59:59.999999+08:00", "2026-05-31T23:59:59.999999+08:00", before_slot.slot_id),
            )

        records = self.db.list_jira_automation_records(
            source=admin_web.JIRA_ISSUE_AUTO_ANALYZE_SOURCE,
            attention_only=True,
            created_after=month_boundary,
            limit=None,
        )
        self.assertIn(boundary_slot.slot_id, {record.slot_id for record in records})
        self.assertNotIn(before_slot.slot_id, {record.slot_id for record in records})

    async def test_github_issue_slot_is_sent_to_codex_with_github_prompt_context(self) -> None:
        self.db.insert_github_issue(
            repo_full_name="apache/doris",
            issue_number=42,
            node_id="I_42",
            title="GitHub issue title",
            body="GitHub issue body",
            author="alice",
            state="open",
            html_url="https://github.com/apache/doris/issues/42",
            labels=[{"name": "bug"}],
            github_created_at="2026-05-21T09:31:00Z",
            github_updated_at="2026-05-21T09:31:00Z",
            raw={"number": 42},
        )
        slot = self.db.create_github_issue_slot_for_issue("apache/doris", 42)
        assert slot is not None
        service = BreakwaterService(self.config(concurrency=1))
        marker = service._github_analysis_marker(slot.slot_id)
        fake = NoReplyCodexClient()
        fake_github = FakeGitHubVerifier([[{"id": 9001, "body": f"analysis\n{marker}"}]])
        service.codex_client = fake  # type: ignore[assignment]
        service.github_client = fake_github  # type: ignore[assignment]

        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.prompt_names, ["github_issue_analyze"])
        self.assertIsNotNone(fake.prompt_contexts[0])
        assert fake.prompt_contexts[0] is not None
        self.assertEqual(fake.prompt_contexts[0]["github_repo"], "apache/doris")
        self.assertEqual(fake.prompt_contexts[0]["github_issue_number"], "42")
        self.assertEqual(fake.prompt_contexts[0]["github_marker"], marker)
        self.assertEqual(fake_github.calls, [("apache/doris", 42)])
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.reply_status, "sent")
        event_types = [event["event_type"] for event in self.db.recent_events(limit=10)]
        self.assertIn("analysis.comment_verified", event_types)

    async def test_later_jira_analyze_resumes_issue_case_thread(self) -> None:
        self.db.insert_jira_comment(
            comment_id="comment-first",
            issue_key="OPS-77",
            project_key="OPS",
            author="Alice",
            body="/analyze first",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"id": "comment-first"},
        )
        first = self.db.create_jira_analyze_slot_for_comment("comment-first")
        assert first is not None
        self.db.mark_codex_turn(first.slot_id, "thread-ops-77", "turn-first")
        self.db.mark_reply_sent(first.slot_id, "jira-reply-first")
        self.db.mark_codex_completed(first.slot_id)

        self.db.insert_jira_comment(
            comment_id="comment-second",
            issue_key="OPS-77",
            project_key="OPS",
            author="Bob",
            body="/analyze follow up",
            jira_created_at="2026-05-21T17:35:00.000+0800",
            jira_updated_at="2026-05-21T17:35:00.000+0800",
            raw={"id": "comment-second"},
        )
        second = self.db.create_jira_analyze_slot_for_comment("comment-second")
        assert second is not None
        self.assertEqual(first.case_id, second.case_id)

        service = BreakwaterService(self.config(concurrency=1))
        marker = service._jira_analysis_marker(second.slot_id)
        fake = CaseRecordingCodexClient(self.db)
        service.codex_client = fake  # type: ignore[assignment]
        service.jira_client = FakeJiraVerifier([[{"id": "jira-reply-second", "body": f"analysis\n{marker}"}]])  # type: ignore[assignment]

        await service._run_codex_for_slot(second)

        self.assertEqual(fake.prompt_names, ["jira_analyze_continue"])
        self.assertEqual(fake.resume_thread_ids, ["thread-ops-77"])
        case = self.db.get_case(second.case_id or "")
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(case.latest_codex_thread_id, "thread-ops-77")
        self.assertEqual(case.latest_slot_id, second.slot_id)
        self.assertIsNotNone(case.summary)
        self.assertIn("verified Jira comment jira-reply-second", case.summary or "")

    async def test_lark_message_can_locate_case_by_jira_key_and_continue_thread(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-88", title="OPS-88")
        seed = self.db.create_slot(
            source="jira_analyze",
            incoming_text="/analyze",
            case_id=case.case_id,
            delivery_target="jira_comment",
            jira_issue_key="OPS-88",
        )
        self.db.mark_codex_turn(seed.slot_id, "thread-ops-88", "turn-seed")

        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FakeLarkReplyClient(mentioned_message_ids={"message-case-followup"})  # type: ignore[assignment]
        event = LarkEvent(
            event_id="event-case-followup",
            message_id="message-case-followup",
            chat_id="chat",
            chat_type="group",
            message_type="text",
            sender_id="ou_user",
            content="@bot 继续 OPS-88，这个结论的证据是什么？",
            raw={"event_id": "event-case-followup"},
        )

        await service._handle_lark_event(event)

        slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-case-followup")
        self.assertEqual(slot.source, "lark_case_followup")
        self.assertEqual(slot.case_id, case.case_id)

        fake = CaseRecordingCodexClient(self.db, reply=True)
        service.codex_client = fake  # type: ignore[assignment]
        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.prompt_names, ["lark_case_followup"])
        self.assertEqual(fake.resume_thread_ids, ["thread-ops-88"])
        self.assertEqual(fake.prompt_contexts[0]["case_id"], case.case_id)
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.reply_status, "pending")
        summarized_case = self.db.get_case(case.case_id)
        self.assertIsNotNone(summarized_case)
        assert summarized_case is not None
        self.assertIn("case followup reply", summarized_case.summary or "")

    async def test_lark_group_name_can_locate_case_without_text_reference(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-92", title="OPS-92")
        seed = self.db.create_slot(
            source="jira_analyze",
            incoming_text="/analyze",
            case_id=case.case_id,
            delivery_target="jira_comment",
            jira_issue_key="OPS-92",
        )
        self.db.mark_codex_turn(seed.slot_id, "thread-ops-92", "turn-seed")
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FakeLarkReplyClient(
            chat_names={"chat": "OPS-92 现场群"},
            mentioned_message_ids={"message-chat-name-followup"},
        )  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-chat-name-followup",
                message_id="message-chat-name-followup",
                chat_id="chat",
                chat_type="group",
                message_type="text",
                sender_id="ou_user",
                content="@bot 这个结论的证据是什么？",
                raw={"event_id": "event-chat-name-followup"},
            )
        )

        slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-chat-name-followup")
        self.assertEqual(slot.source, "lark_case_followup")
        self.assertEqual(slot.case_id, case.case_id)

        fake = CaseRecordingCodexClient(self.db, reply=True)
        service.codex_client = fake  # type: ignore[assignment]
        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.prompt_names, ["lark_case_followup"])
        self.assertEqual(fake.resume_thread_ids, ["thread-ops-92"])

    async def test_lark_case_binds_to_one_group_and_rejects_other_group(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-95", title="OPS-95")
        seed = self.db.create_slot(source="jira_analyze", incoming_text="/analyze", case_id=case.case_id)
        self.db.mark_codex_turn(seed.slot_id, "thread-ops-95", "turn-seed")
        service = BreakwaterService(self.config(concurrency=1))
        fake_lark = FakeLarkReplyClient(
            chat_names={"group-a": "OPS-95 主群", "group-b": "OPS-95 临时群"},
            mentioned_message_ids={"message-group-a", "message-group-b"},
        )
        service.lark_client = fake_lark  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-group-a",
                message_id="message-group-a",
                chat_id="group-a",
                chat_type="group",
                message_type="text",
                sender_id="ou_user",
                content="@bot 继续 OPS-95",
                raw={"event_id": "event-group-a"},
            )
        )

        first_slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-group-a")
        self.assertEqual(first_slot.source, "lark_case_followup")
        bound_case = self.db.get_case(case.case_id)
        self.assertIsNotNone(bound_case)
        assert bound_case is not None
        self.assertEqual(bound_case.bound_lark_chat_id, "group-a")
        self.assertEqual(bound_case.bound_lark_chat_name, "OPS-95 主群")
        self.assertEqual(service._codex_queue.qsize(), 1)

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-group-b",
                message_id="message-group-b",
                chat_id="group-b",
                chat_type="group",
                message_type="text",
                sender_id="ou_user",
                content="@bot 继续 OPS-95",
                raw={"event_id": "event-group-b"},
            )
        )

        self.assertEqual(service._codex_queue.qsize(), 1)
        conflict_slot = next(slot for slot in self.db.recent_lark_slots(limit=8) if slot.lark_event_id == "event-group-b")
        self.assertEqual(conflict_slot.source, "lark_case_binding_conflict")
        self.assertEqual(conflict_slot.case_id, case.case_id)
        pending = next(reply for reply in self.db.pending_replies() if reply.slot_id == conflict_slot.slot_id)
        self.assertIn("已经绑定到另一个飞书群", pending.reply_text or "")
        self.assertIn("OPS-95 主群", pending.reply_text or "")
        self.assertIn("group-a", pending.reply_text or "")

    async def test_lark_group_mention_can_start_new_jira_analysis_case(self) -> None:
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FakeLarkReplyClient(
            chat_names={"new-group": "APP-26000 现场群"},
            mentioned_message_ids={"message-new-analysis"},
        )  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-new-context",
                message_id="message-new-context",
                chat_id="new-group",
                chat_type="group",
                message_type="text",
                sender_id="ou_a",
                content="前置背景：刚刚看到查询超时",
                raw={"event_id": "event-new-context"},
            )
        )
        await service._handle_lark_event(
            LarkEvent(
                event_id="event-new-analysis",
                message_id="message-new-analysis",
                chat_id="new-group",
                chat_type="group",
                message_type="text",
                sender_id="ou_b",
                content="@bot 帮忙分析一下",
                raw={"event_id": "event-new-analysis"},
            )
        )

        slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-new-analysis")
        self.assertEqual(slot.source, "lark_jira_analyze")
        self.assertEqual(slot.jira_issue_key, "APP-26000")
        self.assertEqual(slot.delivery_target, "jira_comment_and_lark_reply")
        self.assertIn("群聊上下文", slot.incoming_text)
        self.assertIn("刚刚看到查询超时", slot.incoming_text)
        case = self.db.find_case_by_alias("jira_issue_key", "APP-26000")
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(slot.case_id, case.case_id)
        self.assertEqual(case.bound_lark_chat_id, "new-group")
        self.assertEqual(case.bound_lark_chat_name, "APP-26000 现场群")
        self.assertEqual(service._codex_queue.qsize(), 1)

        marker = service._jira_analysis_marker(slot.slot_id)
        fake = CaseRecordingCodexClient(self.db, reply=True)
        service.codex_client = fake  # type: ignore[assignment]
        service.jira_client = FakeJiraVerifier([[{"id": "jira-lark-new", "body": f"analysis\n{marker}"}]])  # type: ignore[assignment]
        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.prompt_names, ["lark_jira_analyze"])
        self.assertEqual(fake.resume_thread_ids, [None])
        self.assertEqual(fake.prompt_contexts[0]["jira_issue_key"], "APP-26000")
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.codex_status, "completed")
        self.assertEqual(refreshed.reply_status, "pending")
        event_types = [event["event_type"] for event in self.db.recent_events(limit=20)]
        self.assertIn("lark_analysis.dual_delivery_verified", event_types)

    async def test_group_lark_display_name_text_does_not_count_as_real_mention(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="APP-25963", title="APP-25963")
        seed = self.db.create_slot(source="jira_analyze", incoming_text="/analyze", case_id=case.case_id)
        self.db.mark_codex_turn(seed.slot_id, "thread-app-25963", "turn-seed")
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FakeLarkReplyClient(chat_names={"chat": "APP-25963 测试一下"})  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-display-text-only",
                message_id="message-display-text-only",
                chat_id="chat",
                chat_type="group",
                message_type="text",
                sender_id="ou_user",
                content="@Breakwater Bot 这个问题之前的结论是啥？",
                raw={"event_id": "event-display-text-only"},
            )
        )

        self.assertEqual(service._codex_queue.qsize(), 0)
        self.assertFalse(any(slot.lark_event_id == "event-display-text-only" for slot in self.db.recent_lark_slots(limit=5)))
        message = next(item for item in self.db.recent_lark_messages(limit=5) if item.message_id == "message-display-text-only")
        self.assertFalse(message.mentioned_bot)
        self.assertIsNone(message.handled_slot_id)

    async def test_unhandled_lark_mention_is_recovered_via_message_api(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="APP-25963", title="APP-25963")
        seed = self.db.create_slot(source="jira_analyze", incoming_text="/analyze", case_id=case.case_id)
        self.db.mark_codex_turn(seed.slot_id, "thread-app-25963", "turn-seed")
        self.db.record_lark_message(
            message_id="message-recover-mention",
            event_id="event-recover-mention",
            chat_id="chat",
            chat_type="group",
            chat_name="APP-25963 测试一下",
            sender_id="ou_user",
            message_type="text",
            content="@Breakwater Bot 这个问题之前的结论是啥？",
            mentioned_bot=False,
            raw={"event_id": "event-recover-mention"},
        )
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FakeLarkReplyClient(mentioned_message_ids={"message-recover-mention"})  # type: ignore[assignment]

        await service._recover_unhandled_lark_mentions()

        slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-recover-mention")
        self.assertEqual(slot.source, "lark_case_followup")
        self.assertEqual(slot.case_id, case.case_id)
        self.assertEqual(slot.codex_thread_id, None)
        self.assertEqual(service._codex_queue.qsize(), 1)
        message = next(item for item in self.db.recent_lark_messages(limit=5) if item.message_id == "message-recover-mention")
        self.assertTrue(message.mentioned_bot)
        self.assertEqual(message.handled_slot_id, slot.slot_id)

    async def test_group_lark_messages_require_bot_mention_but_preserve_context(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-93", title="OPS-93")
        seed = self.db.create_slot(source="jira_analyze", incoming_text="/analyze", case_id=case.case_id)
        self.db.mark_codex_turn(seed.slot_id, "thread-ops-93", "turn-seed")
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FakeLarkReplyClient(chat_names={"chat": "OPS-93 现场群"})  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-context-before",
                message_id="message-context-before",
                chat_id="chat",
                chat_type="group",
                message_type="text",
                sender_id="ou_a",
                content="这里先补一条关键背景",
                raw={"event_id": "event-context-before"},
            )
        )
        self.assertEqual(service._codex_queue.qsize(), 0)
        self.assertFalse(any(slot.lark_event_id == "event-context-before" for slot in self.db.recent_lark_slots(limit=5)))

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-mentioned-first",
                message_id="message-mentioned-first",
                chat_id="chat",
                chat_type="group",
                message_type="text",
                sender_id="ou_b",
                content="请结合上面背景解释一下",
                raw={"event": {"message": {"mentions": [{"id": {"open_id": "cli_bot"}}]}}},
            )
        )
        first_slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-mentioned-first")
        self.assertEqual(first_slot.source, "lark_case_followup")
        self.assertIn("群聊上下文", first_slot.incoming_text)
        self.assertIn("这里先补一条关键背景", first_slot.incoming_text)
        self.assertIn("当前触发消息", first_slot.incoming_text)

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-context-after",
                message_id="message-context-after",
                chat_id="chat",
                chat_type="group",
                message_type="text",
                sender_id="ou_c",
                content="这是第二轮之前的新背景",
                raw={"event_id": "event-context-after"},
            )
        )
        await service._handle_lark_event(
            LarkEvent(
                event_id="event-mentioned-second",
                message_id="message-mentioned-second",
                chat_id="chat",
                chat_type="group",
                message_type="text",
                sender_id="ou_b",
                content="再继续看这个新背景",
                raw={"event": {"message": {"mentions": [{"id": {"open_id": "cli_bot"}}]}}},
            )
        )
        second_slot = next(slot for slot in self.db.recent_lark_slots(limit=8) if slot.lark_event_id == "event-mentioned-second")
        self.assertIn("这是第二轮之前的新背景", second_slot.incoming_text)
        self.assertNotIn("这里先补一条关键背景", second_slot.incoming_text)
        current_message = next(message for message in self.db.recent_lark_messages(limit=5) if message.message_id == "message-mentioned-second")
        self.assertTrue(current_message.mentioned_bot)
        self.assertEqual(current_message.handled_slot_id, second_slot.slot_id)

    async def test_group_lark_post_image_context_is_passed_to_codex_when_later_mentioned(self) -> None:
        image_key = "img_v3_group_context"
        content = feishu_post_content(image_key=image_key, prefix="上文截图里有同步任务状态")
        raw = feishu_post_raw(
            event_id="event-group-post-context",
            message_id="om_group_post_context",
            chat_id="group-post-chat",
            chat_type="group",
            sender_id="ou_context",
            content=content,
        )
        service = BreakwaterService(self.config(concurrency=1))
        lark = FakeLarkReplyClient(
            chat_names={"group-post-chat": "OPS-193 现场群"},
            mentioned_message_ids={"message-group-current"},
            image_bytes_by_key={image_key: TEST_PNG_BYTES},
        )
        service.lark_client = lark  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-group-post-context",
                message_id="om_group_post_context",
                chat_id="group-post-chat",
                chat_type="group",
                message_type="post",
                sender_id="ou_context",
                content=content,
                raw=raw,
            )
        )
        self.assertEqual(service._codex_queue.qsize(), 0)
        recorded = next(item for item in self.db.recent_lark_messages(limit=5) if item.message_id == "om_group_post_context")
        self.assertEqual(recorded.message_type, "post")
        self.assertIn("[Image: img_v3_group_context, 1800x150]", recorded.content)

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-group-current",
                message_id="message-group-current",
                chat_id="group-post-chat",
                chat_type="group",
                message_type="text",
                sender_id="ou_current",
                content="帮我结合上面的截图看一下",
                raw={"event": {"message": {"mentions": [{"id": {"open_id": "cli_bot"}}]}}},
            )
        )

        slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-group-current")
        self.assertIn("群聊上下文", slot.incoming_text)
        self.assertIn("上文截图里有同步任务状态", slot.incoming_text)
        slot_raw = self.db.get_slot_raw(slot.slot_id)
        self.assertEqual(slot_raw["breakwater_lark_images"][0]["file_key"], image_key)
        self.assertEqual(slot_raw["breakwater_lark_images"][0]["source"], "context")

        fake = CaseRecordingCodexClient(self.db, reply=True)
        service.codex_client = fake  # type: ignore[assignment]
        await service._run_codex_for_slot(slot)

        self.assertEqual(lark.image_downloads[0][0], "om_group_post_context")
        self.assertEqual(lark.image_downloads[0][1], image_key)
        self.assertEqual(len(fake.input_attachments[0]), 1)
        self.assertIn("source=context", fake.incoming_texts[0])
        self.assertTrue(fake.input_attachments[0][0].path.exists())

    async def test_group_lark_message_without_case_reuses_incremental_context_builder(self) -> None:
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FakeLarkReplyClient(mentioned_message_ids={"message-generic-mentioned"})  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-generic-context",
                message_id="message-generic-context",
                chat_id="generic-group",
                chat_type="group",
                message_type="text",
                sender_id="ou_a",
                content="先补一个非 Jira 的背景",
                raw={"event_id": "event-generic-context"},
            )
        )
        await service._handle_lark_event(
            LarkEvent(
                event_id="event-generic-mentioned",
                message_id="message-generic-mentioned",
                chat_id="generic-group",
                chat_type="group",
                message_type="text",
                sender_id="ou_b",
                content="帮我分析这个现象",
                raw={"event": {"message": {"mentions": [{"id": {"open_id": "cli_bot"}}]}}},
            )
        )

        slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-generic-mentioned")
        self.assertEqual(slot.source, "lark_case_followup")
        self.assertIsNotNone(slot.case_id)
        case = self.db.get_case(slot.case_id or "")
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(case.scope_type, "lark_chat")
        self.assertEqual(case.scope_key, "group:generic-group")
        self.assertEqual(case.bound_lark_chat_id, "generic-group")
        self.assertIn("群聊上下文", slot.incoming_text)
        self.assertIn("该 case", slot.incoming_text)
        self.assertIn("先补一个非 Jira 的背景", slot.incoming_text)
        self.assertIn("当前触发消息", slot.incoming_text)

    async def test_direct_lark_message_reuses_incremental_context_builder(self) -> None:
        self.db.record_lark_message(
            message_id="message-direct-context",
            event_id="event-direct-context",
            chat_id="direct-chat",
            chat_type="p2p",
            chat_name=None,
            sender_id="ou_user",
            message_type="text",
            content="单聊里先补一个背景",
            mentioned_bot=True,
            raw={"event_id": "event-direct-context"},
        )
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FakeLarkReplyClient()  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-direct-current",
                message_id="message-direct-current",
                chat_id="direct-chat",
                chat_type="p2p",
                message_type="text",
                sender_id="ou_user",
                content="现在回答这个问题",
                raw={"event_id": "event-direct-current"},
            )
        )

        slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-direct-current")
        self.assertEqual(slot.source, "lark_case_followup")
        self.assertIsNotNone(slot.case_id)
        case = self.db.get_case(slot.case_id or "")
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(case.scope_type, "lark_chat")
        self.assertEqual(case.scope_key, "p2p:user:ou_user")
        self.assertEqual(self.db.find_case_by_alias("lark_sender_id", "ou_user"), case)
        self.assertIn("单聊上下文", slot.incoming_text)
        self.assertIn("该 case", slot.incoming_text)
        self.assertIn("单聊里先补一个背景", slot.incoming_text)
        self.assertIn("当前触发消息", slot.incoming_text)

    async def test_direct_lark_post_with_image_creates_slot_and_passes_local_image_to_codex(self) -> None:
        image_key = "img_v3_0212h_real_p2p"
        content = feishu_post_content(image_key=image_key)
        raw = feishu_post_raw(
            event_id="event-direct-post",
            message_id="om_direct_post",
            chat_id="direct-post-chat",
            chat_type="p2p",
            sender_id="ou_post_user",
            content=content,
        )
        service = BreakwaterService(self.config(concurrency=1))
        lark = FakeLarkReplyClient(image_bytes_by_key={image_key: TEST_PNG_BYTES})
        service.lark_client = lark  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-direct-post",
                message_id="om_direct_post",
                chat_id="direct-post-chat",
                chat_type="p2p",
                message_type="post",
                sender_id="ou_post_user",
                content=content,
                raw=raw,
            )
        )

        slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-direct-post")
        self.assertEqual(slot.source, "lark_case_followup")
        self.assertIn("版本是4.1.0的存算分离模式", slot.incoming_text)
        self.assertIn("[Image: img_v3_0212h_real_p2p, 1800x150]", slot.incoming_text)
        message = next(item for item in self.db.recent_lark_messages(limit=5) if item.message_id == "om_direct_post")
        self.assertEqual(message.message_type, "post")
        self.assertIn("周期同步看起来也卡住了", message.content)
        slot_raw = self.db.get_slot_raw(slot.slot_id)
        self.assertEqual(slot_raw["breakwater_lark_images"][0]["file_key"], image_key)
        self.assertEqual(slot_raw["breakwater_lark_images"][0]["source"], "current")

        fake = CaseRecordingCodexClient(self.db, reply=True)
        service.codex_client = fake  # type: ignore[assignment]
        await service._run_codex_for_slot(slot)

        self.assertEqual(lark.image_downloads[0][0], "om_direct_post")
        self.assertEqual(lark.image_downloads[0][1], image_key)
        self.assertEqual(len(fake.input_attachments[0]), 1)
        image_path = fake.input_attachments[0][0].path
        self.assertEqual(image_path.suffix, ".png")
        self.assertTrue(image_path.exists())
        self.assertIn("飞书图片附件", fake.incoming_texts[0])

    async def test_direct_lark_cli_post_placeholder_image_is_downloaded_for_codex(self) -> None:
        image_key = "img_v3_0212h_03d2c494-2c69-4593-94bf-27b6a5634e9g"
        content = f"[Image: {image_key}]\n这张图说了啥？"
        raw = {
            "type": "im.message.receive_v1",
            "event_id": "910d887afff0d0474c901ceb6a5ad324",
            "timestamp": "1781098812728",
            "id": "om_x100b6da2dd08bca0c38b3a598ad7a9c",
            "message_id": "om_x100b6da2dd08bca0c38b3a598ad7a9c",
            "create_time": "1781098812427",
            "chat_id": "oc_7d1fe6e9e0d8a92df66418f0f690a451",
            "chat_type": "p2p",
            "message_type": "post",
            "sender_id": "ou_test_user_001",
            "content": content,
        }
        service = BreakwaterService(self.config(concurrency=1))
        lark = FakeLarkReplyClient(image_bytes_by_key={image_key: TEST_PNG_BYTES})
        service.lark_client = lark  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="910d887afff0d0474c901ceb6a5ad324",
                message_id="om_x100b6da2dd08bca0c38b3a598ad7a9c",
                chat_id="oc_7d1fe6e9e0d8a92df66418f0f690a451",
                chat_type="p2p",
                message_type="post",
                sender_id="ou_test_user_001",
                content=content,
                raw=raw,
            )
        )

        slot = next(
            slot
            for slot in self.db.recent_lark_slots(limit=5)
            if slot.lark_event_id == "910d887afff0d0474c901ceb6a5ad324"
        )
        slot_raw = self.db.get_slot_raw(slot.slot_id)
        self.assertEqual(slot_raw["breakwater_lark_images"][0]["file_key"], image_key)
        self.assertEqual(slot_raw["breakwater_lark_images"][0]["source"], "current")
        self.assertEqual(slot_raw["breakwater_lark_images"][0]["message_id"], "om_x100b6da2dd08bca0c38b3a598ad7a9c")

        fake = CaseRecordingCodexClient(self.db, reply=True)
        service.codex_client = fake  # type: ignore[assignment]
        await service._run_codex_for_slot(slot)

        self.assertEqual(lark.image_downloads[0][0], "om_x100b6da2dd08bca0c38b3a598ad7a9c")
        self.assertEqual(lark.image_downloads[0][1], image_key)
        self.assertEqual(len(fake.input_attachments[0]), 1)
        self.assertTrue(fake.input_attachments[0][0].path.exists())
        self.assertIn("localImage", fake.incoming_texts[0])
        self.assertIn(image_key, fake.incoming_texts[0])

    async def test_direct_lark_messages_continue_user_case_and_codex_thread(self) -> None:
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FakeLarkReplyClient()  # type: ignore[assignment]
        fake = CaseRecordingCodexClient(self.db, reply=True)
        service.codex_client = fake  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-direct-turn-1",
                message_id="message-direct-turn-1",
                chat_id="direct-chat",
                chat_type="p2p",
                message_type="text",
                sender_id="ou_same_user",
                content="先分析这个非 Jira 问题",
                raw={"event_id": "event-direct-turn-1"},
            )
        )
        first_slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-direct-turn-1")
        await service._run_codex_for_slot(first_slot)

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-direct-turn-2",
                message_id="message-direct-turn-2",
                chat_id="direct-chat",
                chat_type="p2p",
                message_type="text",
                sender_id="ou_same_user",
                content="继续刚才那个问题，补充验证一下",
                raw={"event_id": "event-direct-turn-2"},
            )
        )
        second_slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-direct-turn-2")
        await service._run_codex_for_slot(second_slot)

        self.assertEqual(second_slot.case_id, first_slot.case_id)
        case = self.db.get_case(second_slot.case_id or "")
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(case.scope_type, "lark_chat")
        self.assertEqual(case.scope_key, "p2p:user:ou_same_user")
        self.assertEqual(case.latest_codex_thread_id, "thread-case-new")
        self.assertEqual(fake.prompt_names, ["lark_case_followup", "lark_case_followup"])
        self.assertEqual(fake.resume_thread_ids, [None, "thread-case-new"])

    async def test_direct_lark_case_and_context_are_isolated_by_sender(self) -> None:
        self.db.record_lark_message(
            message_id="message-direct-a-context",
            event_id="event-direct-a-context",
            chat_id="shared-direct",
            chat_type="p2p",
            chat_name=None,
            sender_id="ou_a",
            message_type="text",
            content="A 的私聊背景",
            mentioned_bot=True,
            raw={"event_id": "event-direct-a-context"},
        )
        self.db.record_lark_message(
            message_id="message-direct-b-context",
            event_id="event-direct-b-context",
            chat_id="shared-direct",
            chat_type="p2p",
            chat_name=None,
            sender_id="ou_b",
            message_type="text",
            content="B 的私聊背景不应给 A",
            mentioned_bot=True,
            raw={"event_id": "event-direct-b-context"},
        )
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FakeLarkReplyClient()  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-direct-a-current",
                message_id="message-direct-a-current",
                chat_id="shared-direct",
                chat_type="p2p",
                message_type="text",
                sender_id="ou_a",
                content="A 的当前问题",
                raw={"event_id": "event-direct-a-current"},
            )
        )
        await service._handle_lark_event(
            LarkEvent(
                event_id="event-direct-b-current",
                message_id="message-direct-b-current",
                chat_id="shared-direct",
                chat_type="p2p",
                message_type="text",
                sender_id="ou_b",
                content="B 的当前问题",
                raw={"event_id": "event-direct-b-current"},
            )
        )

        slot_a = next(slot for slot in self.db.recent_lark_slots(limit=10) if slot.lark_event_id == "event-direct-a-current")
        slot_b = next(slot for slot in self.db.recent_lark_slots(limit=10) if slot.lark_event_id == "event-direct-b-current")
        self.assertNotEqual(slot_a.case_id, slot_b.case_id)
        case_a = self.db.get_case(slot_a.case_id or "")
        case_b = self.db.get_case(slot_b.case_id or "")
        self.assertIsNotNone(case_a)
        self.assertIsNotNone(case_b)
        assert case_a is not None and case_b is not None
        self.assertEqual(case_a.scope_key, "p2p:user:ou_a")
        self.assertEqual(case_b.scope_key, "p2p:user:ou_b")
        self.assertIn("A 的私聊背景", slot_a.incoming_text)
        self.assertNotIn("B 的私聊背景不应给 A", slot_a.incoming_text)
        self.assertIn("B 的私聊背景不应给 A", slot_b.incoming_text)
        self.assertNotIn("A 的私聊背景", slot_b.incoming_text)

    async def test_lark_chat_case_adopts_prior_uncased_lark_slot_and_resumes_thread(self) -> None:
        previous = self.db.create_slot(
            source="lark",
            incoming_text="previous non-jira task",
            lark_event_id="event-prior-chat-task",
            lark_message_id="message-prior-chat-task",
            chat_id="chat-with-history",
            chat_type="group",
            sender_id="ou_user",
        )
        self.db.mark_codex_turn(previous.slot_id, "thread-prior-chat", "turn-prior-chat")
        self.db.mark_reply_sent(previous.slot_id, "message-prior-reply")
        self.db.record_lark_message(
            message_id="message-prior-chat-task",
            event_id="event-prior-chat-task",
            chat_id="chat-with-history",
            chat_type="group",
            chat_name="非 Jira 讨论群",
            sender_id="ou_user",
            message_type="text",
            content="上一条普通讨论",
            mentioned_bot=True,
            raw={"event_id": "event-prior-chat-task"},
            handled_slot_id=previous.slot_id,
        )
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FakeLarkReplyClient(
            chat_names={"chat-with-history": "非 Jira 讨论群"},
            mentioned_message_ids={"message-current-chat-task"},
        )  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-current-chat-task",
                message_id="message-current-chat-task",
                chat_id="chat-with-history",
                chat_type="group",
                message_type="text",
                sender_id="ou_user",
                content="@bot 继续刚才的问题，做一次实际验证",
                raw={"event_id": "event-current-chat-task"},
            )
        )

        slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-current-chat-task")
        adopted_previous = self.db.get_slot(previous.slot_id)
        self.assertIsNotNone(adopted_previous)
        assert adopted_previous is not None
        self.assertEqual(slot.source, "lark_case_followup")
        self.assertEqual(slot.case_id, adopted_previous.case_id)
        case = self.db.get_case(slot.case_id or "")
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(case.latest_codex_thread_id, "thread-prior-chat")
        self.assertEqual(case.scope_type, "lark_chat")
        self.assertEqual(case.scope_key, "group:chat-with-history")

        fake = CaseRecordingCodexClient(self.db, reply=True)
        service.codex_client = fake  # type: ignore[assignment]
        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.prompt_names, ["lark_case_followup"])
        self.assertEqual(fake.resume_thread_ids, ["thread-prior-chat"])
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.codex_thread_id, "thread-prior-chat")

    async def test_lark_chat_history_adoption_ignores_retry_only_thread(self) -> None:
        good = self.db.create_slot(
            source="lark",
            incoming_text="real analysis",
            lark_event_id="event-good-history",
            lark_message_id="message-good-history",
            chat_id="chat-retry-history",
            chat_type="group",
            sender_id="ou_user",
        )
        self.db.mark_codex_turn(good.slot_id, "thread-good-history", "turn-good-history")
        self.db.mark_reply_sent(good.slot_id, "message-good-reply")
        bad_retry = self.db.create_slot(
            source="lark",
            incoming_text="later task that only got retry fallback",
            lark_event_id="event-bad-retry-history",
            lark_message_id="message-bad-retry-history",
            chat_id="chat-retry-history",
            chat_type="group",
            sender_id="ou_user",
        )
        self.db.mark_codex_turn(bad_retry.slot_id, "thread-bad-retry-history", "turn-bad-retry-history")
        self.db.record_reply_request(bad_retry.slot_id, "上一轮任务已完成。")
        with self.db.connect() as conn:
            conn.execute("UPDATE slots SET codex_attempts=2 WHERE slot_id=?", (bad_retry.slot_id,))
        self.db.mark_reply_sent(bad_retry.slot_id, "message-bad-retry-reply")
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FakeLarkReplyClient(
            chat_names={"chat-retry-history": "历史 retry 群"},
            mentioned_message_ids={"message-after-bad-retry"},
        )  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-after-bad-retry",
                message_id="message-after-bad-retry",
                chat_id="chat-retry-history",
                chat_type="group",
                message_type="text",
                sender_id="ou_user",
                content="@bot 继续原来的分析",
                raw={"event_id": "event-after-bad-retry"},
            )
        )

        slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-after-bad-retry")
        self.assertEqual(slot.source, "lark_case_followup")
        self.assertEqual(self.db.get_slot(good.slot_id).case_id, slot.case_id)  # type: ignore[union-attr]
        self.assertEqual(self.db.get_slot(bad_retry.slot_id).case_id, slot.case_id)  # type: ignore[union-attr]
        case = self.db.get_case(slot.case_id or "")
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(case.latest_codex_thread_id, "thread-good-history")

        fake = CaseRecordingCodexClient(self.db, reply=True)
        service.codex_client = fake  # type: ignore[assignment]
        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.resume_thread_ids, ["thread-good-history"])

    def test_startup_backfills_historical_lark_slots_into_chat_case(self) -> None:
        previous = self.db.create_slot(
            source="lark",
            incoming_text="legacy non-jira lark task",
            lark_event_id="event-legacy-chat",
            lark_message_id="message-legacy-chat",
            chat_id="legacy-group",
            chat_type="group",
            sender_id="ou_user",
        )
        self.db.mark_codex_turn(previous.slot_id, "thread-legacy-chat", "turn-legacy-chat")
        self.db.mark_reply_sent(previous.slot_id, "message-legacy-reply")
        self.db.record_lark_message(
            message_id="message-legacy-chat",
            event_id="event-legacy-chat",
            chat_id="legacy-group",
            chat_type="group",
            chat_name="历史普通讨论群",
            sender_id="ou_user",
            message_type="text",
            content="历史普通讨论",
            mentioned_bot=True,
            raw={"event_id": "event-legacy-chat"},
            handled_slot_id=previous.slot_id,
        )
        service = BreakwaterService(self.config(concurrency=1))

        service._backfill_lark_chat_cases()

        refreshed = self.db.get_slot(previous.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertIsNotNone(refreshed.case_id)
        case = self.db.get_case(refreshed.case_id or "")
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual(case.scope_type, "lark_chat")
        self.assertEqual(case.scope_key, "group:legacy-group")
        self.assertEqual(case.bound_lark_chat_id, "legacy-group")
        self.assertEqual(case.bound_lark_chat_name, "历史普通讨论群")
        self.assertEqual(case.latest_codex_thread_id, "thread-legacy-chat")
        self.assertEqual(self.db.find_case_by_alias("lark_chat_id", "legacy-group"), case)
        self.assertEqual(self.db.find_case_by_alias("lark_message_id", "message-legacy-chat"), case)
        self.assertEqual(len(self.db.list_case_slots(case.case_id)), 1)

        service._backfill_lark_chat_cases()

        self.assertEqual(len(self.db.list_case_slots(case.case_id)), 1)

    async def test_resume_failure_recreates_thread_with_case_context(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-87", title="OPS-87")
        self.db.update_case_summary(case.case_id, "prior analysis summary")
        seed = self.db.create_slot(source="jira_analyze", incoming_text="/analyze", case_id=case.case_id)
        self.db.mark_codex_turn(seed.slot_id, "thread-missing", "turn-seed")
        slot = self.db.create_slot(source="lark_case_followup", incoming_text="continue after cleanup", case_id=case.case_id, delivery_target="lark_reply")
        refreshed_case = self.db.get_case(case.case_id)
        assert refreshed_case is not None
        service = BreakwaterService(self.config(concurrency=1, retries=0))
        fake = ResumeFailsOnceCodexClient(self.db)
        service.codex_client = fake  # type: ignore[assignment]

        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.resume_thread_ids, ["thread-missing", None])
        self.assertEqual(fake.prompt_names, ["lark_case_followup", "lark_case_followup"])
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.codex_thread_id, "thread-recreated")
        self.assertEqual(refreshed.codex_status, "completed")
        event_types = [event["event_type"] for event in self.db.recent_events(limit=20)]
        self.assertIn("thread.recreate_after_resume_failed", event_types)

    async def test_context_window_failure_compacts_thread_then_retries_same_slot(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-88", title="OPS-88")
        self.db.update_case_summary(case.case_id, "prior analysis summary")
        seed = self.db.create_slot(source="jira_analyze", incoming_text="/analyze", case_id=case.case_id)
        self.db.mark_codex_turn(seed.slot_id, "thread-full", "turn-seed")
        slot = self.db.create_slot(source="lark_case_followup", incoming_text="continue after context overflow", case_id=case.case_id, delivery_target="lark_reply")
        service = BreakwaterService(self.config(concurrency=1, retries=0))
        fake = ContextWindowThenCompactSuccessCodexClient(self.db)
        service.codex_client = fake  # type: ignore[assignment]

        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.resume_thread_ids, ["thread-full", "thread-full"])
        self.assertEqual(fake.compact_thread_ids, ["thread-full"])
        self.assertEqual(fake.retry_prompts, [None, None])
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.codex_thread_id, "thread-full")
        self.assertEqual(refreshed.codex_status, "completed")
        event_types = [event["event_type"] for event in self.db.recent_events(limit=20)]
        self.assertIn("turn.context_window_exceeded", event_types)
        self.assertIn("thread.compact_after_context_window_exceeded", event_types)
        self.assertIn("thread.compact_completed", event_types)
        self.assertNotIn("thread.recreate_after_context_window_exceeded", event_types)

    async def test_lark_reply_parent_message_can_locate_case_without_text_reference(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-90", title="OPS-90")
        seed = self.db.create_slot(
            source="lark_case_followup",
            incoming_text="previous reply",
            case_id=case.case_id,
            lark_message_id="om_original",
        )
        self.db.mark_reply_sent(seed.slot_id, "om_breakwater_reply")
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FakeLarkReplyClient(mentioned_message_ids={"message-parent-followup"})  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-parent-followup",
                message_id="message-parent-followup",
                chat_id="chat",
                chat_type="group",
                message_type="text",
                sender_id="ou_user",
                content="@bot 这条继续解释一下",
                raw={"event": {"message": {"parent_id": "om_breakwater_reply"}}},
            )
        )

        slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-parent-followup")
        self.assertEqual(slot.source, "lark_case_followup")
        self.assertEqual(slot.case_id, case.case_id)

    async def test_lark_ambiguous_case_reference_asks_for_selection_without_codex(self) -> None:
        self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-91", title="OPS-91")
        self.db.ensure_case(scope_type="jira_issue", scope_key="APP-91", title="APP-91")
        service = BreakwaterService(self.config(concurrency=1))
        fake_lark = FakeLarkReplyClient(mentioned_message_ids={"message-ambiguous-case"})
        service.lark_client = fake_lark  # type: ignore[assignment]

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-ambiguous-case",
                message_id="message-ambiguous-case",
                chat_id="chat",
                chat_type="group",
                message_type="text",
                sender_id="ou_user",
                content="@bot 比较 OPS-91 和 APP-91 后继续分析",
                raw={"event_id": "event-ambiguous-case"},
            )
        )

        self.assertEqual(service._codex_queue.qsize(), 0)
        slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-ambiguous-case")
        self.assertEqual(slot.source, "lark_case_resolution")
        self.assertEqual(slot.codex_status, "skipped")
        pending = self.db.pending_replies()
        self.assertEqual([reply.slot_id for reply in pending], [slot.slot_id])
        await service._send_pending_replies_once()
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.codex_status, "skipped")
        self.assertEqual(refreshed.status, "replied")
        self.assertEqual(len(fake_lark.replies), 1)
        self.assertIn("多个可能的 case", fake_lark.replies[0][1])
        self.assertIn("OPS-91", fake_lark.replies[0][1])
        self.assertIn("APP-91", fake_lark.replies[0][1])

    async def test_lark_case_followup_runs_through_queue_and_sends_reply(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="OPS-89", title="OPS-89")
        seed = self.db.create_slot(
            source="jira_analyze",
            incoming_text="/analyze",
            case_id=case.case_id,
            delivery_target="jira_comment",
            jira_issue_key="OPS-89",
        )
        self.db.mark_codex_turn(seed.slot_id, "thread-ops-89", "turn-seed")
        service = BreakwaterService(self.config(concurrency=1))
        service.codex_server = NoopCodexServer()  # type: ignore[assignment]
        service.codex_client = CaseRecordingCodexClient(self.db, reply=True)  # type: ignore[assignment]
        fake_lark = FakeLarkReplyClient(mentioned_message_ids={"message-case-followup-e2e"})
        service.lark_client = fake_lark  # type: ignore[assignment]
        service._start_codex_workers()

        await service._handle_lark_event(
            LarkEvent(
                event_id="event-case-followup-e2e",
                message_id="message-case-followup-e2e",
                chat_id="chat",
                chat_type="group",
                message_type="text",
                sender_id="ou_user",
                content="@bot 继续 OPS-89，给我一个更短的结论",
                raw={"event_id": "event-case-followup-e2e"},
            )
        )

        slot = next(slot for slot in self.db.recent_lark_slots(limit=5) if slot.lark_event_id == "event-case-followup-e2e")
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            refreshed = self.db.get_slot(slot.slot_id)
            if refreshed and refreshed.codex_status == "completed" and refreshed.reply_status == "pending":
                break
            await asyncio.sleep(0.02)
        await service._send_pending_replies_once()
        await service.stop()

        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.source, "lark_case_followup")
        self.assertEqual(refreshed.case_id, case.case_id)
        self.assertEqual(refreshed.codex_thread_id, "thread-ops-89")
        self.assertEqual(refreshed.reply_status, "sent")
        self.assertEqual(
            fake_lark.replies,
            [
                (
                    "message-case-followup-e2e",
                    f'<at user_id="ou_user">用户</at> case followup reply\n\nCase: OPS-89 · {slot.slot_id} · session thread-ops-89',
                )
            ],
        )

    async def test_jira_analysis_missing_marker_retries_until_comment_is_verified(self) -> None:
        self.db.insert_jira_comment(
            comment_id="comment-2",
            issue_key="APP-43",
            project_key="APP",
            author="Bob",
            body="/analyze explain this",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"id": "comment-2"},
        )
        slot = self.db.create_jira_analyze_slot_for_comment("comment-2")
        assert slot is not None
        service = BreakwaterService(self.config(concurrency=1, retries=1))
        marker = service._jira_analysis_marker(slot.slot_id)
        fake = NoReplyCodexClient()
        fake_jira = FakeJiraVerifier(
            [
                [{"id": "old", "body": "no marker"}],
                [{"id": "old-again", "body": "still no marker"}],
                [{"id": "reply-2", "body": f"analysis\n{marker}"}],
            ]
        )
        service.codex_client = fake  # type: ignore[assignment]
        service.jira_client = fake_jira  # type: ignore[assignment]

        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.calls, 2)
        self.assertIsNone(fake.retry_prompts[0])
        self.assertIn(marker, fake.retry_prompts[1] or "")
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.reply_status, "sent")
        self.assertEqual(refreshed.status, "replied")
        event_types = [event["event_type"] for event in self.db.recent_events(limit=10)]
        self.assertIn("analysis.comment_missing", event_types)
        self.assertIn("analysis.comment_verified", event_types)

    async def test_jira_analysis_codex_exception_verifies_existing_marker_without_retry(self) -> None:
        self.db.insert_jira_comment(
            comment_id="comment-exception",
            issue_key="APP-44",
            project_key="APP",
            author="Bob",
            body="/analyze explain this",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"id": "comment-exception"},
        )
        slot = self.db.create_jira_analyze_slot_for_comment("comment-exception")
        assert slot is not None
        service = BreakwaterService(self.config(concurrency=1, retries=2))
        marker = service._jira_analysis_marker(slot.slot_id)
        fake = FailingCodexClient(RuntimeError("no close frame received or sent"))
        fake_jira = FakeJiraVerifier([[{"id": "reply-after-error", "body": f"analysis\n{marker}"}]])
        service.codex_client = fake  # type: ignore[assignment]
        service.jira_client = fake_jira  # type: ignore[assignment]

        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.calls, 1)
        self.assertEqual(fake_jira.calls, ["APP-44"])
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "replied")
        self.assertEqual(refreshed.codex_status, "completed")
        self.assertEqual(refreshed.reply_status, "sent")
        with self.db.connect() as conn:
            row = conn.execute("SELECT lark_reply_message_id, error FROM slots WHERE slot_id=?", (slot.slot_id,)).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["lark_reply_message_id"], "reply-after-error")
        self.assertIsNone(row["error"])
        event_types = [event["event_type"] for event in self.db.recent_events(limit=10)]
        self.assertIn("analysis.comment_verified", event_types)
        self.assertIn("turn.error_after_jira_marker_verified", event_types)
        self.assertNotIn("turn.failed", event_types)

    async def test_jira_analysis_transport_error_waits_for_running_turn_marker_without_retry(self) -> None:
        self.db.insert_jira_comment(
            comment_id="comment-running-marker",
            issue_key="APP-46",
            project_key="APP",
            author="Bob",
            body="/analyze explain this",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"id": "comment-running-marker"},
        )
        slot = self.db.create_jira_analyze_slot_for_comment("comment-running-marker")
        assert slot is not None
        service = BreakwaterService(
            AppConfig(
                db_path=self.db_path,
                codex=CodexConfig(start_server=False, max_reply_retries=2, turn_timeout_seconds=1),
                reply_poll_interval=0.01,
                codex_concurrency=1,
                web_enabled=False,
            )
        )
        marker = service._jira_analysis_marker(slot.slot_id)
        fake = RunningAfterTransportErrorCodexClient()
        fake_jira = FakeJiraVerifier(
            [
                [{"id": "old-1", "body": "no marker"}],
                [{"id": "old-2", "body": "still no marker"}],
                [{"id": "reply-after-background-turn", "body": f"analysis\n{marker}"}],
            ]
        )
        service.codex_client = fake  # type: ignore[assignment]
        service.jira_client = fake_jira  # type: ignore[assignment]

        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.calls, 1)
        self.assertEqual(fake.read_turn_calls, [("thread-failed", "turn-1")])
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "replied")
        self.assertEqual(refreshed.codex_status, "completed")
        self.assertEqual(refreshed.reply_status, "sent")
        with self.db.connect() as conn:
            row = conn.execute("SELECT lark_reply_message_id, error FROM slots WHERE slot_id=?", (slot.slot_id,)).fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["lark_reply_message_id"], "reply-after-background-turn")
        self.assertIsNone(row["error"])
        event_types = [event["event_type"] for event in self.db.recent_events(limit=20)]
        self.assertIn("turn.transport_error_still_running", event_types)
        self.assertIn("analysis.comment_verified", event_types)
        self.assertNotIn("turn.failed", event_types)

    async def test_jira_analysis_transport_error_retries_after_terminal_turn_without_marker(self) -> None:
        self.db.insert_jira_comment(
            comment_id="comment-terminal-retry",
            issue_key="APP-47",
            project_key="APP",
            author="Bob",
            body="/analyze explain this",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"id": "comment-terminal-retry"},
        )
        slot = self.db.create_jira_analyze_slot_for_comment("comment-terminal-retry")
        assert slot is not None
        service = BreakwaterService(self.config(concurrency=1, retries=1))
        marker = service._jira_analysis_marker(slot.slot_id)
        fake = TerminalAfterTransportErrorCodexClient(self.db)
        fake_jira = FakeJiraVerifier(
            [
                [{"id": "old-1", "body": "no marker"}],
                [{"id": "old-2", "body": "still no marker"}],
                [{"id": "old-3", "body": "still no marker"}],
                [{"id": "reply-after-retry", "body": f"analysis\n{marker}"}],
            ]
        )
        service.codex_client = fake  # type: ignore[assignment]
        service.jira_client = fake_jira  # type: ignore[assignment]

        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.calls, 2)
        self.assertEqual(fake.read_turn_calls, [("thread-transport", "turn-transport")])
        self.assertIsNone(fake.retry_prompts[0])
        self.assertIn(marker, fake.retry_prompts[1] or "")
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "replied")
        self.assertEqual(refreshed.codex_status, "completed")
        self.assertEqual(refreshed.reply_status, "sent")
        event_types = [event["event_type"] for event in self.db.recent_events(limit=20)]
        self.assertIn("turn.transport_error_terminal", event_types)
        self.assertIn("analysis.comment_verified", event_types)

    async def test_jira_analysis_codex_exception_logs_missing_marker_check(self) -> None:
        self.db.insert_jira_comment(
            comment_id="comment-exception-missing",
            issue_key="APP-45",
            project_key="APP",
            author="Bob",
            body="/analyze explain this",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"id": "comment-exception-missing"},
        )
        slot = self.db.create_jira_analyze_slot_for_comment("comment-exception-missing")
        assert slot is not None
        service = BreakwaterService(self.config(concurrency=1, retries=0))
        fake = FailingCodexClient(RuntimeError("websocket dropped"))
        fake_jira = FakeJiraVerifier([[{"id": "old", "body": "no marker"}]])
        service.codex_client = fake  # type: ignore[assignment]
        service.jira_client = fake_jira  # type: ignore[assignment]

        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.calls, 1)
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "failed")
        event_types = [event["event_type"] for event in self.db.recent_events(limit=10)]
        self.assertIn("analysis.marker_check_started", event_types)
        self.assertIn("analysis.marker_not_found", event_types)
        self.assertIn("turn.failed", event_types)

    async def test_lark_jira_analysis_retries_until_jira_marker_and_lark_reply_exist(self) -> None:
        case = self.db.ensure_case(scope_type="jira_issue", scope_key="APP-26001", title="APP-26001")
        slot = self.db.create_slot(
            source="lark_jira_analyze",
            incoming_text="analyze from group",
            lark_event_id="event-dual-delivery",
            lark_message_id="message-dual-delivery",
            chat_id="group",
            chat_type="group",
            sender_id="ou_user",
            case_id=case.case_id,
            jira_issue_key="APP-26001",
            delivery_target="jira_comment_and_lark_reply",
        )
        service = BreakwaterService(self.config(concurrency=1, retries=1))
        marker = service._jira_analysis_marker(slot.slot_id)
        fake = CaseRecordingCodexClient(self.db, reply=True)
        fake_jira = FakeJiraVerifier(
            [
                [{"id": "old", "body": "no marker"}],
                [{"id": "jira-dual", "body": f"analysis\n{marker}"}],
            ]
        )
        service.codex_client = fake  # type: ignore[assignment]
        service.jira_client = fake_jira  # type: ignore[assignment]

        await service._run_codex_for_slot(slot)

        self.assertEqual(fake.prompt_names, ["lark_jira_analyze", "lark_jira_analyze"])
        self.assertIsNone(fake.retry_prompts[0])
        self.assertIn(marker, fake.retry_prompts[1] or "")
        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.codex_status, "completed")
        self.assertEqual(refreshed.reply_status, "pending")
        event_types = [event["event_type"] for event in self.db.recent_events(limit=20)]
        self.assertIn("lark_analysis.delivery_missing", event_types)
        self.assertIn("lark_analysis.dual_delivery_verified", event_types)

    async def test_jira_analyze_pending_breakwater_reply_is_not_posted_by_main_service(self) -> None:
        self.db.insert_jira_comment(
            comment_id="comment-3",
            issue_key="APP-44",
            project_key="APP",
            author="Carol",
            body="/analyze explain this",
            jira_created_at="2026-05-21T17:31:00.000+0800",
            jira_updated_at="2026-05-21T17:31:00.000+0800",
            raw={"id": "comment-3"},
        )
        slot = self.db.create_jira_analyze_slot_for_comment("comment-3")
        assert slot is not None
        self.db.record_reply_request(slot.slot_id, "wrong delivery path")
        service = BreakwaterService(self.config(concurrency=1))

        await service._send_pending_replies_once()

        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.reply_status, "pending")
        event_types = [event["event_type"] for event in self.db.recent_events(limit=10)]
        self.assertIn("analysis.breakwater_reply_ignored", event_types)

    async def test_withdrawn_lark_message_reply_is_marked_failed_without_retry(self) -> None:
        slot = self.db.create_slot(
            source="lark_initial",
            incoming_text="follow up",
            lark_message_id="message-withdrawn",
            chat_id="chat",
            chat_type="group",
            sender_id="ou_user",
            delivery_target="lark_reply",
        )
        self.db.record_reply_request(slot.slot_id, "reply text")
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FailingLarkReplyClient(
            "lark reply failed code=230011 msg=The message was withdrawn. request_id="
        )  # type: ignore[assignment]

        await service._send_pending_replies_once()

        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "reply_failed")
        self.assertEqual(refreshed.reply_status, "failed")
        with self.db.connect() as conn:
            error = conn.execute("SELECT error FROM slots WHERE slot_id=?", (slot.slot_id,)).fetchone()["error"]
        self.assertIn("code=230011", error)
        self.assertNotIn(slot.slot_id, {reply.slot_id for reply in self.db.pending_replies()})
        event_types = [event["event_type"] for event in self.db.recent_events(limit=10)]
        self.assertIn("message.reply_permanent_failed", event_types)

    async def test_transient_lark_reply_error_remains_pending_for_retry(self) -> None:
        slot = self.db.create_slot(
            source="lark_initial",
            incoming_text="follow up",
            lark_message_id="message-transient",
            chat_id="chat",
            chat_type="group",
            sender_id="ou_user",
            delivery_target="lark_reply",
        )
        self.db.record_reply_request(slot.slot_id, "reply text")
        service = BreakwaterService(self.config(concurrency=1))
        service.lark_client = FailingLarkReplyClient("temporary network failure")  # type: ignore[assignment]

        await service._send_pending_replies_once()

        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "reply_pending")
        self.assertEqual(refreshed.reply_status, "pending")
        self.assertIn(slot.slot_id, {reply.slot_id for reply in self.db.pending_replies()})
        event_types = [event["event_type"] for event in self.db.recent_events(limit=10)]
        self.assertIn("message.reply_failed", event_types)
        self.assertNotIn("message.reply_permanent_failed", event_types)

    async def test_github_issue_pending_breakwater_reply_is_not_marked_by_main_service(self) -> None:
        self.db.insert_github_issue(
            repo_full_name="apache/doris",
            issue_number=55,
            node_id="I_55",
            title="wrong route",
            body="body",
            author="alice",
            state="open",
            html_url="https://github.com/apache/doris/issues/55",
            labels=[],
            github_created_at="2026-05-21T09:31:00Z",
            github_updated_at="2026-05-21T09:31:00Z",
            raw={"number": 55},
        )
        slot = self.db.create_github_issue_slot_for_issue("apache/doris", 55)
        assert slot is not None
        self.db.record_reply_request(slot.slot_id, "wrong delivery path")
        service = BreakwaterService(self.config(concurrency=1))

        await service._send_pending_replies_once()

        refreshed = self.db.get_slot(slot.slot_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed.reply_status, "pending")
        event_types = [event["event_type"] for event in self.db.recent_events(limit=10)]
        self.assertIn("analysis.breakwater_reply_ignored", event_types)

    def test_prompt_library_renders_jira_prompt_from_shared_file(self) -> None:
        prompt = PromptLibrary().render(
            "jira_analyze",
            slot_id="slot_test",
            breakwater_db=self.db_path,
            jira_issue_key="APP-44",
            jira_comment_id="comment-3",
            jira_issue_command="uv run --project /skill python3 /skill/scripts/jira_search_issue.py --jql 'issuekey = APP-44'",
            jira_comment_file=".breakwater/jira-analysis-slot_test.md",
            jira_comment_command="uv run --project /skill python3 /skill/scripts/jira_comment_issue.py --issue-key APP-44 --comment-file .breakwater/jira-analysis-slot_test.md",
            jira_skill_status="Jira skill path: /skill/SKILL.md",
            develop_skill_status="Develop skill path: /devskill/SKILL.md",
            jira_marker="Breakwater-Analysis-Slot: slot_test",
            jira_comment_body="/analyze please",
        )

        self.assertIn("APP-44", prompt)
        self.assertIn("slot_test", prompt)
        self.assertIn("Breakwater-Analysis-Slot: slot_test", prompt)
        self.assertIn("Jira skill path: /skill/SKILL.md", prompt)
        self.assertIn("jira-issue skill", prompt)

    def test_prompt_library_renders_jira_auto_issue_prompt_from_shared_file(self) -> None:
        prompt = PromptLibrary().render(
            "jira_issue_auto_analyze",
            slot_id="slot_auto",
            incoming_text="SHOULD_NOT_APPEAR",
            jira_issue_key="OPS-200",
            jira_marker="Breakwater-Analysis-Slot: slot_auto",
            jira_skill_status="Jira skill path: /skill/SKILL.md",
            develop_skill_status="Develop skill path: /devskill/SKILL.md",
            automation_report_file=".breakwater/jira-automation-slot_auto.json",
            automation_report_command="BREAKWATER_DB=/tmp/db uv run breakwater automation-report slot_auto --json-file .breakwater/jira-automation-slot_auto.json",
        )

        self.assertIn("新建了一条 Jira issue", prompt)
        self.assertIn("必须先通过 jira-issue skill 读取", prompt)
        self.assertIn("不要依赖 Breakwater 传入的摘要或缓存内容", prompt)
        self.assertIn("OPS-200", prompt)
        self.assertIn("Breakwater-Analysis-Slot: slot_auto", prompt)
        self.assertIn("missing_required_material", prompt)
        self.assertIn("automation-report slot_auto", prompt)
        self.assertNotIn("SHOULD_NOT_APPEAR", prompt)

    def test_prompt_library_renders_jira_status_summary_prompt_from_shared_file(self) -> None:
        prompt = PromptLibrary().render(
            "jira_status_summary",
            slot_id="slot_status",
            incoming_text="SHOULD_NOT_APPEAR",
            jira_issue_key="OPS-300",
            jira_marker="Breakwater-Analysis-Slot: slot_status",
            jira_skill_status="Jira skill path: /skill/SKILL.md",
            develop_skill_status="Develop skill path: /devskill/SKILL.md",
            automation_report_file=".breakwater/jira-automation-slot_status.json",
            automation_report_command="BREAKWATER_DB=/tmp/db uv run breakwater automation-report slot_status --json-file .breakwater/jira-automation-slot_status.json",
        )

        self.assertIn("状态变化到了配置的收口状态", prompt)
        self.assertIn("简要总结评论", prompt)
        self.assertIn("必须通过 jira-issue skill 重新读取 Jira issue 的当前内容", prompt)
        self.assertIn("has_clear_resolution", prompt)
        self.assertIn("automation-report slot_status", prompt)
        self.assertNotIn("SHOULD_NOT_APPEAR", prompt)
        self.assertIn("Breakwater-Analysis-Slot: slot_status", prompt)

    def test_prompt_library_renders_github_issue_prompt_from_shared_file(self) -> None:
        prompt = PromptLibrary().render(
            "github_issue_analyze",
            slot_id="slot_test",
            case_id="case_github",
            case_scope_type="github_issue",
            case_scope_key="apache/doris#42",
            github_repo="apache/doris",
            github_issue_number="42",
            github_marker="Breakwater-GitHub-Analysis-Slot: slot_test",
            github_comment_file=".breakwater/github-analysis-slot_test.md",
            github_comment_command="BREAKWATER_DB=/tmp/db uv run breakwater github-comment slot_test --body-file .breakwater/github-analysis-slot_test.md",
            github_issue_body="GitHub issue body",
            develop_skill_status="Develop skill path: /devskill/SKILL.md",
        )

        self.assertIn("apache/doris#42", prompt)
        self.assertIn("Breakwater-GitHub-Analysis-Slot: slot_test", prompt)
        self.assertIn("breakwater github-comment", prompt)
        self.assertIn("GitHub issue body", prompt)

    def test_codex_prompt_values_include_configured_github_api_url_in_comment_command(self) -> None:
        client = CodexAppServerClient(
            CodexConfig(start_server=False),
            project_root=Path(self.tempdir.name),
            workspace=Path(self.tempdir.name),
            db_path=self.db_path,
            jira_skill_dir=Path(self.tempdir.name) / "jira",
            develop_skill_dir=Path(self.tempdir.name) / "develop",
            github_api_url="https://github.example.com/api/v3",
        )

        values = client._prompt_values(
            "slot_github",
            incoming_text="body",
            github_repo="owner/repo",
            github_issue_number="1",
        )

        self.assertIn("--api-url https://github.example.com/api/v3", str(values["github_comment_command"]))

    def test_codex_prompt_values_use_absolute_automation_report_command(self) -> None:
        project_root = Path(self.tempdir.name) / "Breakwater Root"
        client = CodexAppServerClient(
            CodexConfig(start_server=False),
            project_root=project_root,
            workspace=Path(self.tempdir.name),
            db_path=self.db_path,
            jira_skill_dir=Path(self.tempdir.name) / "jira",
            develop_skill_dir=Path(self.tempdir.name) / "develop",
        )

        values = client._prompt_values("slot_auto", incoming_text="")

        expected_report_file = project_root / ".breakwater" / "jira-automation-slot_auto.json"
        command = str(values["automation_report_command"])
        self.assertEqual(values["automation_report_file"], str(expected_report_file))
        self.assertIn(f"uv run --project {shlex.quote(str(project_root))} breakwater automation-report slot_auto", command)
        self.assertIn(f"--json-file {shlex.quote(str(expected_report_file))}", command)
        self.assertNotIn("--json-file .breakwater/", command)

    def test_lark_prompt_includes_configurable_develop_skill_and_prompt_variables(self) -> None:
        jira_skill_dir = Path(self.tempdir.name) / "skills" / "jira-issue"
        develop_skill_dir = Path(self.tempdir.name) / "skills" / "breakwater-develop"
        jira_skill_dir.mkdir(parents=True)
        develop_skill_dir.mkdir(parents=True)
        (jira_skill_dir / "SKILL.md").write_text("jira", encoding="utf-8")
        (develop_skill_dir / "SKILL.md").write_text("develop", encoding="utf-8")
        client = CodexAppServerClient(
            CodexConfig(start_server=False),
            project_root=Path(self.tempdir.name),
            workspace=Path(self.tempdir.name),
            db_path=self.db_path,
            jira_skill_dir=jira_skill_dir,
            develop_skill_dir=develop_skill_dir,
            prompt_variables={"DEVELOP_DEFAULT_REPOSITORY": "/repo/from-config"},
        )

        prompt = client._initial_prompt("slot_dev", "develop this feature", "lark_initial", {})
        items = client._input_items(prompt, "lark_initial")

        self.assertIn(f"Develop skill path: {develop_skill_dir / 'SKILL.md'}", prompt)
        self.assertIn("Default develop repository: /repo/from-config", prompt)
        self.assertIn("直接 coding", prompt)
        self.assertIn("breakwater-develop", {item.get("name") for item in items})

    def test_codex_input_items_include_local_images_before_text(self) -> None:
        client = CodexAppServerClient(
            CodexConfig(start_server=False),
            project_root=Path(self.tempdir.name),
            workspace=Path(self.tempdir.name),
            db_path=self.db_path,
            jira_skill_dir=Path(self.tempdir.name) / "jira",
            develop_skill_dir=Path(self.tempdir.name) / "develop",
        )
        image_path = Path(self.tempdir.name) / "image.png"
        image_path.write_bytes(TEST_PNG_BYTES)

        items = client._input_items(
            "hello",
            "lark_initial",
            input_attachments=[CodexInputAttachment(path=image_path, detail="original")],
        )

        text_index = next(index for index, item in enumerate(items) if item["type"] == "text")
        image_item = next(item for item in items if item["type"] == "localImage")
        self.assertLess(items.index(image_item), text_index)
        self.assertEqual(image_item["path"], str(image_path))
        self.assertEqual(image_item["detail"], "original")

    def test_lark_case_prompt_requires_jira_sync_for_new_findings(self) -> None:
        common = {
            "slot_id": "slot_case",
            "breakwater_db": self.db_path,
            "case_id": "case_jira_issue_ops_1",
            "case_scope_type": "jira_issue",
            "case_scope_key": "OPS-1",
            "jira_issue_key": "OPS-1",
            "codex_thread_id": "thread-1",
            "delivery_target": "lark_reply",
            "jira_skill_status": "Jira skill path: /skill/SKILL.md",
            "develop_skill_status": "Develop skill path: /devskill/SKILL.md",
            "reply_command": "breakwater reply slot_case --message ...",
            "reply_file_command": "breakwater reply slot_case --message-file file",
            "incoming_text": "继续分析",
            "case_summary": "prior summary",
        }

        followup = PromptLibrary().render("lark_case_followup", **common)

        self.assertIn("Jira issue for progress comments: OPS-1", followup)
        self.assertIn("新的事实、证据、判断、修复进展或规避建议", followup)
        self.assertIn("jira-issue skill", followup)
        self.assertIn("不能替代 Jira 评论", followup)
        self.assertIn("如果只是解释既有结论", followup)

    def test_lark_jira_analysis_prompt_requires_dual_delivery(self) -> None:
        common = {
            "slot_id": "slot_lark_jira",
            "breakwater_db": self.db_path,
            "case_id": "case_jira_issue_app_26000",
            "case_scope_type": "jira_issue",
            "case_scope_key": "APP-26000",
            "jira_issue_key": "APP-26000",
            "lark_chat_id": "oc_group",
            "lark_message_id": "om_message",
            "jira_marker": "Breakwater-Analysis-Slot: slot_lark_jira",
            "jira_skill_status": "Jira skill path: /skill/SKILL.md",
            "develop_skill_status": "Develop skill path: /devskill/SKILL.md",
            "reply_command": "breakwater reply slot_lark_jira --message ...",
            "reply_file_command": "breakwater reply slot_lark_jira --message-file file",
            "incoming_text": "请分析这个 Jira",
        }

        prompt = PromptLibrary().render("lark_jira_analyze", **common)
        retry = PromptLibrary().render("lark_jira_analyze_retry", **common)

        self.assertIn("同时完成两个出口", prompt)
        self.assertIn("Breakwater-Analysis-Slot: slot_lark_jira", prompt)
        self.assertIn("jira-issue skill", prompt)
        self.assertIn("必须通过 Breakwater reply 回复飞书群", prompt)
        self.assertIn("APP-26000", retry)
        self.assertIn("完整双出口", retry)

    async def test_codex_app_server_captures_turn_error_notification(self) -> None:
        class FakeWebSocket:
            def __init__(self, messages: list[dict[str, object]]):
                self.messages = [json.dumps(message) for message in messages]

            async def recv(self) -> str:
                if not self.messages:
                    raise AssertionError("unexpected websocket recv")
                return self.messages.pop(0)

        client = CodexAppServerClient(
            CodexConfig(start_server=False),
            project_root=Path(self.tempdir.name),
            workspace=Path(self.tempdir.name),
            db_path=self.db_path,
            jira_skill_dir=Path(self.tempdir.name) / "jira",
            develop_skill_dir=Path(self.tempdir.name) / "develop",
        )
        turn_errors: dict[str, dict[str, object]] = {}
        answer_parts: list[str] = []

        completed = await client._read_until_completed(
            FakeWebSocket(
                [
                    {
                        "method": "error",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "error": {
                                "message": "Codex ran out of room in the model's context window. Start a new thread or clear earlier history before retrying.",
                                "codexErrorInfo": "contextWindowExceeded",
                            },
                            "willRetry": False,
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-1", "status": "failed", "error": None, "durationMs": 1},
                        },
                    },
                ]
            ),
            "thread-1",
            "turn-1",
            answer_parts,
            turn_errors,
        )

        error = client._turn_error(completed["turn"], turn_errors.get("turn-1"))
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error["codexErrorInfo"], "contextWindowExceeded")
        self.assertIn("context window", str(error["message"]))

    async def test_codex_app_server_reads_context_compaction_completion(self) -> None:
        class FakeWebSocket:
            def __init__(self, messages: list[dict[str, object]]):
                self.messages = [json.dumps(message) for message in messages]

            async def recv(self) -> str:
                if not self.messages:
                    raise AssertionError("unexpected websocket recv")
                return self.messages.pop(0)

        client = CodexAppServerClient(
            CodexConfig(start_server=False),
            project_root=Path(self.tempdir.name),
            workspace=Path(self.tempdir.name),
            db_path=self.db_path,
            jira_skill_dir=Path(self.tempdir.name) / "jira",
            develop_skill_dir=Path(self.tempdir.name) / "develop",
        )
        completed = await client._read_until_compaction_completed(
            FakeWebSocket(
                [
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-compact",
                            "item": {"type": "contextCompaction"},
                        },
                    },
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-compact",
                            "item": {"type": "contextCompaction"},
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-compact", "status": "completed", "durationMs": 9},
                        },
                    },
                ]
            ),
            "thread-1",
            {},
        )

        self.assertEqual(completed["turn"]["id"], "turn-compact")
        self.assertEqual(completed["turn"]["status"], "completed")

    def test_codex_app_server_uses_configured_workspace_for_thread_and_turn_cwd(self) -> None:
        breakwater_root = Path(self.tempdir.name) / "breakwater"
        workspace = Path(self.tempdir.name) / "codex-workspace"
        jira_skill_dir = breakwater_root / "skills" / "jira-issue"
        develop_skill_dir = breakwater_root / "skills" / "breakwater-develop"
        jira_skill_dir.mkdir(parents=True)
        develop_skill_dir.mkdir(parents=True)
        (breakwater_root / "skills" / "breakwater-reply").mkdir(parents=True)
        (jira_skill_dir / "SKILL.md").write_text("jira", encoding="utf-8")
        (develop_skill_dir / "SKILL.md").write_text("develop", encoding="utf-8")
        (breakwater_root / "skills" / "breakwater-reply" / "SKILL.md").write_text("reply", encoding="utf-8")
        client = CodexAppServerClient(
            CodexConfig(start_server=False, model="gpt-5.5", effort="xhigh"),
            project_root=breakwater_root,
            workspace=workspace,
            db_path=self.db_path,
            jira_skill_dir=jira_skill_dir,
            develop_skill_dir=develop_skill_dir,
            task_env={"JIRA_TOKEN": "task-token"},
        )

        thread_params = client._thread_start_params()
        resume_params = client._thread_resume_params("thread-existing")
        turn_params = client._turn_start_params("thread-1", [{"type": "text", "text": "hello"}])
        input_items = client._input_items("hello", "lark_initial")
        jira_auto_items = client._input_items("hello", "jira_issue_auto_analyze")
        jira_status_items = client._input_items("hello", "jira_status_summary")

        self.assertEqual(thread_params["cwd"], str(workspace))
        self.assertEqual(turn_params["cwd"], str(workspace))
        self.assertEqual(thread_params["model"], "gpt-5.5")
        self.assertEqual(thread_params["serviceName"], "breakwater")
        self.assertEqual(resume_params["threadId"], "thread-existing")
        self.assertEqual(resume_params["config"]["shell_environment_policy"]["set"]["JIRA_TOKEN"], "task-token")
        self.assertEqual(turn_params["model"], "gpt-5.5")
        self.assertEqual(turn_params["effort"], "xhigh")
        self.assertEqual(
            thread_params["config"]["shell_environment_policy"]["set"]["JIRA_TOKEN"],
            "task-token",
        )
        self.assertIn(
            str(breakwater_root / "skills" / "breakwater-reply" / "SKILL.md"),
            {item.get("path") for item in input_items},
        )
        self.assertNotIn(
            str(breakwater_root / "skills" / "breakwater-reply" / "SKILL.md"),
            {item.get("path") for item in jira_auto_items},
        )
        self.assertNotIn(
            str(breakwater_root / "skills" / "breakwater-reply" / "SKILL.md"),
            {item.get("path") for item in jira_status_items},
        )

    def test_prompt_library_replaces_arbitrary_config_variables_without_code_branches(self) -> None:
        prompt_path = Path(self.tempdir.name) / "prompt.md"
        prompt_path.write_text(
            """
<!-- prompt: demo -->
repo=$code_repo
release=${release_repo}
unknown=$not_configured
<!-- /prompt -->
""",
            encoding="utf-8",
        )

        prompt = PromptLibrary(
            path=prompt_path,
            variables={
                "code_repo": "/mnt/code",
                "release_repo": "/mnt/release",
            },
        ).render("demo")

        self.assertIn("repo=/mnt/code", prompt)
        self.assertIn("release=/mnt/release", prompt)
        self.assertIn("unknown=$not_configured", prompt)

    def test_build_config_reads_yaml_and_falls_back_to_env_jira_token(self) -> None:
        config_path = Path(self.tempdir.name) / "config.yaml"
        config_path.write_text(
            """
db_path: state/breakwater.db
codex:
  url: ws://127.0.0.1:19999
  model: gpt-test
  effort: high
  workspace: codex-workspace
  start_server: false
queue:
  concurrency: 3
jira:
  skill_dir: skills/jira-issue
  projects: [OPS]
  auto_analyze_new_issues: true
  auto_analyze_projects: [OPS]
  status_summary_enabled: true
  status_summary_projects: [OPS]
  status_summary_target_statuses: [Done, Backlog]
  bot_mention_keys: [breakwater-bot]
  poll_interval_seconds: 12
  overlap_seconds: 34
github:
  repositories: [octo-org/example-repo]
  auto_analyze: true
  poll_interval_seconds: 7
  token: github-config-token
develop:
  skill_dir: skills/dev
prompt_variables:
  DEVELOP_DEFAULT_REPOSITORY: ${CODEX_WORKSPACE}
web:
  enabled: true
  host: 0.0.0.0
  port: 9999
admin_web:
  enabled: true
  host: 127.0.0.1
  port: 9876
  password_hash: pbkdf2_sha256$1$c2FsdA$FouE_BFeFb-52U0O7tysuqZP7Lm59JUUWb-D3MmXX2M
  session_secret: configured-secret
  jira_base_url: https://jira.example.com
lark:
  chat_id: oc_test
""",
            encoding="utf-8",
        )
        args = argparse.Namespace(
            config=config_path,
            db=None,
            codex_url=None,
            codex_model=None,
            codex_effort=None,
            codex_sandbox=None,
            max_reply_retries=None,
            codex_concurrency=None,
            turn_timeout=None,
            no_start_codex_server=None,
            jira_skill_dir=None,
            jira_token=None,
            no_jira=None,
            jira_record_new_issues=None,
            jira_auto_analyze_new_issues=None,
            jira_auto_analyze_project=None,
            jira_status_summary_enabled=None,
            jira_status_summary_project=None,
            jira_status_summary_target_status=None,
            jira_bot_mention_key=None,
            jira_project=None,
            jira_poll_interval=None,
            jira_overlap_seconds=None,
            web_host=None,
            web_port=None,
            no_web=None,
            admin_web_enabled=None,
            admin_web_host=None,
            admin_web_port=None,
            admin_password_hash=None,
            admin_session_secret=None,
            no_proxy=None,
            proxy_http=None,
            proxy_https=None,
            proxy_all=None,
            lark_app_id=None,
            lark_app_secret=None,
            lark_cli_config=None,
            chat_id=None,
        )

        with patch.dict(os.environ, {"JIRA_TOKEN": "env-token"}):
            config = build_config(args)

        self.assertEqual(config.db_path, config_path.parent / "state" / "breakwater.db")
        self.assertEqual(config.codex.ws_url, "ws://127.0.0.1:19999")
        self.assertEqual(config.codex.model, "gpt-test")
        self.assertEqual(config.codex.effort, "high")
        self.assertEqual(config.codex_workspace, config_path.parent / "codex-workspace")
        self.assertFalse(config.codex.start_server)
        self.assertEqual(config.codex_concurrency, 3)
        self.assertEqual(config.jira.projects, ("OPS",))
        self.assertEqual(config.jira.token, "env-token")
        self.assertFalse(config.jira.record_new_issues)
        self.assertTrue(config.jira.auto_analyze_new_issues)
        self.assertEqual(config.jira.auto_analyze_projects, ("OPS",))
        self.assertTrue(config.jira.status_summary_enabled)
        self.assertEqual(config.jira.status_summary_projects, ("OPS",))
        self.assertEqual(config.jira.status_summary_target_statuses, ("Done", "Backlog"))
        self.assertEqual(config.jira.bot_mention_keys, ("breakwater-bot",))
        self.assertTrue(config.github.enabled)
        self.assertEqual(config.github.repositories, ("octo-org/example-repo",))
        self.assertTrue(config.github.auto_analyze)
        self.assertEqual(config.github.poll_interval_seconds, 7)
        self.assertEqual(config.github.token, "github-config-token")
        self.assertEqual(config.jira_skill_dir, config_path.parent / "skills" / "jira-issue")
        self.assertEqual(config.develop.skill_dir, config_path.parent / "skills" / "dev")
        self.assertEqual(config.prompt_variables["CODEX_WORKSPACE"], str(config_path.parent / "codex-workspace"))
        self.assertEqual(config.prompt_variables["DEVELOP_DEFAULT_REPOSITORY"], str(config_path.parent / "codex-workspace"))
        self.assertTrue(config.proxy.enabled)
        self.assertEqual(proxy_environment(config.proxy)["http_proxy"], "http://127.0.0.1:7890")
        self.assertEqual(proxy_environment(config.proxy)["all_proxy"], "socks5h://127.0.0.1:7890")
        self.assertEqual(config.web_host, "0.0.0.0")
        self.assertEqual(config.web_port, 9999)
        self.assertTrue(config.admin_web.enabled)
        self.assertEqual(config.admin_web.host, "127.0.0.1")
        self.assertEqual(config.admin_web.port, 9876)
        self.assertEqual(config.admin_web.session_secret, "configured-secret")
        self.assertEqual(config.admin_web.jira_base_url, "https://jira.example.com")
        self.assertEqual(config.lark.chat_id, "oc_test")

    def test_build_config_reads_top_level_jira_token_without_prompt_exposure(self) -> None:
        config_path = Path(self.tempdir.name) / "config.yaml"
        config_path.write_text(
            """
JIRA_TOKEN: config-token
GITHUB_TOKEN: github-config-token
jira:
  token: nested-token
prompt_variables:
  JIRA_TOKEN: prompt-token
  jira_token: prompt-lower-token
  GITHUB_TOKEN: prompt-github-token
  github_token: prompt-github-lower-token
""",
            encoding="utf-8",
        )
        args = argparse.Namespace(
            config=config_path,
            db=None,
            codex_url=None,
            codex_model=None,
            codex_effort=None,
            codex_sandbox=None,
            codex_workspace=None,
            max_reply_retries=None,
            codex_concurrency=None,
            turn_timeout=None,
            no_start_codex_server=None,
            jira_skill_dir=None,
            jira_token=None,
            no_jira=None,
            jira_record_new_issues=None,
            jira_project=None,
            jira_poll_interval=None,
            jira_overlap_seconds=None,
            web_host=None,
            web_port=None,
            no_web=None,
            no_proxy=None,
            proxy_http=None,
            proxy_https=None,
            proxy_all=None,
            lark_app_id=None,
            lark_app_secret=None,
            lark_cli_config=None,
            chat_id=None,
        )

        with patch.dict(os.environ, {"JIRA_TOKEN": "env-token"}):
            config = build_config(args)

        self.assertEqual(config.jira.token, "config-token")
        self.assertEqual(config.github.token, "github-config-token")
        self.assertNotIn("JIRA_TOKEN", config.prompt_variables)
        self.assertNotIn("jira_token", config.prompt_variables)
        self.assertNotIn("GITHUB_TOKEN", config.prompt_variables)
        self.assertNotIn("github_token", config.prompt_variables)


if __name__ == "__main__":
    unittest.main()
