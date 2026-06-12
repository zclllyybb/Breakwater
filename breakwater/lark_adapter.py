from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lark_oapi as lark
from Crypto.Cipher import AES
from lark_oapi.api.im.v1 import (
    GetChatRequest,
    GetMessageRequest,
    GetMessageResourceRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from .config import DEFAULT_LARK_CLI_CONFIG, DEFAULT_LARK_CLI_SECRET_DIR
from .mentions import structured_mention_identifiers


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class LarkEvent:
    event_id: str | None
    message_id: str
    chat_id: str
    chat_type: str
    message_type: str
    sender_id: str
    content: str
    raw: dict[str, Any]
    chat_name: str = ""


def decrypt_lark_cli_secret(app_id: str, secret_dir: Path = DEFAULT_LARK_CLI_SECRET_DIR) -> str:
    key_path = secret_dir / "master.key"
    secret_path = secret_dir / f"appsecret_{app_id}.enc"
    if not key_path.exists() or not secret_path.exists():
        raise RuntimeError(f"lark-cli encrypted secret files not found under {secret_dir}")

    key = key_path.read_bytes()
    blob = secret_path.read_bytes()
    nonce = blob[:12]
    ciphertext = blob[12:-16]
    tag = blob[-16:]
    return AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ciphertext, tag).decode()


def load_secret_from_lark_cli(config_path: Path = DEFAULT_LARK_CLI_CONFIG, app_id: str | None = None) -> tuple[str, str]:
    data = json.loads(config_path.read_text())
    apps = data.get("apps") or []
    if not apps:
        raise RuntimeError(f"no apps found in lark-cli config: {config_path}")

    app: dict[str, Any] | None
    if app_id:
        app = next((item for item in apps if item.get("appId") == app_id), None)
    elif len(apps) == 1:
        app = apps[0]
    else:
        ids = ", ".join(str(item.get("appId")) for item in apps)
        raise RuntimeError(f"multiple lark-cli apps found; pass --lark-app-id. apps={ids}")
    if app is None:
        raise RuntimeError(f"appId {app_id} not found in {config_path}")

    loaded_app_id = app.get("appId")
    app_secret = app.get("appSecret")
    if not loaded_app_id or not app_secret:
        raise RuntimeError(f"selected app in {config_path} is missing appId/appSecret")
    if isinstance(app_secret, dict) and app_secret.get("source") == "keychain":
        app_secret = decrypt_lark_cli_secret(loaded_app_id)
    if not isinstance(app_secret, str):
        raise RuntimeError(f"unsupported appSecret format in {config_path}")
    return loaded_app_id, app_secret


def build_text_content(text: str) -> str:
    return json.dumps({"text": text}, ensure_ascii=False)


def at_user_text(open_id: str, fallback_name: str = "用户") -> str:
    if not open_id:
        return ""
    return f'<at user_id="{open_id}">{fallback_name}</at>'


def lark_message_mentions_bot(raw: dict[str, Any], content: str, bot_identifier: str | None) -> bool:
    """Return true when a Lark text event appears to mention this bot."""

    identifiers = {item for item in structured_mention_identifiers(raw) if item}
    if bot_identifier and bot_identifier in identifiers:
        return True
    text = _content_text(content)
    if bot_identifier and (
        f'user_id="{bot_identifier}"' in text
        or f'open_id="{bot_identifier}"' in text
        or f"id='{bot_identifier}'" in text
    ):
        return True
    return False


def _content_text(content: str) -> str:
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(decoded, dict):
        text = decoded.get("text")
        if isinstance(text, str):
            return text
    return content


def request_id_of(response: Any) -> str:
    return (
        getattr(response, "request_id", None)
        or getattr(response, "requestId", None)
        or getattr(response, "RequestId", None)
        or ""
    )


