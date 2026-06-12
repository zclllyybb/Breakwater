from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from inspect import isawaitable
from pathlib import Path
from typing import Any, Awaitable, Callable

import websockets

from .config import CodexConfig
from .prompts import PromptLibrary


LOG = logging.getLogger(__name__)
SessionCallback = Callable[[str, str | None], Awaitable[None] | None]


@dataclass(frozen=True)
class CodexInputAttachment:
    path: Path
    kind: str = "image"
    detail: str | None = "original"


@dataclass(frozen=True)
class CodexRunResult:
    thread_id: str
    turn_id: str
    status: str
    answer: str
    duration_ms: int | None
    error_message: str | None = None
    error_info: str | None = None


@dataclass(frozen=True)
class CodexCompactResult:
    thread_id: str
    turn_id: str | None
    status: str
    duration_ms: int | None
    error_message: str | None = None
    error_info: str | None = None


@dataclass(frozen=True)
class CodexTurnSnapshot:
    thread_id: str
    turn_id: str
    status: str
    error_message: str | None = None
    error_info: str | None = None


async def notify_session_started(callback: SessionCallback | None, thread_id: str, turn_id: str | None) -> None:
    if callback is None:
        return
    result = callback(thread_id, turn_id)
    if isawaitable(result):
        await result


class CodexAppServerManager:
    def __init__(self, config: CodexConfig, env_overrides: dict[str, str] | None = None):
        self.config = config
        self.env_overrides = env_overrides or {}
        self._process: asyncio.subprocess.Process | None = None
        self._log_task: asyncio.Task[None] | None = None
        self._owned = False

    @property
    def owned(self) -> bool:
        return self._owned

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process and self._process.returncode is None else None

    async def start(self) -> None:
        if await self._is_ready():
            if self.env_overrides:
                LOG.warning("codex app-server already ready; configured environment overrides were not injected", extra={"component": "codex"})
            LOG.info("codex app-server already ready", extra={"component": "codex"})
            return
        if not self.config.start_server:
            raise RuntimeError(f"codex app-server is not ready at {self.config.ws_url}")

        command = ["codex", "app-server", "--listen", self.config.ws_url]
        LOG.info("starting codex app-server", extra={"component": "codex"})
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, **self.env_overrides},
        )
        self._owned = True
        assert self._process.stdout is not None
        self._log_task = asyncio.create_task(self._log_output(self._process.stdout), name="codex-app-server-log")

        for _ in range(100):
            if await self._is_ready():
                LOG.info("codex app-server ready", extra={"component": "codex"})
                return
            if self._process.returncode is not None:
                raise RuntimeError(f"codex app-server exited early code={self._process.returncode}")
            await asyncio.sleep(0.1)
        raise TimeoutError(f"codex app-server did not become ready: {self.config.ws_url}")

    async def stop(self) -> None:
        if not self._owned or self._process is None:
            return
        LOG.info("stopping codex app-server", extra={"component": "codex"})
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._log_task is not None:
            with suppress(Exception):
                await asyncio.wait_for(self._log_task, timeout=2)
        transport = getattr(self._process, "_transport", None)
        if transport is not None:
            with suppress(Exception):
                transport.close()
        self._process = None
        self._log_task = None
        self._owned = False

    async def _log_output(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            LOG.info("codex-app-server: %s", line.decode("utf-8", errors="replace").rstrip(), extra={"component": "codex"})

    async def _is_ready(self) -> bool:
        try:
            return await asyncio.to_thread(is_codex_app_server_ready, self.config.ws_url)
        except Exception:
            return False


def codex_ready_url(ws_url: str) -> str:
    return ws_url.replace("ws://", "http://", 1).replace("wss://", "https://", 1).rstrip("/") + "/readyz"


def is_codex_app_server_ready(ws_url: str) -> bool:
    return _url_ok(codex_ready_url(ws_url))


def _url_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError):
        return False


