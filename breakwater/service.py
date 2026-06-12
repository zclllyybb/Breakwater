from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass

from .cases import (
    ActiveCaseRegistry,
    CaseResolver,
    CodexSlotPlanner,
    build_case_anchor,
    build_case_summary,
    extract_lark_reference_message_ids,
    format_case_resolution_message,
)
from .admin_web import BreakwaterAdminServer
from .codex_app_server import (
    CodexAppServerClient,
    CodexAppServerManager,
    CodexInputAttachment,
    CodexRunResult,
    CodexTurnSnapshot,
    codex_ready_url,
    is_codex_app_server_ready,
)
from .config import AppConfig
from .db import AnalysisCase, Database, LarkMessageRecord, Slot, utc_now
from .github_adapter import GitHubRestClient
from .github_monitor import GitHubMonitor
from .jira_adapter import JiraRestClient
from .jira_monitor import JiraMonitor
from .lark_adapter import LarkClient, LarkEvent, LarkEventConsumer, at_user_text, lark_message_mentions_bot
from .lark_content import LarkImageAttachment, normalize_lark_content
from .runtime import apply_jira_token_to_env, apply_proxy_environment, jira_token_environment, proxy_environment, recent_codex_threads
from .sources import (
    GITHUB_ISSUE_ANALYZE_SOURCE,
    JIRA_ISSUE_AUTO_ANALYZE_SOURCE,
    JIRA_STATUS_SUMMARY_SOURCE,
    LARK_JIRA_ANALYZE_SOURCE,
    is_direct_jira_analysis_source,
    requires_jira_automation_report,
)
from .web import BreakwaterWebServer


LOG = logging.getLogger(__name__)
LARK_CODEX_IMAGE_LIMIT = 5
LARK_SLOT_IMAGES_KEY = "breakwater_lark_images"
LARK_LISTENER_RETRY_BASE_SECONDS = 1.0
LARK_LISTENER_RETRY_MAX_SECONDS = 30.0


def _safe_lark_file_fragment(value: str) -> str:
    fragment = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return (fragment or "image")[:120]


def is_permanent_lark_reply_error(error: str) -> bool:
    normalized = error.lower()
    return "code=230011" in normalized or "message was withdrawn" in normalized


@dataclass(frozen=True)
class _TurnFailureHandling:
    completed: bool = False
    retry_same_prompt: bool = False
    stop: bool = False
    error: str | None = None


@dataclass(frozen=True)
class _CompletionVerification:
    completed: bool
    error: str | None = None


@dataclass(frozen=True)
class _JiraMarkerComment:
    comment_id: str
    body: str


@dataclass(frozen=True)
class _CodexExceptionHandling:
    completed: bool = False
    retry_allowed: bool = True
    error: str | None = None


@dataclass(frozen=True)
class _LarkPromptInput:
    text: str
    context_messages: list[LarkMessageRecord]
    images: list[dict[str, object]]