class LarkClient:
    def __init__(self, app_id: str | None = None, app_secret: str | None = None, config_path: Path = DEFAULT_LARK_CLI_CONFIG):
        if not app_id or not app_secret:
            app_id, app_secret = load_secret_from_lark_cli(config_path, app_id)
        self.app_id = app_id
        self._client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        self._chat_name_cache: dict[str, str] = {}

    def reply_text(self, message_id: str, text: str) -> str | None:
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(build_text_content(text))
                .reply_in_thread(False)
                .uuid(f"bw-{uuid.uuid4().hex}")
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(
                f"lark reply failed code={response.code} msg={response.msg} request_id={request_id_of(response)}"
            )
        return response.data.message_id if response.data else None

    def get_chat_name(self, chat_id: str) -> str:
        if not chat_id:
            return ""
        if chat_id in self._chat_name_cache:
            return self._chat_name_cache[chat_id]
        request = GetChatRequest.builder().chat_id(chat_id).build()
        response = self._client.im.v1.chat.get(request)
        if not response.success():
            raise RuntimeError(
                f"lark get chat failed code={response.code} msg={response.msg} request_id={request_id_of(response)}"
            )
        chat_name = response.data.name if response.data and response.data.name else ""
        self._chat_name_cache[chat_id] = chat_name
        return chat_name

    def message_mentions_bot(self, message_id: str) -> bool:
        if not message_id:
            return False
        request = GetMessageRequest.builder().message_id(message_id).user_id_type("open_id").build()
        response = self._client.im.v1.message.get(request)
        if not response.success():
            raise RuntimeError(
                f"lark get message failed code={response.code} msg={response.msg} request_id={request_id_of(response)}"
            )
        items = response.data.items if response.data and response.data.items else []
        mentions: list[dict[str, str]] = []
        for item in items:
            for mention in item.mentions or []:
                mentions.append(
                    {
                        "id": mention.id or "",
                        "id_type": mention.id_type or "",
                        "key": mention.key or "",
                        "name": mention.name or "",
                    }
                )
        return lark_message_mentions_bot({"mentions": mentions}, "", self.app_id)

    def download_message_image(self, *, message_id: str, file_key: str, destination: Path) -> Path:
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type("image")
            .build()
        )
        response = self._client.im.v1.message_resource.get(request)
        if not response.success():
            raise RuntimeError(
                f"lark image download failed code={response.code} msg={response.msg} request_id={request_id_of(response)}"
            )
        if response.file is None:
            raise RuntimeError(f"lark image download returned no file for message_id={message_id} file_key={file_key}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as output:
            try:
                shutil.copyfileobj(response.file, output)
            finally:
                with suppress(Exception):
                    response.file.close()
        return _rename_with_detected_image_suffix(destination)


class LarkEventConsumer:
    def __init__(self, chat_id: str | None = None):
        self.chat_id = chat_id
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def events(self) -> AsyncIterator[LarkEvent]:
        command = ["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot"]
        LOG.info("starting lark event consumer", extra={"component": "lark"})
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._stderr_task = asyncio.create_task(self._log_stderr(self._process.stderr), name="lark-cli-stderr")

        while True:
            line = await self._process.stdout.readline()
            if not line:
                code = await self._process.wait()
                if self._stopping:
                    return
                raise RuntimeError(f"lark-cli event consumer exited code={code}")
            raw_line = line.decode("utf-8", errors="replace").strip()
            if not raw_line:
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                LOG.warning("failed to parse lark event line", extra={"component": "lark"})
                continue
            event = self._parse_event(payload)
            if event is None:
                continue
            yield event

    async def stop(self) -> None:
        self._stopping = True
        if self._process is None:
            return
        if self._process.stdin is not None:
            self._process.stdin.close()
            with suppress(Exception):
                await self._process.stdin.wait_closed()
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        if self._stderr_task is not None:
            with suppress(Exception):
                await asyncio.wait_for(self._stderr_task, timeout=2)
        transport = getattr(self._process, "_transport", None)
        if transport is not None:
            with suppress(Exception):
                transport.close()
        self._process = None
        self._stderr_task = None

    async def _log_stderr(self, stderr: asyncio.StreamReader) -> None:
        while line := await stderr.readline():
            LOG.info("lark-cli: %s", line.decode("utf-8", errors="replace").rstrip(), extra={"component": "lark"})

    def _parse_event(self, payload: dict[str, Any]) -> LarkEvent | None:
        if payload.get("type") != "im.message.receive_v1":
            return None
        chat_id = str(payload.get("chat_id") or "")
        if self.chat_id and chat_id != self.chat_id:
            LOG.info("skip lark event from another chat", extra={"component": "lark"})
            return None
        message_id = str(payload.get("message_id") or payload.get("id") or "")
        if not message_id:
            LOG.warning("skip lark event without message_id", extra={"component": "lark"})
            return None
        return LarkEvent(
            event_id=payload.get("event_id"),
            message_id=message_id,
            chat_id=chat_id,
            chat_type=str(payload.get("chat_type") or ""),
            message_type=str(payload.get("message_type") or ""),
            sender_id=str(payload.get("sender_id") or ""),
            content=str(payload.get("content") or ""),
            raw=payload,
            chat_name=extract_lark_chat_name(payload),
        )


def extract_lark_chat_name(raw: dict[str, Any]) -> str:
    """Extract a group chat name from common lark-cli event payload shapes."""

    direct_keys = ("chat_name", "chat_title", "group_name")

    def visit(value: Any, *, depth: int = 0) -> str:
        if depth > 4 or not isinstance(value, dict):
            return ""
        for key in direct_keys:
            item = value.get(key)
            if isinstance(item, str) and item:
                return item
        chat = value.get("chat")
        if isinstance(chat, dict):
            for key in ("name", "title"):
                item = chat.get(key)
                if isinstance(item, str) and item:
                    return item
        for nested_key in ("event", "message", "data"):
            nested = value.get(nested_key)
            found = visit(nested, depth=depth + 1)
            if found:
                return found
        return ""

    return visit(raw)


def _rename_with_detected_image_suffix(path: Path) -> Path:
    suffix = _detect_image_suffix(path)
    if not suffix or path.suffix == suffix:
        return path
    renamed = path.with_suffix(suffix)
    path.replace(renamed)
    return renamed


def _detect_image_suffix(path: Path) -> str:
    with path.open("rb") as file:
        header = file.read(16)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
        return ".gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ".webp"
    return ""