class CodexAppServerClient:
    def __init__(
        self,
        config: CodexConfig,
        project_root: Path,
        workspace: Path,
        db_path: Path,
        jira_skill_dir: Path,
        develop_skill_dir: Path,
        github_api_url: str = "https://api.github.com",
        develop_enabled: bool = True,
        prompt_variables: dict[str, str] | None = None,
        task_env: dict[str, str] | None = None,
    ):
        self.config = config
        self.project_root = project_root
        self.workspace = workspace
        self.db_path = db_path
        self.jira_skill_dir = jira_skill_dir
        self.develop_skill_dir = develop_skill_dir
        self.github_api_url = github_api_url
        self.develop_enabled = develop_enabled
        base_prompt_variables = {
            "PROJECT_ROOT": str(project_root),
            "CODEX_WORKSPACE": str(workspace),
            "codex_workspace": str(workspace),
            "DEVELOP_DEFAULT_REPOSITORY": str(workspace),
            "DEVELOP_SKILL_DIR": str(develop_skill_dir),
            "DEVELOP_SKILL_PATH": str(develop_skill_dir / "SKILL.md"),
            "JIRA_SKILL_DIR": str(jira_skill_dir),
            "JIRA_SKILL_PATH": str(jira_skill_dir / "SKILL.md"),
        }
        base_prompt_variables.update(prompt_variables or {})
        self.prompts = PromptLibrary(variables=base_prompt_variables)
        self.task_env = {key: value for key, value in (task_env or {}).items() if value}
        self._next_id = 1

    async def run_slot(
        self,
        *,
        slot_id: str,
        incoming_text: str,
        input_attachments: list[CodexInputAttachment] | None = None,
        retry_prompt: str | None = None,
        prompt_name: str = "lark_initial",
        prompt_context: dict[str, object] | None = None,
        on_session_started: SessionCallback | None = None,
        resume_thread_id: str | None = None,
    ) -> CodexRunResult:
        answer_parts: list[str] = []
        turn_errors: dict[str, dict[str, Any]] = {}
        thread_id = ""
        turn_id = ""
        prompt = retry_prompt or self._initial_prompt(slot_id, incoming_text, prompt_name, prompt_context or {})
        async with websockets.connect(self.config.ws_url) as websocket:
            await self._request(
                websocket,
                "initialize",
                {
                    "clientInfo": {"name": "breakwater", "version": "0.1.0", "title": "Breakwater"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await websocket.send(json.dumps({"method": "initialized"}))

            if resume_thread_id:
                thread_start = await self._request(websocket, "thread/resume", self._thread_resume_params(resume_thread_id))
            else:
                thread_start = await self._request(websocket, "thread/start", self._thread_start_params())
            thread_id = thread_start["thread"]["id"]
            await notify_session_started(on_session_started, thread_id, None)

            input_items = self._input_items(prompt, prompt_name, input_attachments=input_attachments)
            turn_params = self._turn_start_params(thread_id, input_items)

            try:
                turn_start = await self._request(websocket, "turn/start", turn_params)
            except RuntimeError as exc:
                if "skill" not in str(exc):
                    raise
                LOG.warning("turn/start with skill inputs failed; retrying with text-only prompt", extra={"component": "codex"})
                turn_params["input"] = self._prompt_input_items(prompt, input_attachments=input_attachments)
                turn_start = await self._request(websocket, "turn/start", turn_params)
            turn_id = turn_start["turn"]["id"]
            await notify_session_started(on_session_started, thread_id, turn_id)

            completed = await asyncio.wait_for(
                self._read_until_completed(websocket, thread_id, turn_id, answer_parts, turn_errors),
                timeout=self.config.turn_timeout_seconds,
            )
            turn = completed.get("turn") or {}
            error = self._turn_error(turn, turn_errors.get(turn_id))
            return CodexRunResult(
                thread_id=thread_id,
                turn_id=turn_id,
                status=turn.get("status") or "unknown",
                answer="".join(answer_parts),
                duration_ms=turn.get("durationMs"),
                error_message=error.get("message") if error else None,
                error_info=error.get("codexErrorInfo") or error.get("code") if error else None,
            )

    async def compact_thread(self, thread_id: str) -> CodexCompactResult:
        turn_errors: dict[str, dict[str, Any]] = {}
        async with websockets.connect(self.config.ws_url) as websocket:
            await self._request(
                websocket,
                "initialize",
                {
                    "clientInfo": {"name": "breakwater", "version": "0.1.0", "title": "Breakwater"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await websocket.send(json.dumps({"method": "initialized"}))
            thread_start = await self._request(websocket, "thread/resume", self._thread_resume_params(thread_id))
            actual_thread_id = thread_start["thread"]["id"]
            await self._request(websocket, "thread/compact/start", {"threadId": actual_thread_id})
            completed = await asyncio.wait_for(
                self._read_until_compaction_completed(websocket, actual_thread_id, turn_errors),
                timeout=self.config.turn_timeout_seconds,
            )
            turn = completed.get("turn") or {}
            turn_id = str(turn.get("id") or completed.get("turnId") or "") or None
            error = self._turn_error(turn, turn_errors.get(turn_id or ""))
            return CodexCompactResult(
                thread_id=actual_thread_id,
                turn_id=turn_id,
                status=turn.get("status") or "unknown",
                duration_ms=turn.get("durationMs"),
                error_message=error.get("message") if error else None,
                error_info=error.get("codexErrorInfo") or error.get("code") if error else None,
            )

    async def read_turn(self, thread_id: str, turn_id: str) -> CodexTurnSnapshot | None:
        async with websockets.connect(self.config.ws_url) as websocket:
            await self._request(
                websocket,
                "initialize",
                {
                    "clientInfo": {"name": "breakwater", "version": "0.1.0", "title": "Breakwater"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await websocket.send(json.dumps({"method": "initialized"}))
            result = await self._request(websocket, "thread/read", {"threadId": thread_id, "includeTurns": True})
        thread = result.get("thread") or {}
        for turn in thread.get("turns") or []:
            if str(turn.get("id") or "") != turn_id:
                continue
            error = self._turn_error(turn, None)
            return CodexTurnSnapshot(
                thread_id=str(thread.get("id") or thread_id),
                turn_id=turn_id,
                status=str(turn.get("status") or "unknown"),
                error_message=error.get("message") if error else None,
                error_info=error.get("codexErrorInfo") or error.get("code") if error else None,
            )
        return None

    def _thread_start_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": str(self.workspace),
            "ephemeral": False,
            "sandbox": self.config.sandbox,
            "approvalPolicy": self.config.approval_policy,
        }
        if self.config.model:
            params["model"] = self.config.model
        if self.task_env:
            params["config"] = {"shell_environment_policy": {"inherit": "all", "set": dict(self.task_env)}}
        params["serviceName"] = "breakwater"
        return params

    def _thread_resume_params(self, thread_id: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "cwd": str(self.workspace),
            "sandbox": self.config.sandbox,
            "approvalPolicy": self.config.approval_policy,
        }
        if self.config.model:
            params["model"] = self.config.model
        if self.task_env:
            params["config"] = {"shell_environment_policy": {"inherit": "all", "set": dict(self.task_env)}}
        return params

    def _turn_start_params(self, thread_id: str, input_items: list[dict[str, Any]]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": input_items,
            "cwd": str(self.workspace),
            "approvalPolicy": self.config.approval_policy,
        }
        if self.config.model:
            params["model"] = self.config.model
        if self.config.effort:
            params["effort"] = self.config.effort
        return params

    def retry_prompt(self, slot_id: str) -> str:
        return self.prompts.render("retry_missing_reply", **self._prompt_values(slot_id, incoming_text=""))

    def jira_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return self.prompts.render(
            "jira_analyze_retry",
            **self._prompt_values(slot_id, incoming_text=str(prompt_context.get("jira_comment_body") or ""), **prompt_context),
        )

    def jira_issue_auto_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return self.prompts.render(
            "jira_issue_auto_analyze_retry",
            **self._prompt_values(slot_id, incoming_text=str(prompt_context.get("jira_comment_body") or ""), **prompt_context),
        )

    def jira_status_summary_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return self.prompts.render(
            "jira_status_summary_retry",
            **self._prompt_values(slot_id, incoming_text=str(prompt_context.get("jira_comment_body") or ""), **prompt_context),
        )

    def lark_jira_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return self.prompts.render(
            "lark_jira_analyze_retry",
            **self._prompt_values(slot_id, incoming_text=str(prompt_context.get("jira_comment_body") or ""), **prompt_context),
        )

    def github_issue_analysis_retry_prompt(self, slot_id: str, prompt_context: dict[str, object]) -> str:
        return self.prompts.render(
            "github_issue_analyze_retry",
            **self._prompt_values(slot_id, incoming_text=str(prompt_context.get("github_issue_body") or ""), **prompt_context),
        )

    def _initial_prompt(self, slot_id: str, incoming_text: str, prompt_name: str, prompt_context: dict[str, object]) -> str:
        return self.prompts.render(
            prompt_name,
            **self._prompt_values(slot_id, incoming_text=incoming_text, **prompt_context),
        )

    def _prompt_values(self, slot_id: str, incoming_text: str, **extra: object) -> dict[str, object]:
        db_value = shlex.quote(str(self.db_path))
        issue_key = str(extra.get("jira_issue_key") or "")
        jira_jql = f"issuekey = {issue_key}" if issue_key else "updated >= -7d ORDER BY updated DESC"
        marker = str(extra.get("jira_marker") or f"Breakwater-Analysis-Slot: {slot_id}")
        comment_file = f".breakwater/jira-analysis-{slot_id}.md"
        automation_report_file = self.project_root / ".breakwater" / f"jira-automation-{slot_id}.json"
        github_comment_file = f".breakwater/github-analysis-{slot_id}.md"
        jira_skill_path = self.jira_skill_dir / "SKILL.md"
        jira_search_script = self.jira_skill_dir / "scripts" / "jira_search_issue.py"
        develop_skill_path = self.develop_skill_dir / "SKILL.md"
        jira_skill_status = (
            f"Jira skill path: {jira_skill_path}"
            if jira_skill_path.exists()
            else f"Jira skill was expected at {jira_skill_path}, but it was not found. Report this missing path instead of guessing another Jira write path."
        )
        develop_skill_status = (
            f"Develop skill path: {develop_skill_path}"
            if self.develop_enabled and develop_skill_path.exists()
            else f"Develop skill was expected at {develop_skill_path}, but it was not found or disabled. Report this missing path before attempting direct coding."
        )
        values: dict[str, object] = {
            "slot_id": slot_id,
            "breakwater_db": self.db_path,
            "incoming_text": incoming_text,
            "reply_command": f'BREAKWATER_DB={db_value} uv run breakwater reply {slot_id} --message "要回复给用户的内容"',
            "reply_file_command": f"BREAKWATER_DB={db_value} uv run breakwater reply {slot_id} --message-file .breakwater/reply-{slot_id}.txt",
            "automation_report_file": str(automation_report_file),
            "automation_report_command": (
                f"BREAKWATER_DB={db_value} uv run --project {shlex.quote(str(self.project_root))} "
                "breakwater automation-report "
                f"{shlex.quote(slot_id)} --json-file {shlex.quote(str(automation_report_file))}"
            ),
            "jira_issue_command": (
                f"uv run --project {shlex.quote(str(self.jira_skill_dir))} "
                f"python3 {shlex.quote(str(jira_search_script))} "
                f"--jql {shlex.quote(jira_jql)} "
                "--show-comments --show-description --show-attachments --max-results 1"
            ),
            "jira_comment_file": comment_file,
            "jira_comment_command": (
                f"uv run --project {shlex.quote(str(self.jira_skill_dir))} "
                f"python3 {shlex.quote(str(self.jira_skill_dir / 'scripts' / 'jira_comment_issue.py'))} "
                f"--issue-key {shlex.quote(issue_key)} --comment-file {shlex.quote(comment_file)}"
            ),
            "github_comment_file": github_comment_file,
            "github_comment_command": (
                f"BREAKWATER_DB={db_value} uv run breakwater github-comment {slot_id} "
                f"--body-file {shlex.quote(github_comment_file)} --api-url {shlex.quote(self.github_api_url)}"
            ),
            "jira_skill_dir": self.jira_skill_dir,
            "jira_skill_path": jira_skill_path,
            "jira_skill_status": jira_skill_status,
            "develop_skill_dir": self.develop_skill_dir,
            "develop_skill_path": develop_skill_path,
            "develop_skill_status": develop_skill_status,
            "jira_marker": marker,
            "jira_issue_key": issue_key,
            "jira_comment_id": str(extra.get("jira_comment_id") or ""),
            "jira_comment_body": str(extra.get("jira_comment_body") or incoming_text),
            "github_repo": str(extra.get("github_repo") or ""),
            "github_issue_number": str(extra.get("github_issue_number") or ""),
            "github_issue_body": str(extra.get("github_issue_body") or incoming_text),
            "github_marker": str(extra.get("github_marker") or f"Breakwater-GitHub-Analysis-Slot: {slot_id}"),
        }
        values.update(extra)
        return values

    def _input_items(
        self,
        prompt: str,
        prompt_name: str,
        *,
        input_attachments: list[CodexInputAttachment] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        jira_skill_path = self.jira_skill_dir / "SKILL.md"
        develop_skill_path = self.develop_skill_dir / "SKILL.md"
        if jira_skill_path.exists():
            items.append(
                {
                    "type": "skill",
                    "name": "jira-issue",
                    "path": str(jira_skill_path),
                }
            )
        if self.develop_enabled and develop_skill_path.exists():
            items.append(
                {
                    "type": "skill",
                    "name": "breakwater-develop",
                    "path": str(develop_skill_path),
                }
            )
        items.extend(self._prompt_input_items(prompt, input_attachments=input_attachments))
        is_jira_prompt = (
            prompt_name.startswith("jira_analyze")
            or prompt_name.startswith("jira_issue_auto_analyze")
            or prompt_name.startswith("jira_status_summary")
        )
        if not is_jira_prompt and not prompt_name.startswith("github_issue_analyze"):
            items.insert(
                0,
                {
                    "type": "skill",
                    "name": "breakwater-reply",
                    "path": str(self.project_root / "skills" / "breakwater-reply" / "SKILL.md"),
                },
            )
        return items

    def _prompt_input_items(
        self,
        prompt: str,
        *,
        input_attachments: list[CodexInputAttachment] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for attachment in input_attachments or []:
            if attachment.kind != "image":
                continue
            item: dict[str, Any] = {"type": "localImage", "path": str(attachment.path)}
            if attachment.detail:
                item["detail"] = attachment.detail
            items.append(item)
        items.append({"type": "text", "text": prompt})
        return items

    async def _request(self, websocket: Any, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = self._next_id
        self._next_id += 1
        await websocket.send(json.dumps({"id": request_id, "method": method, "params": params}))
        while True:
            message = json.loads(await websocket.recv())
            if "id" in message:
                if message["id"] != request_id:
                    LOG.info("ignored unmatched codex response", extra={"component": "codex"})
                    continue
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error'].get('message')}")
                return message.get("result")
            await self._handle_notification(message)

    async def _read_until_completed(
        self,
        websocket: Any,
        thread_id: str,
        turn_id: str,
        answer_parts: list[str],
        turn_errors: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        while True:
            message = json.loads(await websocket.recv())
            if "id" in message:
                await self._handle_server_request(websocket, message)
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if method == "item/agentMessage/delta":
                if params.get("threadId") == thread_id and params.get("turnId") == turn_id:
                    answer_parts.append(str(params.get("delta") or ""))
                continue
            if method == "turn/completed" and params.get("threadId") == thread_id:
                return params
            if method == "error" and params.get("threadId") == thread_id:
                error_turn_id = params.get("turnId")
                error = params.get("error")
                if error_turn_id and isinstance(error, dict):
                    turn_errors[str(error_turn_id)] = error
            await self._handle_notification(message)

    async def _read_until_compaction_completed(
        self,
        websocket: Any,
        thread_id: str,
        turn_errors: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        compact_turn_id: str | None = None
        while True:
            message = json.loads(await websocket.recv())
            if "id" in message:
                await self._handle_server_request(websocket, message)
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if params.get("threadId") != thread_id:
                await self._handle_notification(message)
                continue
            if method == "turn/started":
                turn = params.get("turn") or {}
                if turn.get("kind") == "compact" or turn.get("type") == "compact":
                    compact_turn_id = str(turn.get("id") or params.get("turnId") or "")
            elif method in {"item/started", "item/completed"}:
                item = params.get("item") or {}
                if item.get("type") == "contextCompaction":
                    compact_turn_id = str(params.get("turnId") or compact_turn_id or "")
            elif method == "error":
                error_turn_id = params.get("turnId")
                error = params.get("error")
                if error_turn_id and isinstance(error, dict):
                    turn_errors[str(error_turn_id)] = error
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                turn_id = str(turn.get("id") or params.get("turnId") or "")
                if compact_turn_id is None or turn_id == compact_turn_id:
                    return params
            await self._handle_notification(message)

    async def _handle_server_request(self, websocket: Any, message: dict[str, Any]) -> None:
        if "method" not in message:
            return
        LOG.warning("unsupported codex server request: %s", message.get("method"), extra={"component": "codex"})
        await websocket.send(
            json.dumps(
                {
                    "id": message.get("id"),
                    "error": {"code": -32601, "message": f"unsupported client-side request: {message.get('method')}"},
                }
            )
        )

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method in {"thread/status/changed", "turn/started", "warning", "error"}:
            LOG.info("codex notification %s %s", method, json.dumps(message.get("params"), ensure_ascii=False), extra={"component": "codex"})

    def _turn_error(self, turn: dict[str, Any], notification_error: dict[str, Any] | None) -> dict[str, Any] | None:
        error = turn.get("error")
        if isinstance(error, dict):
            return error
        return notification_error