class BreakwaterService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.started_at = utc_now()
        applied_proxy_env = apply_proxy_environment(config.proxy)
        credential_env = jira_token_environment(config.jira.token)
        if config.github.token:
            credential_env["GITHUB_TOKEN"] = config.github.token
        os.environ.update(credential_env)
        self.db = Database(config.db_path)
        codex_env = dict(applied_proxy_env)
        apply_jira_token_to_env(codex_env, config.jira.token)
        if config.github.token:
            codex_env["GITHUB_TOKEN"] = config.github.token
        self.codex_server = CodexAppServerManager(config.codex, env_overrides=codex_env)
        self.codex_client = CodexAppServerClient(
            config.codex,
            project_root=config.project_root,
            workspace=config.codex_workspace,
            db_path=config.db_path,
            jira_skill_dir=config.jira_skill_dir,
            develop_skill_dir=config.develop.skill_dir,
            github_api_url=config.github.api_url,
            develop_enabled=config.develop.enabled,
            prompt_variables=config.prompt_variables,
            task_env=codex_env,
        )
        self.lark_client: LarkClient | None = None
        self.lark_consumer: LarkEventConsumer | None = None
        self.jira_client: JiraRestClient | None = None
        self.github_client: GitHubRestClient | None = None
        self.jira_monitor = JiraMonitor(self.db, config.jira, config.jira_skill_dir)
        self.github_monitor = GitHubMonitor(self.db, config.github)
        self.web_server: BreakwaterWebServer | None = None
        self.admin_web_server: BreakwaterAdminServer | None = None
        self.case_resolver = CaseResolver(self.db)
        self.slot_planner = CodexSlotPlanner()
        self.active_runs = ActiveCaseRegistry()
        self._codex_queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued_slot_ids: set[str] = set()
        self._recovered_running_slot_ids: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self._lark_listener_running = False
        self._lark_listener_restart_count = 0
        self._lark_listener_last_started_at: str | None = None
        self._lark_listener_last_event_at: str | None = None
        self._lark_listener_last_error: str | None = None
        self._lark_listener_retry_base_seconds = LARK_LISTENER_RETRY_BASE_SECONDS
        self._lark_listener_retry_max_seconds = LARK_LISTENER_RETRY_MAX_SECONDS

    async def serve(self) -> None:
        self.started_at = utc_now()
        self.db.init()
        self._backfill_lark_chat_cases()
        await self.codex_server.start()
        self._start_codex_workers()
        self._recover_queued_slots()
        self.lark_client = LarkClient(
            app_id=self.config.lark.app_id,
            app_secret=self.config.lark.app_secret,
            config_path=self.config.lark.cli_config,
        )
        await self._recover_unhandled_lark_mentions()
        reply_task = self._start_reply_sender()
        listen_task = asyncio.create_task(self._lark_listener_supervisor_loop(), name="lark-listener")
        self._tasks.add(listen_task)
        listen_task.add_done_callback(self._log_background_task_failure)
        if self.config.jira.enabled:
            jira_task = asyncio.create_task(self._jira_poller_loop(), name="jira-poller")
            self._tasks.add(jira_task)
        if self.config.github.enabled and self.config.github.repositories:
            github_task = asyncio.create_task(self._github_poller_loop(), name="github-poller")
            self._tasks.add(github_task)
        if self.config.web_enabled:
            self.web_server = BreakwaterWebServer(self.config.web_host, self.config.web_port, self.status_snapshot)
            self.web_server.start()
        if self.config.admin_web.enabled:
            if not self.config.admin_web.password_hash:
                self.db.log_event(
                    "admin",
                    "web.skipped",
                    "admin web is enabled but password_hash is not configured",
                    host=self.config.admin_web.host,
                    port=self.config.admin_web.port,
                )
            else:
                self.admin_web_server = BreakwaterAdminServer(
                    self.config.admin_web.host,
                    self.config.admin_web.port,
                    self.db,
                    password_hash=self.config.admin_web.password_hash,
                    session_secret=self.config.admin_web.session_secret,
                    session_ttl_seconds=self.config.admin_web.session_ttl_seconds,
                    jira_base_url=self.config.admin_web.jira_base_url,
                )
                self.admin_web_server.start()
                self.db.log_event(
                    "admin",
                    "web.started",
                    "admin web server started",
                    host=self.config.admin_web.host,
                    port=self.config.admin_web.port,
                )
        LOG.info("breakwater service started", extra={"component": "service"})
        try:
            await self._stop.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        LOG.info("stopping breakwater service", extra={"component": "service"})
        self._stop.set()
        if self.lark_consumer:
            await self.lark_consumer.stop()
        if self.web_server:
            await asyncio.to_thread(self.web_server.stop)
        if self.admin_web_server:
            await asyncio.to_thread(self.admin_web_server.stop)
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with suppress(asyncio.CancelledError):
                try:
                    await task
                except Exception:
                    LOG.exception("worker task failed while stopping", extra={"component": "service"})
        await self.codex_server.stop()

    def _backfill_lark_chat_cases(self) -> None:
        adopted = self.db.backfill_lark_chat_cases()
        if not adopted:
            return
        self.db.log_event("lark", "lark.chat_cases.backfilled", "historical lark slots attached to chat cases", None, adopted_slots=adopted)
        LOG.info("historical lark slots attached to chat cases", extra={"component": "lark", "adopted_slots": adopted})

    def status_snapshot(self) -> dict[str, object]:
        slots = self.db.list_slots(limit=25)
        running_slots = self.db.list_slots_by_ids(self.active_runs.active_slot_ids)
        queued_slots = self.db.list_slots_by_ids(self._queued_slot_ids)
        codex_thread_ids = self.db.recent_codex_thread_ids(limit=100)
        case_timelines = self.db.case_timelines(limit=25, slots_per_case=30)
        consumer_running = bool(getattr(self.lark_consumer, "is_running", False)) if self.lark_consumer is not None else False
        return {
            "started_at": self.started_at,
            "lark_listener": {
                "running": self._lark_listener_running,
                "consumer_running": consumer_running,
                "restart_count": self._lark_listener_restart_count,
                "last_started_at": self._lark_listener_last_started_at,
                "last_event_at": self._lark_listener_last_event_at,
                "last_error": self._lark_listener_last_error,
            },
            "queue": {
                "concurrency": self.config.codex_concurrency,
                "queued": self._codex_queue.qsize(),
                "running": len(self.active_runs.active_slot_ids),
                "queued_slot_ids": sorted(self._queued_slot_ids),
                "active_slot_ids": sorted(self.active_runs.active_slot_ids),
            },
            "status_counts": self.db.count_slots_by_status(),
            "slots": [slot.__dict__ for slot in slots],
            "running_slots": [slot.__dict__ for slot in running_slots],
            "queued_slots": [slot.__dict__ for slot in queued_slots],
            "open_jira_analyze_slots": [slot.__dict__ for slot in self.db.open_jira_analyze_slots(limit=200)],
            "lark_messages": [slot.__dict__ for slot in self.db.recent_lark_slots(limit=100)],
            "lark_message_events": [message.__dict__ for message in self.db.recent_lark_messages(limit=120)],
            "cases": [item["case"] for item in case_timelines],
            "case_timelines": case_timelines,
            "codex": {
                "app_server_url": self.config.codex.ws_url,
                "ready_url": codex_ready_url(self.config.codex.ws_url),
                "ready": is_codex_app_server_ready(self.config.codex.ws_url),
                "owned": self.codex_server.owned,
                "pid": self.codex_server.pid,
                "model": self.config.codex.model,
                "effort": self.config.codex.effort,
                "sandbox": self.config.codex.sandbox,
                "approval_policy": self.config.codex.approval_policy,
                "workspace": str(self.config.codex_workspace),
                "start_server": self.config.codex.start_server,
                "proxy_enabled": self.config.proxy.enabled,
                "proxy_env": proxy_environment(self.config.proxy),
                "recent_threads": recent_codex_threads(
                    limit=12,
                    cwd=self.config.codex_workspace,
                    breakwater_only=True,
                    thread_ids=codex_thread_ids,
                ),
            },
            "jira": {
                "enabled": self.config.jira.enabled,
                "record_new_issues": self.config.jira.record_new_issues,
                "auto_analyze_new_issues": self.config.jira.auto_analyze_new_issues,
                "auto_analyze_projects": list(self.config.jira.auto_analyze_projects or self.config.jira.projects),
                "status_summary_enabled": self.config.jira.status_summary_enabled,
                "status_summary_projects": list(self.config.jira.status_summary_projects or self.config.jira.projects),
                "status_summary_target_statuses": list(self.config.jira.status_summary_target_statuses),
                "bot_mention_keys": list(self.config.jira.bot_mention_keys),
                "projects": list(self.config.jira.projects),
                "poll_interval_seconds": self.config.jira.poll_interval_seconds,
                "overlap_seconds": self.config.jira.overlap_seconds,
                "cursor": self.db.get_state("jira.last_success_at"),
                "counts": self.db.jira_counts(),
                "recent_issues": [issue.__dict__ for issue in self.db.recent_jira_issues(limit=10)],
                "recent_analyze_comments": [comment.__dict__ for comment in self.db.recent_jira_comments(limit=20)],
            },
            "github": {
                "enabled": self.config.github.enabled,
                "repositories": list(self.config.github.repositories),
                "auto_analyze": self.config.github.auto_analyze,
                "poll_interval_seconds": self.config.github.poll_interval_seconds,
                "overlap_seconds": self.config.github.overlap_seconds,
                "cursors": {
                    repo: self.db.get_state(f"github:{repo.lower()}.last_success_at")
                    for repo in self.config.github.repositories
                },
                "counts": self.db.github_counts(),
                "recent_issues": [issue.__dict__ for issue in self.db.recent_github_issues(limit=10)],
            },
            "events": self.db.recent_events(limit=30),
        }

    def enqueue_slot(self, slot: Slot) -> None:
        if slot.slot_id in self._queued_slot_ids or self.active_runs.is_slot_active(slot.slot_id):
            return
        self._queued_slot_ids.add(slot.slot_id)
        self._codex_queue.put_nowait(slot.slot_id)
        self.db.log_event("queue", "slot.enqueued", "slot enqueued for codex", slot.slot_id, queue_size=self._codex_queue.qsize())
        LOG.info("slot enqueued", extra={"component": "queue", "slot_id": slot.slot_id})

    def _start_codex_workers(self) -> None:
        existing = [task for task in self._tasks if task.get_name().startswith("codex-worker-") and not task.done()]
        missing = max(0, self.config.codex_concurrency - len(existing))
        for index in range(len(existing), len(existing) + missing):
            task = asyncio.create_task(self._codex_worker_loop(index + 1), name=f"codex-worker-{index + 1}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _start_reply_sender(self) -> asyncio.Task[None]:
        for task in self._tasks:
            if task.get_name() == "reply-sender" and not task.done():
                return task
        task = asyncio.create_task(self._reply_sender_loop(), name="reply-sender")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _recover_queued_slots(self) -> None:
        for slot in self.db.recoverable_slots():
            if slot.codex_status == "running":
                self._recovered_running_slot_ids.add(slot.slot_id)
                self.db.log_event(
                    "queue",
                    "slot.recovered_running",
                    "running slot recovered after restart; will ask Codex to continue",
                    slot.slot_id,
                    thread_id=slot.codex_thread_id,
                    turn_id=slot.codex_turn_id,
                )
            self.enqueue_slot(slot)

    async def _recover_unhandled_lark_mentions(self) -> None:
        if self.lark_client is None:
            return
        for message in reversed(self.db.recent_lark_messages(limit=200)):
            if message.handled_slot_id or message.message_type not in {"text", "post"}:
                continue
            mentioned_bot = message.chat_type != "group" or await self._stored_lark_message_mentions_bot(message)
            if message.chat_type == "group" and not mentioned_bot:
                continue
            resolution = self.case_resolver.inspect_lark_text(message.content, chat_name=message.chat_name or "")
            event_id = message.event_id or f"recovered:{message.message_id}"
            raw = self.db.get_lark_message_raw(message.message_id) or {"recovered_from_lark_message": message.message_id}
            if resolution.status in {"unknown", "ambiguous"}:
                new_issue_key = self._single_unresolved_jira_issue_key(resolution)
                if message.chat_type == "group" and new_issue_key:
                    slot = self._create_lark_jira_analyze_slot(
                        issue_key=new_issue_key,
                        event_id=event_id,
                        message_id=message.message_id,
                        chat_id=message.chat_id,
                        chat_type=message.chat_type,
                        chat_name=message.chat_name,
                        sender_id=message.sender_id,
                        message_type=message.message_type,
                        content=message.content,
                        mentioned_bot=mentioned_bot,
                        raw=raw,
                    )
                    self.db.log_event(
                        "lark",
                        "message.recovered_jira_analysis",
                        "recovered unhandled lark mention into new Jira analysis case",
                        slot.slot_id,
                        message_id=message.message_id,
                        issue_key=new_issue_key,
                    )
                    self.enqueue_slot(slot)
                    continue
                slot = self._record_lark_reply_slot(
                    source="lark_case_resolution",
                    event_id=event_id,
                    message_id=message.message_id,
                    chat_id=message.chat_id,
                    chat_type=message.chat_type,
                    chat_name=message.chat_name,
                    sender_id=message.sender_id,
                    message_type=message.message_type,
                    content=message.content,
                    mentioned_bot=mentioned_bot,
                    raw=raw,
                    reply_text=format_case_resolution_message(resolution),
                )
                self.db.log_event(
                    "lark",
                    "message.recovered_resolution",
                    "recovered unhandled lark mention and asked for case clarification",
                    slot.slot_id,
                    message_id=message.message_id,
                    status=resolution.status,
                )
                continue
            case = resolution.case if resolution.is_resolved else None
            if case is None:
                case = self._ensure_lark_chat_case(
                    chat_id=message.chat_id,
                    chat_type=message.chat_type,
                    chat_name=message.chat_name,
                    sender_id=message.sender_id,
                )
            if message.chat_type == "group" and case:
                case, binding_error = self._bind_case_to_lark_group_or_error(
                    case,
                    chat_id=message.chat_id,
                    chat_name=message.chat_name,
                )
                if binding_error:
                    slot = self._record_lark_reply_slot(
                        source="lark_case_binding_conflict",
                        event_id=event_id,
                        message_id=message.message_id,
                        chat_id=message.chat_id,
                        chat_type=message.chat_type,
                        chat_name=message.chat_name,
                        sender_id=message.sender_id,
                        message_type=message.message_type,
                        content=message.content,
                        mentioned_bot=mentioned_bot,
                        raw=raw,
                        reply_text=binding_error,
                        case_id=resolution.case.case_id if resolution.case else None,
                    )
                    self.db.log_event(
                        "lark",
                        "message.recovered_binding_conflict",
                        "recovered lark mention resolved to a case bound to another group",
                        slot.slot_id,
                        message_id=message.message_id,
                        case_id=resolution.case.case_id if resolution.case else None,
                        current_chat_id=message.chat_id,
                    )
                    continue
                assert case is not None
            prompt_input = self._build_lark_prompt_input(
                current_message=message.content,
                chat_id=message.chat_id,
                chat_type=message.chat_type,
                sender_id=message.sender_id,
                current_message_id=message.message_id,
                case_id=case.case_id,
            )
            slot = self.db.create_slot(
                source="lark_case_followup",
                lark_event_id=event_id,
                lark_message_id=message.message_id,
                chat_id=message.chat_id,
                chat_type=message.chat_type,
                sender_id=message.sender_id,
                incoming_text=prompt_input.text,
                raw=self._lark_slot_raw(
                    raw,
                    current_message_id=message.message_id,
                    current_message_type=message.message_type,
                    current_content=message.content,
                    prompt_images=prompt_input.images,
                ),
                case_id=case.case_id,
                delivery_target="lark_reply",
            )
            self.db.record_lark_message(
                message_id=message.message_id,
                event_id=message.event_id,
                chat_id=message.chat_id,
                chat_type=message.chat_type or "",
                chat_name=message.chat_name,
                sender_id=message.sender_id,
                message_type=message.message_type,
                content=message.content,
                mentioned_bot=mentioned_bot,
                raw=raw,
                handled_slot_id=slot.slot_id,
            )
            self.db.log_event(
                "lark",
                "message.recovered",
                "recovered unhandled lark mention into codex queue",
                slot.slot_id,
                message_id=message.message_id,
                chat_name=message.chat_name,
                case_id=case.case_id if case else None,
                context_message_count=len(prompt_input.context_messages),
            )
            self.enqueue_slot(slot)

    async def _lark_event_mentions_bot(self, event: LarkEvent) -> bool:
        assert self.lark_client is not None
        if lark_message_mentions_bot(event.raw, event.content, self.lark_client.app_id):
            return True
        try:
            return await asyncio.to_thread(self.lark_client.message_mentions_bot, event.message_id)
        except Exception as exc:
            self.db.log_event(
                "lark",
                "mention.lookup_failed",
                "failed to verify lark mention through message API",
                message_id=event.message_id,
                error=str(exc),
            )
            LOG.exception("failed to verify lark mention", extra={"component": "lark"})
            return False

    async def _stored_lark_message_mentions_bot(self, message: LarkMessageRecord) -> bool:
        assert self.lark_client is not None
        try:
            return await asyncio.to_thread(self.lark_client.message_mentions_bot, message.message_id)
        except Exception as exc:
            self.db.log_event(
                "lark",
                "mention.lookup_failed",
                "failed to verify stored lark mention through message API",
                message_id=message.message_id,
                error=str(exc),
            )
            LOG.exception("failed to verify stored lark mention", extra={"component": "lark"})
            return False

    async def _codex_worker_loop(self, worker_id: int) -> None:
        LOG.info("codex worker started", extra={"component": "queue"})
        while True:
            slot_id = await self._codex_queue.get()
            self._queued_slot_ids.discard(slot_id)
            slot: Slot | None = None
            try:
                slot = self.db.get_slot(slot_id)
                if slot is None:
                    self.db.log_event("queue", "slot.missing", "queued slot disappeared", slot_id)
                    continue
                if slot.reply_status == "sent" or slot.status in {"replied", "failed"}:
                    continue
                if not self.active_runs.claim(slot):
                    self._queued_slot_ids.add(slot_id)
                    self._codex_queue.put_nowait(slot_id)
                    await asyncio.sleep(0.2)
                    continue
                await self._run_codex_for_slot(slot)
            finally:
                self.active_runs.release(slot)
                self._codex_queue.task_done()

    async def _wait_for_slot_terminal(self, slot_id: str) -> None:
        while True:
            slot = self.db.get_slot(slot_id)
            if slot and (slot.status == "failed" or (slot.status == "replied" and slot.codex_status in {"completed", "failed"})):
                return
            await asyncio.sleep(0.2)

    def _new_lark_consumer(self) -> LarkEventConsumer:
        return LarkEventConsumer(chat_id=self.config.lark.chat_id)

    async def _lark_listener_supervisor_loop(self) -> None:
        retry_seconds = self._lark_listener_retry_base_seconds
        while not self._stop.is_set():
            consumer = self._new_lark_consumer()
            self.lark_consumer = consumer
            self._lark_listener_running = True
            self._lark_listener_last_started_at = utc_now()
            self.db.log_event(
                "lark",
                "listener.started",
                "lark event listener started",
                restart_count=self._lark_listener_restart_count,
            )
            try:
                await self._consume_lark_events(consumer)
                if self._stop.is_set():
                    return
                raise RuntimeError("lark event listener stopped unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._lark_listener_running = False
                self._lark_listener_last_error = str(exc)
                self._lark_listener_restart_count += 1
                self.db.log_event(
                    "lark",
                    "listener.failed",
                    "lark event listener failed; restarting",
                    error=str(exc),
                    restart_count=self._lark_listener_restart_count,
                    retry_seconds=retry_seconds,
                )
                LOG.exception("lark event listener failed; restarting", extra={"component": "lark"})
            finally:
                self._lark_listener_running = False
                with suppress(Exception):
                    await consumer.stop()
                if self.lark_consumer is consumer:
                    self.lark_consumer = None
            if not self._stop.is_set():
                await asyncio.sleep(retry_seconds)
                retry_seconds = min(retry_seconds * 2, self._lark_listener_retry_max_seconds)

    async def _consume_lark_events(self, consumer: LarkEventConsumer) -> None:
        async for event in consumer.events():
            self._lark_listener_last_event_at = utc_now()
            try:
                await self._handle_lark_event(event)
            except Exception as exc:
                self.db.log_event(
                    "lark",
                    "message.handle_failed",
                    "failed to handle lark event",
                    message_id=event.message_id,
                    event_id=event.event_id,
                    error=str(exc),
                )
                LOG.exception("failed to handle lark event", extra={"component": "lark"})

    def _log_background_task_failure(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        self.db.log_event("service", "task.failed", "background task failed", task=task.get_name(), error=str(exc))
        LOG.exception(
            "background task failed",
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={"component": "service", "task": task.get_name()},
        )

    async def _jira_poller_loop(self) -> None:
        while True:
            try:
                result = await asyncio.to_thread(self.jira_monitor.poll_once)
                LOG.info(
                    "jira poll result new_issues=%s analyze_comments=%s analyze_slots=%s issue_analyze_slots=%s status_summary_slots=%s status_changed_issues=%s changed_issues=%s",
                    result.new_issues,
                    result.analyze_comments,
                    result.analyze_slots,
                    result.issue_analyze_slots,
                    result.status_summary_slots,
                    result.status_changed_issues,
                    result.changed_issues,
                    extra={"component": "jira"},
                )
                self._enqueue_jira_analyze_slots()
            except Exception as exc:
                self.db.log_event("jira", "poll.failed", "jira poll failed", error=str(exc))
                LOG.exception("jira poll failed", extra={"component": "jira"})
            await asyncio.sleep(self.config.jira.poll_interval_seconds)

    def _enqueue_jira_analyze_slots(self) -> None:
        for slot in self.db.jira_analyze_slots_needing_queue():
            self.enqueue_slot(slot)

    async def _github_poller_loop(self) -> None:
        while True:
            try:
                result = await asyncio.to_thread(self.github_monitor.poll_once)
                LOG.info(
                    "github poll result new_issues=%s analyze_slots=%s repositories=%s",
                    result.new_issues,
                    result.analyze_slots,
                    ",".join(result.repositories),
                    extra={"component": "github"},
                )
                self._enqueue_github_issue_slots()
            except Exception as exc:
                self.db.log_event("github", "poll.failed", "github poll failed", error=str(exc))
                LOG.exception("github poll failed", extra={"component": "github"})
            await asyncio.sleep(self.config.github.poll_interval_seconds)

    def _enqueue_github_issue_slots(self) -> None:
        for slot in self.db.github_issue_slots_needing_queue():
            self.enqueue_slot(slot)

    def _single_unresolved_jira_issue_key(self, resolution) -> str | None:
        if resolution.status != "unknown":
            return None
        issue_keys = {
            reference.alias_key.upper()
            for reference in resolution.unresolved
            if reference.alias_type == "jira_issue_key"
        }
        if len(issue_keys) == 1 and len(resolution.unresolved) == 1:
            return next(iter(issue_keys))
        return None

    def _bind_case_to_lark_group_or_error(
        self,
        case: AnalysisCase,
        *,
        chat_id: str,
        chat_name: str | None,
    ) -> tuple[AnalysisCase | None, str | None]:
        if case.bound_lark_chat_id and case.bound_lark_chat_id != chat_id:
            return None, self._case_group_binding_conflict_message(case, chat_id=chat_id, chat_name=chat_name)
        bound = self.db.bind_case_lark_group(case.case_id, chat_id, chat_name) or case
        if bound.bound_lark_chat_id and bound.bound_lark_chat_id != chat_id:
            return None, self._case_group_binding_conflict_message(bound, chat_id=chat_id, chat_name=chat_name)
        return bound, None

    def _case_group_binding_conflict_message(self, case: AnalysisCase, *, chat_id: str, chat_name: str | None) -> str:
        original_name = case.bound_lark_chat_name or "unknown group"
        current_name = chat_name or "current group"
        return (
            "这个 case 已经绑定到另一个飞书群，Breakwater 不会在当前群继续处理，避免同一个问题分析分散。\n"
            f"Case: {case.scope_key} / {case.case_id}\n"
            f"原绑定群: {original_name} ({case.bound_lark_chat_id or 'unknown chat id'})\n"
            f"当前群: {current_name} ({chat_id})\n"
            "请回到原绑定群继续讨论。"
        )

    def _ensure_lark_chat_case(
        self,
        *,
        chat_id: str,
        chat_type: str | None,
        chat_name: str | None,
        sender_id: str | None,
    ) -> AnalysisCase:
        case = self.db.ensure_lark_chat_case(chat_id=chat_id, chat_type=chat_type, chat_name=chat_name, sender_id=sender_id)
        self.db.log_event(
            "lark",
            "chat_case.ensured",
            "ensured non-Jira Lark chat analysis case",
            case_id=case.case_id,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=chat_name,
            sender_id=sender_id,
            latest_thread_id=case.latest_codex_thread_id,
        )
        return case

    def _record_lark_reply_slot(
        self,
        *,
        source: str,
        event_id: str | None,
        message_id: str,
        chat_id: str,
        chat_type: str | None,
        chat_name: str | None,
        sender_id: str | None,
        message_type: str,
        content: str,
        mentioned_bot: bool,
        raw: dict,
        reply_text: str,
        case_id: str | None = None,
    ) -> Slot:
        slot = self.db.create_slot(
            source=source,
            lark_event_id=event_id,
            lark_message_id=message_id,
            chat_id=chat_id,
            chat_type=chat_type,
            sender_id=sender_id,
            incoming_text=content,
            raw=raw,
            case_id=case_id,
            delivery_target="lark_reply",
        )
        self.db.mark_codex_skipped(slot.slot_id)
        self.db.record_lark_message(
            message_id=message_id,
            event_id=event_id,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=chat_name,
            sender_id=sender_id,
            message_type=message_type,
            content=content,
            mentioned_bot=mentioned_bot,
            raw=raw,
            handled_slot_id=slot.slot_id,
        )
        self.db.record_reply_request(slot.slot_id, reply_text)
        return slot

    def _create_lark_jira_analyze_slot(
        self,
        *,
        issue_key: str,
        event_id: str | None,
        message_id: str,
        chat_id: str,
        chat_type: str | None,
        chat_name: str | None,
        sender_id: str | None,
        message_type: str,
        content: str,
        mentioned_bot: bool,
        raw: dict,
    ) -> Slot:
        normalized_issue_key = issue_key.upper()
        case = self.db.ensure_case(scope_type="jira_issue", scope_key=normalized_issue_key, title=normalized_issue_key)
        if chat_type == "group":
            case = self.db.bind_case_lark_group(case.case_id, chat_id, chat_name) or case
        prompt_input = self._build_lark_prompt_input(
            current_message=content,
            chat_id=chat_id,
            chat_type=chat_type,
            sender_id=sender_id,
            current_message_id=message_id,
            case_id=case.case_id,
        )
        slot = self.db.create_slot(
            source="lark_jira_analyze",
            lark_event_id=event_id,
            lark_message_id=message_id,
            chat_id=chat_id,
            chat_type=chat_type,
            sender_id=sender_id,
            incoming_text=prompt_input.text,
            raw=self._lark_slot_raw(
                raw,
                current_message_id=message_id,
                current_message_type=message_type,
                current_content=content,
                prompt_images=prompt_input.images,
            ),
            jira_issue_key=normalized_issue_key,
            case_id=case.case_id,
            delivery_target="jira_comment_and_lark_reply",
        )
        self.db.record_lark_message(
            message_id=message_id,
            event_id=event_id,
            chat_id=chat_id,
            chat_type=chat_type,
            chat_name=chat_name,
            sender_id=sender_id,
            message_type=message_type,
            content=content,
            mentioned_bot=mentioned_bot,
            raw=raw,
            handled_slot_id=slot.slot_id,
        )
        self.db.log_event(
            "lark",
            "jira_analysis.requested",
            "created Jira analysis case from lark group mention",
            slot.slot_id,
            issue_key=normalized_issue_key,
            case_id=case.case_id,
            chat_id=chat_id,
            chat_name=chat_name,
            context_message_count=len(prompt_input.context_messages),
        )
        return slot

    async def _handle_lark_event(self, event: LarkEvent) -> None:
        assert self.lark_client is not None
        if event.sender_id == self.lark_client.app_id:
            LOG.info("skip self-sent lark event", extra={"component": "lark"})
            return
        if event.message_type not in {"text", "post"}:
            LOG.info("skip unsupported lark event", extra={"component": "lark"})
            return
        normalized = normalize_lark_content(event.message_type, event.content, event.raw)
        reference_message_ids = extract_lark_reference_message_ids(event.raw)
        chat_name = await self._lark_chat_name(event)
        mentioned_bot = event.chat_type != "group" or await self._lark_event_mentions_bot(event)
        resolution = self.case_resolver.inspect_lark_text(
            normalized.text,
            lark_message_ids=reference_message_ids,
            chat_name=chat_name,
        )
        if event.chat_type == "group" and not mentioned_bot:
            self.db.record_lark_message(
                message_id=event.message_id,
                event_id=event.event_id,
                chat_id=event.chat_id,
                chat_type=event.chat_type,
                chat_name=chat_name,
                sender_id=event.sender_id,
                message_type=event.message_type,
                content=normalized.text,
                mentioned_bot=False,
                raw=event.raw,
            )
            self.db.log_event(
                "lark",
                "message.context_recorded",
                "recorded unmentioned group message for future case context",
                message_id=event.message_id,
                chat_id=event.chat_id,
                chat_name=chat_name,
                case_id=resolution.case.case_id if resolution.is_resolved and resolution.case else None,
            )
            return
        if resolution.status in {"unknown", "ambiguous"}:
            new_issue_key = self._single_unresolved_jira_issue_key(resolution)
            if event.chat_type == "group" and new_issue_key:
                slot = self._create_lark_jira_analyze_slot(
                    issue_key=new_issue_key,
                    event_id=event.event_id,
                    message_id=event.message_id,
                    chat_id=event.chat_id,
                    chat_type=event.chat_type,
                    chat_name=chat_name,
                    sender_id=event.sender_id,
                    message_type=event.message_type,
                    content=normalized.text,
                    mentioned_bot=mentioned_bot,
                    raw=event.raw,
                )
                self.enqueue_slot(slot)
                return
            slot = self._record_lark_reply_slot(
                source="lark_case_resolution",
                event_id=event.event_id,
                message_id=event.message_id,
                chat_id=event.chat_id,
                chat_type=event.chat_type,
                chat_name=chat_name,
                sender_id=event.sender_id,
                message_type=event.message_type,
                content=normalized.text,
                mentioned_bot=mentioned_bot,
                raw=event.raw,
                reply_text=format_case_resolution_message(resolution),
            )
            self.db.log_event(
                "lark",
                "case_resolution.required",
                "case reference was missing or ambiguous; asked user to clarify",
                slot.slot_id,
                status=resolution.status,
                chat_name=chat_name,
                reference_message_ids=reference_message_ids,
            )
            return
        case = resolution.case if resolution.is_resolved else None
        if case is None:
            case = self._ensure_lark_chat_case(
                chat_id=event.chat_id,
                chat_type=event.chat_type,
                chat_name=chat_name,
                sender_id=event.sender_id,
            )
        if event.chat_type == "group" and case:
            case, binding_error = self._bind_case_to_lark_group_or_error(
                case,
                chat_id=event.chat_id,
                chat_name=chat_name,
            )
            if binding_error:
                slot = self._record_lark_reply_slot(
                    source="lark_case_binding_conflict",
                    event_id=event.event_id,
                    message_id=event.message_id,
                    chat_id=event.chat_id,
                    chat_type=event.chat_type,
                    chat_name=chat_name,
                    sender_id=event.sender_id,
                    message_type=event.message_type,
                    content=normalized.text,
                    mentioned_bot=mentioned_bot,
                    raw=event.raw,
                    reply_text=binding_error,
                    case_id=resolution.case.case_id if resolution.case else None,
                )
                self.db.log_event(
                    "lark",
                    "case_binding.conflict",
                    "case is bound to a different lark group; replied without codex",
                    slot.slot_id,
                    case_id=resolution.case.case_id if resolution.case else None,
                    bound_chat_id=resolution.case.bound_lark_chat_id if resolution.case else None,
                    current_chat_id=event.chat_id,
                    current_chat_name=chat_name,
                )
                return
            assert case is not None
        prompt_input = self._build_lark_prompt_input(
            current_message=normalized.text,
            chat_id=event.chat_id,
            chat_type=event.chat_type,
            sender_id=event.sender_id,
            current_message_id=event.message_id,
            case_id=case.case_id,
        )
        slot = self.db.create_slot(
            source="lark_case_followup",
            lark_event_id=event.event_id,
            lark_message_id=event.message_id,
            chat_id=event.chat_id,
            chat_type=event.chat_type,
            sender_id=event.sender_id,
            incoming_text=prompt_input.text,
            raw=self._lark_slot_raw(
                event.raw,
                current_message_id=event.message_id,
                current_message_type=event.message_type,
                current_content=event.content,
                prompt_images=prompt_input.images,
            ),
            case_id=case.case_id,
            delivery_target="lark_reply",
        )
        self.db.record_lark_message(
            message_id=event.message_id,
            event_id=event.event_id,
            chat_id=event.chat_id,
            chat_type=event.chat_type,
            chat_name=chat_name,
            sender_id=event.sender_id,
            message_type=event.message_type,
            content=normalized.text,
            mentioned_bot=mentioned_bot,
            raw=event.raw,
            handled_slot_id=slot.slot_id,
        )
        self.db.log_event(
            "lark",
            "message.received",
            "created case follow-up slot from lark message" if case else "created slot from lark message",
            slot.slot_id,
            message_id=event.message_id,
            chat_id=event.chat_id,
            chat_name=chat_name,
            sender_id=event.sender_id,
            content=normalized.text,
            case_id=case.case_id if case else None,
            reference_message_ids=reference_message_ids,
            context_message_count=len(prompt_input.context_messages),
        )
        LOG.info("created slot from lark message", extra={"component": "lark", "slot_id": slot.slot_id})
        self.enqueue_slot(slot)

    def _build_lark_prompt_input(
        self,
        *,
        current_message: str,
        chat_id: str,
        chat_type: str | None,
        sender_id: str | None,
        current_message_id: str,
        case_id: str | None,
    ) -> _LarkPromptInput:
        context_messages = [
            message
            for message in self.db.lark_context_since_last_handled(
                chat_id=chat_id,
                chat_type=chat_type,
                sender_id=sender_id,
                case_id=case_id,
            )
            if message.message_id != current_message_id
        ]
        return _LarkPromptInput(
            text=self._format_lark_prompt_input(
                current_message,
                context_messages,
                chat_type=chat_type,
                case_bound=bool(case_id),
            ),
            context_messages=context_messages,
            images=self._lark_context_image_entries(context_messages),
        )

    def _format_lark_prompt_input(
        self,
        current_message: str,
        context_messages: list[LarkMessageRecord],
        *,
        chat_type: str | None,
        case_bound: bool,
    ) -> str:
        if not context_messages:
            return current_message
        chat_label = "群聊" if chat_type == "group" else "单聊"
        scope_label = "该 case" if case_bound else "该聊天"
        lines = [
            f"{chat_label}上下文（上次 Breakwater 处理{scope_label}以来的新消息，按时间顺序）:",
        ]
        for message in context_messages:
            chat_name = f" {message.chat_name}" if message.chat_name else ""
            lines.append(f"- [{message.observed_at}] sender={message.sender_id or ''}{chat_name}: {message.content}")
        lines.extend(["", "当前触发消息:", current_message])
        return "\n".join(lines)

    def _format_lark_case_input(self, current_message: str, context_messages: list[LarkMessageRecord]) -> str:
        return self._format_lark_prompt_input(
            current_message,
            context_messages,
            chat_type="group",
            case_bound=True,
        )

    def _lark_context_image_entries(self, context_messages: list[LarkMessageRecord]) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        for message in context_messages:
            entries.extend(
                self._lark_image_entries(
                    message_id=message.message_id,
                    message_type=message.message_type,
                    content=message.content,
                    raw=self.db.get_lark_message_raw(message.message_id),
                    source="context",
                )
            )
        return entries

    def _lark_slot_raw(
        self,
        raw: dict,
        *,
        current_message_id: str,
        current_message_type: str,
        current_content: str,
        prompt_images: list[dict[str, object]],
    ) -> dict:
        images = list(prompt_images)
        images.extend(
            self._lark_image_entries(
                message_id=current_message_id,
                message_type=current_message_type,
                content=current_content,
                raw=raw,
                source="current",
            )
        )
        if not images:
            return raw
        enriched = dict(raw or {})
        enriched[LARK_SLOT_IMAGES_KEY] = images
        return enriched

    def _lark_image_entries(
        self,
        *,
        message_id: str,
        message_type: str,
        content: str,
        raw: dict,
        source: str,
    ) -> list[dict[str, object]]:
        normalized = normalize_lark_content(message_type, content, raw)
        return [
            self._lark_image_entry(message_id=message_id, image=image, source=source)
            for image in normalized.images
        ]

    def _lark_image_entry(self, *, message_id: str, image: LarkImageAttachment, source: str) -> dict[str, object]:
        entry: dict[str, object] = {
            "message_id": message_id,
            "file_key": image.file_key,
            "placeholder": image.placeholder,
            "source": source,
        }
        if image.width:
            entry["width"] = image.width
        if image.height:
            entry["height"] = image.height
        return entry

    async def _lark_chat_name(self, event: LarkEvent) -> str:
        if event.chat_name:
            return event.chat_name
        if event.chat_type and event.chat_type != "group":
            return ""
        if not event.chat_id or self.lark_client is None:
            return ""
        get_chat_name = getattr(self.lark_client, "get_chat_name", None)
        if get_chat_name is None:
            return ""
        try:
            return await asyncio.to_thread(get_chat_name, event.chat_id)
        except Exception as exc:
            self.db.log_event(
                "lark",
                "chat_name.lookup_failed",
                "failed to resolve lark chat name",
                chat_id=event.chat_id,
                error=str(exc),
            )
            return ""

    async def _prepare_lark_codex_input(self, slot: Slot) -> tuple[str, list[CodexInputAttachment]]:
        if not slot.lark_message_id:
            return slot.incoming_text, []
        images = self.db.get_slot_raw(slot.slot_id).get(LARK_SLOT_IMAGES_KEY)
        if not isinstance(images, list) or not images:
            return slot.incoming_text, []
        download_image = getattr(self.lark_client, "download_message_image", None) if self.lark_client is not None else None
        if download_image is None:
            self.db.log_event(
                "lark",
                "image.download_unavailable",
                "lark client does not support message image download",
                slot.slot_id,
                image_count=len(images),
            )
        attachments: list[CodexInputAttachment] = []
        manifest_lines: list[str] = []
        image_dir = self.config.db_path.parent / "lark-images" / slot.slot_id
        for index, item in enumerate(images[:LARK_CODEX_IMAGE_LIMIT], start=1):
            if not isinstance(item, dict):
                continue
            file_key = str(item.get("file_key") or "").strip()
            message_id = str(item.get("message_id") or slot.lark_message_id or "").strip()
            if not file_key or not message_id:
                continue
            placeholder = str(item.get("placeholder") or f"[Image: {file_key}]")
            size = self._lark_image_size(item)
            source = str(item.get("source") or "")
            line = f"- Image {index}: source={source} message_id={message_id} file_key={file_key}{size} placeholder={placeholder}"
            if download_image is None:
                manifest_lines.append(f"{line} status=unavailable")
                continue
            destination = image_dir / f"{index:02d}-{_safe_lark_file_fragment(file_key)}.bin"
            try:
                path = await asyncio.to_thread(download_image, message_id=message_id, file_key=file_key, destination=destination)
            except Exception as exc:
                self.db.log_event(
                    "lark",
                    "image.download_failed",
                    "failed to download lark image for codex input",
                    slot.slot_id,
                    message_id=message_id,
                    file_key=file_key,
                    error=str(exc),
                )
                manifest_lines.append(f"{line} status=unavailable error={str(exc)[:160]}")
                continue
            attachments.append(CodexInputAttachment(path=path, detail="original"))
            manifest_lines.append(f"{line} local_path={path}")
        if len(images) > LARK_CODEX_IMAGE_LIMIT:
            manifest_lines.append(f"- skipped {len(images) - LARK_CODEX_IMAGE_LIMIT} extra image(s) due to limit={LARK_CODEX_IMAGE_LIMIT}")
        if not manifest_lines:
            return slot.incoming_text, attachments
        attachment_note = "\n".join(
            [
                "",
                "飞书图片附件（按列表顺序附加为 Codex localImage 输入；不可用项仅保留文字占位）:",
                *manifest_lines,
            ]
        )
        return f"{slot.incoming_text}\n{attachment_note}", attachments

    def _lark_image_size(self, item: dict[str, object]) -> str:
        width = item.get("width")
        height = item.get("height")
        return f" size={width}x{height}" if width and height else ""

    async def _run_codex_for_slot(self, slot: Slot) -> None:
        last_error: str | None = None
        case = self.db.get_case(slot.case_id) if slot.case_id else None
        jira_marker = self._jira_analysis_marker(slot.slot_id)
        github_marker = self._github_analysis_marker(slot.slot_id)
        plan = self.slot_planner.plan(slot, case, jira_marker=jira_marker, github_marker=github_marker)
        prompt_context = plan.prompt_context
        incoming_text, input_attachments = await self._prepare_lark_codex_input(slot)
        attempts = max(self.config.codex.max_reply_retries + 1, 2 if plan.resume_thread_id else 1)
        resume_failed = False
        context_compacted = False
        retry_after_compact = False
        retry_prompt_after_compact: str | None = None
        current_thread_id = slot.codex_thread_id or plan.resume_thread_id
        current_turn_id = slot.codex_turn_id
        attempt = 0
        while attempt < attempts:
            attempt += 1
            if attempt > 1 and is_direct_jira_analysis_source(slot.source) and await self._complete_jira_analysis_if_marker_exists(
                slot,
                jira_marker,
                attempt=attempt,
            ):
                return
            resume_thread_id: str | None = None
            if attempt == 1 and slot.slot_id in self._recovered_running_slot_ids:
                retry_prompt = "继续"
                resume_thread_id = current_thread_id
                self.db.log_event(
                    "codex",
                    "turn.continue_after_restart",
                    "continuing recovered running slot",
                    slot.slot_id,
                    thread_id=resume_thread_id,
                )
                self._recovered_running_slot_ids.discard(slot.slot_id)
            elif retry_after_compact:
                retry_prompt = retry_prompt_after_compact
                resume_thread_id = current_thread_id or plan.resume_thread_id
                retry_after_compact = False
                retry_prompt_after_compact = None
            elif resume_failed:
                retry_prompt = None
                resume_thread_id = None
            elif attempt == 1:
                retry_prompt = None
                resume_thread_id = plan.resume_thread_id
            elif slot.source == JIRA_ISSUE_AUTO_ANALYZE_SOURCE:
                retry_prompt = self.codex_client.jira_issue_auto_analysis_retry_prompt(slot.slot_id, prompt_context)
                resume_thread_id = current_thread_id or plan.resume_thread_id
            elif slot.source == JIRA_STATUS_SUMMARY_SOURCE:
                retry_prompt = self.codex_client.jira_status_summary_retry_prompt(slot.slot_id, prompt_context)
                resume_thread_id = current_thread_id or plan.resume_thread_id
            elif is_direct_jira_analysis_source(slot.source):
                retry_prompt = self.codex_client.jira_analysis_retry_prompt(slot.slot_id, prompt_context)
                resume_thread_id = current_thread_id or plan.resume_thread_id
            elif slot.source == LARK_JIRA_ANALYZE_SOURCE:
                retry_prompt = self.codex_client.lark_jira_analysis_retry_prompt(slot.slot_id, prompt_context)
                resume_thread_id = current_thread_id or plan.resume_thread_id
            elif slot.source == GITHUB_ISSUE_ANALYZE_SOURCE:
                retry_prompt = self.codex_client.github_issue_analysis_retry_prompt(slot.slot_id, prompt_context)
                resume_thread_id = current_thread_id or plan.resume_thread_id
            else:
                retry_prompt = self.codex_client.retry_prompt(slot.slot_id)
                resume_thread_id = current_thread_id or plan.resume_thread_id
            self.db.mark_codex_started(slot.slot_id, attempt)
            self.db.log_event("codex", "turn.started", "codex attempt started", slot.slot_id, attempt=attempt)
            LOG.info("starting codex attempt", extra={"component": "codex", "slot_id": slot.slot_id})

            async def record_session(thread_id: str, turn_id: str | None) -> None:
                nonlocal current_thread_id, current_turn_id
                current_thread_id = thread_id
                if turn_id:
                    current_turn_id = turn_id
                self.db.mark_codex_session(slot.slot_id, thread_id, turn_id)
                self.db.log_event(
                    "codex",
                    "session.started" if turn_id is None else "turn.session_bound",
                    "codex session id available" if turn_id is None else "codex turn id available",
                    slot.slot_id,
                    attempt=attempt,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )

            try:
                run_kwargs = {
                    "slot_id": slot.slot_id,
                    "incoming_text": incoming_text,
                    "retry_prompt": retry_prompt,
                    "prompt_name": plan.prompt_name,
                    "prompt_context": prompt_context,
                    "on_session_started": record_session,
                    "resume_thread_id": resume_thread_id,
                }
                if input_attachments:
                    run_kwargs["input_attachments"] = input_attachments
                result = await self.codex_client.run_slot(
                    **run_kwargs,
                )
                self.db.mark_codex_turn(slot.slot_id, result.thread_id, result.turn_id)
                current_thread_id = result.thread_id
                current_turn_id = result.turn_id
                self.db.log_event(
                    "codex",
                    "turn.completed",
                    "codex turn completed",
                    slot.slot_id,
                    attempt=attempt,
                    thread_id=result.thread_id,
                    turn_id=result.turn_id,
                    status=result.status,
                    duration_ms=result.duration_ms,
                    answer=result.answer,
                )
                LOG.info(
                    "codex turn completed",
                    extra={"component": "codex", "slot_id": slot.slot_id, "thread_id": result.thread_id, "turn_id": result.turn_id},
                )
                if result.status != "completed":
                    failure = await self._handle_failed_codex_result(
                        slot,
                        result,
                        jira_marker=jira_marker,
                        attempt=attempt,
                        context_compacted=context_compacted,
                    )
                    if failure.completed:
                        return
                    last_error = failure.error
                    if failure.retry_same_prompt:
                        context_compacted = True
                        retry_after_compact = True
                        retry_prompt_after_compact = retry_prompt
                        current_thread_id = result.thread_id
                        attempts = max(attempts, attempt + 1)
                        continue
                    if failure.stop:
                        break
                    continue

                completion = await self._verify_completed_codex_turn_delivery(
                    slot,
                    result,
                    jira_marker=jira_marker,
                    github_marker=github_marker,
                    attempt=attempt,
                )
                if completion.completed:
                    return
                last_error = completion.error
            except Exception as exc:
                last_error = str(exc)
                handling = await self._handle_codex_exception(
                    slot,
                    error=last_error,
                    attempt=attempt,
                    jira_marker=jira_marker,
                    thread_id=current_thread_id,
                    turn_id=current_turn_id,
                )
                if handling.completed:
                    return
                last_error = handling.error or last_error
                self.db.log_event("codex", "turn.failed", "codex turn failed", slot.slot_id, attempt=attempt, error=last_error)
                LOG.exception("codex attempt failed", extra={"component": "codex", "slot_id": slot.slot_id})
                if not handling.retry_allowed:
                    break
                if resume_thread_id and not resume_failed:
                    resume_failed = True
                    current_thread_id = None
                    current_turn_id = None
                    self.db.log_event(
                        "codex",
                        "thread.recreate_after_resume_failed",
                        "resume failed; retrying in a new Codex thread with case context",
                        slot.slot_id,
                        failed_thread_id=resume_thread_id,
                        error=last_error,
                    )
        self.db.mark_codex_failed(slot.slot_id, last_error or "codex failed")

    async def _reply_sender_loop(self) -> None:
        while True:
            await self._send_pending_replies_once()
            await asyncio.sleep(self.config.reply_poll_interval)

    async def _send_pending_replies_once(self) -> None:
        for slot in self.db.pending_replies():
            if is_direct_jira_analysis_source(slot.source):
                self.db.log_event(
                    "jira",
                    "analysis.breakwater_reply_ignored",
                    "jira analysis slots must be completed by a Jira comment with the slot marker",
                    slot.slot_id,
                )
                continue
            if slot.source == GITHUB_ISSUE_ANALYZE_SOURCE:
                self.db.log_event(
                    "github",
                    "analysis.breakwater_reply_ignored",
                    "github analysis slots must be completed by a GitHub comment with the slot marker",
                    slot.slot_id,
                )
                continue
            if not slot.lark_message_id:
                self.db.mark_reply_sent(slot.slot_id, None)
                self.db.log_event("reply", "reply.captured", "captured local reply without lark target", slot.slot_id)
                continue
            if self.lark_client is None:
                LOG.warning("lark client is not ready; cannot send reply", extra={"component": "reply", "slot_id": slot.slot_id})
                continue
            text = self._format_lark_reply(slot)
            try:
                reply_message_id = await asyncio.to_thread(self.lark_client.reply_text, slot.lark_message_id, text)
                self.db.mark_reply_sent(slot.slot_id, reply_message_id)
                self.db.log_event(
                    "lark",
                    "message.replied",
                    "sent lark reply",
                    slot.slot_id,
                    lark_message_id=slot.lark_message_id,
                    reply_message_id=reply_message_id,
                )
                LOG.info("sent lark reply", extra={"component": "reply", "slot_id": slot.slot_id})
            except Exception as exc:
                error = str(exc)
                if is_permanent_lark_reply_error(error):
                    self.db.mark_reply_failed(slot.slot_id, error)
                    self.db.log_event(
                        "lark",
                        "message.reply_permanent_failed",
                        "lark reply target is no longer replyable; stopped retrying",
                        slot.slot_id,
                        lark_message_id=slot.lark_message_id,
                        error=error,
                    )
                else:
                    self.db.log_event("lark", "message.reply_failed", "failed to send lark reply", slot.slot_id, error=error)
                LOG.exception("failed to send lark reply", extra={"component": "reply", "slot_id": slot.slot_id})

    async def _handle_failed_codex_result(
        self,
        slot: Slot,
        result: CodexRunResult,
        *,
        jira_marker: str,
        attempt: int,
        context_compacted: bool,
    ) -> _TurnFailureHandling:
        if is_direct_jira_analysis_source(slot.source) and await self._complete_jira_analysis_if_marker_exists(slot, jira_marker, attempt=attempt):
            return _TurnFailureHandling(completed=True)

        error = self._codex_result_error(result)
        self.db.log_event(
            "codex",
            "turn.failed_status",
            "codex turn ended without completed status",
            slot.slot_id,
            attempt=attempt,
            thread_id=result.thread_id,
            turn_id=result.turn_id,
            status=result.status,
            error=error,
            error_info=result.error_info,
        )
        if not self._is_context_window_exceeded(result):
            return _TurnFailureHandling(error=error)

        self.db.log_event(
            "codex",
            "turn.context_window_exceeded",
            "codex turn exceeded the model context window",
            slot.slot_id,
            attempt=attempt,
            thread_id=result.thread_id,
            turn_id=result.turn_id,
            error=error,
        )
        if not result.thread_id or context_compacted:
            return _TurnFailureHandling(stop=True, error=error)

        compact_error = await self._compact_codex_thread_after_context_window(slot, result.thread_id, attempt, error)
        if compact_error:
            return _TurnFailureHandling(stop=True, error=compact_error)
        return _TurnFailureHandling(retry_same_prompt=True, error=error)

    async def _verify_completed_codex_turn_delivery(
        self,
        slot: Slot,
        result: CodexRunResult,
        *,
        jira_marker: str,
        github_marker: str,
        attempt: int,
    ) -> _CompletionVerification:
        if is_direct_jira_analysis_source(slot.source):
            if await self._complete_jira_analysis_if_marker_exists(slot, jira_marker, attempt=attempt):
                return _CompletionVerification(completed=True)
            error = f"codex completed without Jira comment marker {jira_marker}"
            self.db.log_event("jira", "analysis.comment_missing", error, slot.slot_id, attempt=attempt, marker=jira_marker)
            return _CompletionVerification(completed=False, error=error)

        if slot.source == LARK_JIRA_ANALYZE_SOURCE:
            return await self._verify_lark_jira_analysis_delivery(slot, result, jira_marker=jira_marker, attempt=attempt)

        if slot.source == GITHUB_ISSUE_ANALYZE_SOURCE:
            comment_id = await self._find_github_analysis_comment(slot, github_marker)
            if comment_id:
                self.db.mark_reply_sent(slot.slot_id, comment_id)
                self.db.mark_codex_completed(slot.slot_id)
                self._update_case_summary(slot.slot_id, f"verified GitHub comment {comment_id}")
                self.db.log_event(
                    "github",
                    "analysis.comment_verified",
                    f"verified GitHub analysis comment on {slot.github_repo}#{slot.github_issue_number}",
                    slot.slot_id,
                    repo_full_name=slot.github_repo,
                    issue_number=slot.github_issue_number,
                    reply_comment_id=comment_id,
                    marker=github_marker,
                )
                return _CompletionVerification(completed=True)
            error = f"codex completed without GitHub comment marker {github_marker}"
            self.db.log_event("github", "analysis.comment_missing", error, slot.slot_id, attempt=attempt, marker=github_marker)
            return _CompletionVerification(completed=False, error=error)

        if self.db.has_reply(slot.slot_id):
            self.db.mark_codex_completed(slot.slot_id)
            replied_slot = self.db.get_slot(slot.slot_id)
            self._update_case_summary(slot.slot_id, (replied_slot.reply_text if replied_slot else None) or result.answer)
            return _CompletionVerification(completed=True)

        error = "codex completed without calling breakwater reply"
        self.db.log_event("codex", "reply.missing", error, slot.slot_id, attempt=attempt)
        return _CompletionVerification(completed=False, error=error)

    async def _verify_lark_jira_analysis_delivery(
        self,
        slot: Slot,
        result: CodexRunResult,
        *,
        jira_marker: str,
        attempt: int,
    ) -> _CompletionVerification:
        comment = await self._find_jira_analysis_comment(slot, jira_marker)
        has_lark_reply = self.db.has_reply(slot.slot_id)
        if comment and has_lark_reply:
            self.db.mark_codex_completed(slot.slot_id)
            replied_slot = self.db.get_slot(slot.slot_id)
            outcome = (replied_slot.reply_text if replied_slot else None) or result.answer
            self._update_case_summary(slot.slot_id, f"verified Jira comment {comment.comment_id}; lark reply: {outcome}")
            self.db.log_event(
                "jira",
                "lark_analysis.dual_delivery_verified",
                f"verified Jira analysis comment and Lark reply for {slot.jira_issue_key}",
                slot.slot_id,
                issue_key=slot.jira_issue_key,
                reply_comment_id=comment.comment_id,
                marker=jira_marker,
                lark_reply_pending_or_sent=has_lark_reply,
            )
            return _CompletionVerification(completed=True)

        missing = []
        if not comment:
            missing.append(f"Jira comment marker {jira_marker}")
        if not has_lark_reply:
            missing.append("Breakwater Lark reply")
        error = "codex completed without " + " and ".join(missing)
        self.db.log_event(
            "jira",
            "lark_analysis.delivery_missing",
            error,
            slot.slot_id,
            attempt=attempt,
            marker=jira_marker,
            has_lark_reply=has_lark_reply,
        )
        return _CompletionVerification(completed=False, error=error)

    async def _handle_codex_exception(
        self,
        slot: Slot,
        *,
        error: str,
        attempt: int,
        jira_marker: str,
        thread_id: str | None,
        turn_id: str | None,
    ) -> _CodexExceptionHandling:
        if not is_direct_jira_analysis_source(slot.source):
            return _CodexExceptionHandling(error=error)
        if await self._complete_jira_analysis_if_marker_exists(slot, jira_marker, attempt=attempt, codex_error=error):
            self.db.log_event(
                "codex",
                "turn.error_after_jira_marker_verified",
                "codex turn errored after the Jira marker had been written",
                slot.slot_id,
                attempt=attempt,
                error=error,
            )
            LOG.warning(
                "codex attempt errored after jira marker was verified",
                exc_info=True,
                extra={"component": "codex", "slot_id": slot.slot_id},
            )
            return _CodexExceptionHandling(completed=True)
        if not thread_id or not turn_id:
            return _CodexExceptionHandling(error=error)
        return await self._wait_for_jira_marker_or_codex_turn_terminal(
            slot,
            jira_marker,
            attempt=attempt,
            codex_error=error,
            thread_id=thread_id,
            turn_id=turn_id,
        )

    async def _wait_for_jira_marker_or_codex_turn_terminal(
        self,
        slot: Slot,
        marker: str,
        *,
        attempt: int,
        codex_error: str,
        thread_id: str,
        turn_id: str,
    ) -> _CodexExceptionHandling:
        if getattr(self.codex_client, "read_turn", None) is None:
            self.db.log_event(
                "codex",
                "turn.reconcile_unavailable",
                "codex client does not support turn readback after transport errors",
                slot.slot_id,
                attempt=attempt,
                thread_id=thread_id,
                turn_id=turn_id,
                error=codex_error,
            )
            return _CodexExceptionHandling(error=codex_error)
        deadline = time.monotonic() + max(float(self.config.codex.turn_timeout_seconds), 0.0)
        poll_interval = min(max(float(self.config.reply_poll_interval), 0.1), 10.0)
        last_status = "unknown"
        last_error = codex_error
        while True:
            if await self._complete_jira_analysis_if_marker_exists(slot, marker, attempt=attempt, codex_error=codex_error):
                return _CodexExceptionHandling(completed=True)
            snapshot = await self._read_codex_turn_snapshot(slot, thread_id, turn_id, attempt=attempt, codex_error=codex_error)
            if snapshot is None:
                last_status = "unknown"
            else:
                last_status = snapshot.status
                if snapshot.error_message:
                    last_error = f"{snapshot.error_info}: {snapshot.error_message}" if snapshot.error_info else snapshot.error_message
                if snapshot.status == "completed":
                    self.db.log_event(
                        "codex",
                        "turn.transport_error_completed_without_marker",
                        "codex turn completed after a transport error but did not write the Jira marker",
                        slot.slot_id,
                        attempt=attempt,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        marker=marker,
                    )
                    return _CodexExceptionHandling(
                        retry_allowed=True,
                        error=f"codex turn completed after transport error without Jira marker {marker}",
                    )
                if snapshot.status in {"failed", "interrupted"}:
                    self.db.log_event(
                        "codex",
                        "turn.transport_error_terminal",
                        "codex turn reached a terminal status after a transport error; retry is allowed",
                        slot.slot_id,
                        attempt=attempt,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        status=snapshot.status,
                        error=last_error,
                    )
                    return _CodexExceptionHandling(retry_allowed=True, error=last_error)
                self.db.log_event(
                    "codex",
                    "turn.transport_error_still_running",
                    "codex transport failed but the original turn is still running; waiting before retry",
                    slot.slot_id,
                    attempt=attempt,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    status=snapshot.status,
                )
            if time.monotonic() >= deadline:
                message = (
                    "codex transport failed and the original turn was not confirmed terminal "
                    f"before timeout; last_status={last_status}"
                )
                self.db.log_event(
                    "codex",
                    "turn.transport_error_unconfirmed",
                    message,
                    slot.slot_id,
                    attempt=attempt,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    status=last_status,
                    error=last_error,
                )
                return _CodexExceptionHandling(retry_allowed=False, error=message)
            await asyncio.sleep(poll_interval)

    async def _read_codex_turn_snapshot(
        self,
        slot: Slot,
        thread_id: str,
        turn_id: str,
        *,
        attempt: int,
        codex_error: str,
    ) -> CodexTurnSnapshot | None:
        read_turn = getattr(self.codex_client, "read_turn", None)
        if read_turn is None:
            self.db.log_event(
                "codex",
                "turn.reconcile_unavailable",
                "codex client does not support turn readback after transport errors",
                slot.slot_id,
                attempt=attempt,
                thread_id=thread_id,
                turn_id=turn_id,
                error=codex_error,
            )
            return None
        try:
            snapshot = await read_turn(thread_id, turn_id)
        except Exception as exc:
            self.db.log_event(
                "codex",
                "turn.reconcile_failed",
                "failed to read codex turn state after transport error",
                slot.slot_id,
                attempt=attempt,
                thread_id=thread_id,
                turn_id=turn_id,
                error=str(exc),
                codex_error=codex_error,
            )
            LOG.exception("failed to read codex turn state", extra={"component": "codex", "slot_id": slot.slot_id})
            return None
        if snapshot is None:
            self.db.log_event(
                "codex",
                "turn.reconcile_missing",
                "codex turn was not found during transport-error reconciliation",
                slot.slot_id,
                attempt=attempt,
                thread_id=thread_id,
                turn_id=turn_id,
            )
        return snapshot

    async def _complete_jira_analysis_if_marker_exists(
        self,
        slot: Slot,
        marker: str,
        *,
        attempt: int,
        codex_error: str | None = None,
    ) -> bool:
        check_data: dict[str, object] = {
            "issue_key": slot.jira_issue_key,
            "trigger_comment_id": slot.jira_comment_id,
            "marker": marker,
            "attempt": attempt,
            "after_codex_error": bool(codex_error),
        }
        if codex_error:
            check_data["codex_error"] = codex_error
        self.db.log_event(
            "jira",
            "analysis.marker_check_started",
            f"checking Jira analysis marker on {slot.jira_issue_key}",
            slot.slot_id,
            **check_data,
        )
        comment = await self._find_jira_analysis_comment(slot, marker)
        if not comment:
            self.db.log_event(
                "jira",
                "analysis.marker_not_found",
                f"Jira analysis marker not found on {slot.jira_issue_key}",
                slot.slot_id,
                **check_data,
            )
            return False
        if requires_jira_automation_report(slot.source):
            self.db.record_jira_automation_comment(slot, comment.comment_id)
            if not self.db.jira_automation_report_ready(slot.slot_id):
                record = self.db.get_jira_automation_record(slot.slot_id)
                self.db.log_event(
                    "jira",
                    "automation.report_missing",
                    f"Jira comment marker exists but automation report is missing for {slot.jira_issue_key}",
                    slot.slot_id,
                    issue_key=slot.jira_issue_key,
                    reply_comment_id=comment.comment_id,
                    marker=marker,
                    report_status=record.report_status if record else "missing",
                    report_error=record.error if record else None,
                    attempt=attempt,
                )
                return False
        self.db.mark_reply_sent(slot.slot_id, comment.comment_id)
        self.db.mark_codex_completed(slot.slot_id)
        self._update_case_summary(slot.slot_id, f"verified Jira comment {comment.comment_id}")
        event_data: dict[str, object] = {
            "issue_key": slot.jira_issue_key,
            "trigger_comment_id": slot.jira_comment_id,
            "reply_comment_id": comment.comment_id,
            "marker": marker,
            "attempt": attempt,
        }
        if codex_error:
            event_data["codex_error"] = codex_error
        self.db.log_event(
            "jira",
            "analysis.comment_verified",
            f"verified Jira analysis comment on {slot.jira_issue_key}",
            slot.slot_id,
            **event_data,
        )
        return True

    async def _compact_codex_thread_after_context_window(self, slot: Slot, thread_id: str, attempt: int, error: str) -> str | None:
        compact_thread = getattr(self.codex_client, "compact_thread", None)
        if compact_thread is None:
            message = "codex client does not support thread compact after context window exceeded"
            self.db.log_event("codex", "thread.compact_unavailable", message, slot.slot_id, attempt=attempt, thread_id=thread_id, error=error)
            return message
        self.db.log_event(
            "codex",
            "thread.compact_after_context_window_exceeded",
            "context window exceeded; compacting the same Codex thread before retrying",
            slot.slot_id,
            attempt=attempt,
            thread_id=thread_id,
            error=error,
        )
        try:
            result = await compact_thread(thread_id)
        except Exception as exc:
            message = f"context compact failed: {exc}"
            self.db.log_event("codex", "thread.compact_failed", message, slot.slot_id, attempt=attempt, thread_id=thread_id, error=str(exc))
            LOG.exception("codex thread compact failed", extra={"component": "codex", "slot_id": slot.slot_id, "thread_id": thread_id})
            return message
        error_message = getattr(result, "error_message", None)
        error_info = getattr(result, "error_info", None)
        if getattr(result, "status", None) != "completed":
            message = f"context compact ended with status {getattr(result, 'status', 'unknown')}"
            if error_message:
                message = f"{error_info}: {error_message}" if error_info else str(error_message)
            self.db.log_event(
                "codex",
                "thread.compact_failed_status",
                message,
                slot.slot_id,
                attempt=attempt,
                thread_id=getattr(result, "thread_id", thread_id),
                turn_id=getattr(result, "turn_id", None),
                status=getattr(result, "status", None),
                error_info=error_info,
            )
            return message
        self.db.log_event(
            "codex",
            "thread.compact_completed",
            "compacted the same Codex thread after context window exceeded",
            slot.slot_id,
            attempt=attempt,
            thread_id=getattr(result, "thread_id", thread_id),
            turn_id=getattr(result, "turn_id", None),
            duration_ms=getattr(result, "duration_ms", None),
        )
        return None

    def _codex_result_error(self, result: CodexRunResult) -> str:
        if result.error_message:
            if result.error_info:
                return f"{result.error_info}: {result.error_message}"
            return result.error_message
        return f"codex turn ended with status {result.status}"

    def _is_context_window_exceeded(self, result: CodexRunResult) -> bool:
        error_info = (result.error_info or "").lower()
        error_message = (result.error_message or "").lower()
        return error_info == "contextwindowexceeded" or "context window" in error_message

    async def _find_jira_analysis_comment(self, slot: Slot, marker: str) -> _JiraMarkerComment | None:
        if not slot.jira_issue_key:
            self.db.log_event("jira", "analysis.verify_missing_issue", "cannot verify Jira analysis without issue key", slot.slot_id)
            return None
        if not self.config.jira.enabled and self.jira_client is None:
            self.db.log_event("jira", "analysis.verify_skipped", "Jira verification disabled", slot.slot_id)
            return None
        try:
            client = self._get_jira_client()
            comments = await asyncio.to_thread(client.list_comments, slot.jira_issue_key)
        except Exception as exc:
            self.db.log_event("jira", "analysis.verify_failed", "failed to verify Jira analysis comment", slot.slot_id, error=str(exc))
            LOG.exception("failed to verify jira analysis comment", extra={"component": "jira", "slot_id": slot.slot_id})
            return None
        for comment in comments:
            body = str(comment.get("body") or "")
            if marker in body:
                comment_id = comment.get("id")
                return _JiraMarkerComment(str(comment_id) if comment_id is not None else "", body)
        return None

    async def _find_github_analysis_comment(self, slot: Slot, marker: str) -> str | None:
        if not slot.github_repo or not slot.github_issue_number:
            self.db.log_event("github", "analysis.verify_missing_issue", "cannot verify GitHub analysis without repo and issue number", slot.slot_id)
            return None
        try:
            client = self._get_github_client()
            comments = await asyncio.to_thread(client.list_issue_comments, slot.github_repo, slot.github_issue_number)
        except Exception as exc:
            self.db.log_event("github", "analysis.verify_failed", "failed to verify GitHub analysis comment", slot.slot_id, error=str(exc))
            LOG.exception("failed to verify github analysis comment", extra={"component": "github", "slot_id": slot.slot_id})
            return None
        for comment in comments:
            body = str(comment.get("body") or "")
            if marker in body:
                comment_id = comment.get("id")
                return str(comment_id) if comment_id is not None else ""
        return None

    def _jira_analysis_marker(self, slot_id: str) -> str:
        return f"{self.config.jira.analysis_marker_prefix}: {slot_id}"

    def _github_analysis_marker(self, slot_id: str) -> str:
        return f"{self.config.github.analysis_marker_prefix}: {slot_id}"

    def _get_jira_client(self) -> JiraRestClient:
        if self.jira_client is None:
            self.jira_client = JiraRestClient(
                self.config.jira_skill_dir,
                page_size=self.config.jira.page_size,
                token=self.config.jira.token,
            )
        return self.jira_client

    def _get_github_client(self) -> GitHubRestClient:
        if self.github_client is None:
            self.github_client = GitHubRestClient(
                token=self.config.github.token,
                api_url=self.config.github.api_url,
                page_size=self.config.github.page_size,
            )
        return self.github_client

    def _format_lark_reply(self, slot: Slot) -> str:
        assert slot.reply_text is not None
        text = slot.reply_text
        if slot.case_id:
            case = self.db.get_case(slot.case_id)
            if case:
                text = f"{text}\n\n{build_case_anchor(case, slot)}"
        if slot.chat_type == "group" and slot.sender_id:
            return f"{at_user_text(slot.sender_id)} {text}".strip()
        return text

    def _update_case_summary(self, slot_id: str, outcome: str | None) -> None:
        slot = self.db.get_slot(slot_id)
        if not slot or not slot.case_id:
            return
        case = self.db.get_case(slot.case_id)
        if not case:
            return
        self.db.update_case_summary(case.case_id, build_case_summary(case, slot, outcome=outcome))
