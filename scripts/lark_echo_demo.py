#!/usr/bin/env python3
"""Minimal Lark SDK echo demo.

This is intentionally small and isolated from the future Breakwater service
architecture. It proves the SDK receive -> reply path with the same local app
configured by lark-cli.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetChatRequest,
    ListMessageRequest,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from Crypto.Cipher import AES


DEFAULT_LARK_CLI_CONFIG = Path.home() / ".lark-cli" / "config.json"
DEFAULT_LARK_CLI_SECRET_DIR = Path.home() / ".local" / "share" / "lark-cli"


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


def load_secret_from_lark_cli(config_path: Path, app_id: str | None) -> tuple[str, str]:
    if not config_path.exists():
        raise RuntimeError(f"lark-cli config not found: {config_path}")

    data = json.loads(config_path.read_text())
    apps = data.get("apps") or []
    if not apps:
        raise RuntimeError(f"no apps found in lark-cli config: {config_path}")

    app: dict[str, Any] | None = None
    if app_id:
        app = next((item for item in apps if item.get("appId") == app_id), None)
        if app is None:
            raise RuntimeError(f"appId {app_id} not found in {config_path}")
    elif len(apps) == 1:
        app = apps[0]
    else:
        ids = ", ".join(str(item.get("appId")) for item in apps)
        raise RuntimeError(f"multiple lark-cli apps found; pass --app-id. apps={ids}")

    loaded_app_id = app.get("appId")
    app_secret = app.get("appSecret")
    if not loaded_app_id or not app_secret:
        raise RuntimeError(f"selected app in {config_path} is missing appId/appSecret")
    if isinstance(app_secret, dict) and app_secret.get("source") == "keychain":
        app_secret = decrypt_lark_cli_secret(loaded_app_id)
    if not isinstance(app_secret, str):
        raise RuntimeError(f"unsupported appSecret format in {config_path}")
    return loaded_app_id, app_secret


def parse_text_content(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    text = payload.get("text")
    if isinstance(text, str):
        return text
    return raw


def build_text_content(text: str) -> str:
    return json.dumps({"text": text}, ensure_ascii=False)


def at_user_text(open_id: str, fallback_name: str = "用户") -> str:
    if not open_id:
        return ""
    return f'<at user_id="{open_id}">{fallback_name}</at>'


def request_id_of(response: Any) -> str:
    return (
        getattr(response, "request_id", None)
        or getattr(response, "requestId", None)
        or getattr(response, "RequestId", None)
        or ""
    )


def mention_to_dict(mention: Any) -> dict[str, str]:
    return {
        "id": str(getattr(mention, "id", "") or ""),
        "id_type": str(getattr(mention, "id_type", "") or ""),
        "name": str(getattr(mention, "name", "") or ""),
        "key": str(getattr(mention, "key", "") or ""),
    }


def is_bot_mentioned(mentions: list[Any] | None, bot_ids: set[str], strict: bool) -> bool:
    if not mentions:
        return False
    for mention in mentions:
        data = mention_to_dict(mention)
        if data["id"] in bot_ids or data["key"] in bot_ids:
            return True
    # Demo fallback: when polling a test group and the message has exactly one
    # mention, accepting it lets us observe the real mention id in logs.
    return len(mentions) == 1 and not strict


def main() -> int:
    parser = argparse.ArgumentParser(description="Echo Lark text messages using lark-oapi.")
    parser.add_argument("--app-id", default=os.getenv("LARK_APP_ID"))
    parser.add_argument("--app-secret", default=os.getenv("LARK_APP_SECRET"))
    parser.add_argument("--config", type=Path, default=DEFAULT_LARK_CLI_CONFIG)
    parser.add_argument("--no-lark-cli-config", action="store_true")
    parser.add_argument("--mode", choices=("poll", "ws", "cli-event"), default="poll")
    parser.add_argument("--chat-id", default=os.getenv("BREAKWATER_DEMO_CHAT_ID"))
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--replay-latest", action="store_true", help="echo the latest non-app message once and exit")
    parser.add_argument("--bot-mention-id", action="append", default=[], help="extra mention id/key that identifies this bot")
    parser.add_argument("--strict-mention-match", action="store_true", help="in group chats, only respond when mention id/key exactly matches the bot ids")
    parser.add_argument("--prefix", default="echo: ")
    args = parser.parse_args()

    app_id = args.app_id
    app_secret = args.app_secret
    if (not app_id or not app_secret) and not args.no_lark_cli_config:
        app_id, app_secret = load_secret_from_lark_cli(args.config, app_id)
    if not app_id or not app_secret:
        raise RuntimeError("missing credentials: set LARK_APP_ID/LARK_APP_SECRET or allow lark-cli config fallback")

    stop_event = threading.Event()
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    bot_ids = {app_id, *args.bot_mention_id}

    def reply_text(message_id: str, text: str) -> bool:
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
        response = client.im.v1.message.reply(request)
        if not response.success():
            print(
                f"[send-error] code={response.code} msg={response.msg} request_id={request_id_of(response)}",
                file=sys.stderr,
                flush=True,
            )
            return False
        print(f"[sent] reply_to={message_id}", flush=True)
        return True

    def send_chat_text(chat_id: str, text: str) -> bool:
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(build_text_content(text))
                .uuid(f"bw-{uuid.uuid4().hex}")
                .build()
            )
            .build()
        )
        response = client.im.v1.message.create(request)
        if not response.success():
            print(
                f"[send-error] code={response.code} msg={response.msg} request_id={request_id_of(response)}",
                file=sys.stderr,
                flush=True,
            )
            return False
        sent_id = response.data.message_id if response.data else ""
        print(f"[sent] chat_id={chat_id} message_id={sent_id}", flush=True)
        return True

    def get_chat_mode(chat_id: str) -> str:
        request = (
            GetChatRequest.builder()
            .chat_id(chat_id)
            .user_id_type("open_id")
            .build()
        )
        response = client.im.v1.chat.get(request)
        if not response.success():
            print(
                f"[chat-error] code={response.code} msg={response.msg} request_id={request_id_of(response)}",
                file=sys.stderr,
                flush=True,
            )
            return "unknown"
        return response.data.chat_mode or response.data.chat_type or "unknown"

    def process_message(
        *,
        message_id: str | None,
        chat_id: str,
        chat_mode: str,
        msg_type: str | None,
        sender_type: str,
        sender_id: str,
        sender_name: str,
        content: str | None,
        mentions: list[Any] | None,
        source: str,
    ) -> bool:
        if not message_id:
            print("[skip] message missing message_id", flush=True)
            return False
        if sender_type == "app":
            print("[skip] app-sent message", flush=True)
            return False

        text = parse_text_content(content)
        mention_dicts = [mention_to_dict(mention) for mention in (mentions or [])]
        print(
            f"[recv] source={source} message_id={message_id} chat_id={chat_id} "
            f"chat_mode={chat_mode} type={msg_type} sender_type={sender_type} "
            f"mentions={mention_dicts} text={text!r}",
            flush=True,
        )

        if chat_mode != "p2p":
            if not is_bot_mentioned(mentions, bot_ids, args.strict_mention_match):
                print("[skip] group message does not mention this bot", flush=True)
                return False
            group_reply = f"{at_user_text(sender_id, sender_name)} {text}".strip()
            if msg_type != "text":
                return reply_text(message_id, f"{at_user_text(sender_id, sender_name)} 暂时只回显文本消息".strip())
            return reply_text(message_id, group_reply)

        if msg_type != "text":
            return send_chat_text(chat_id, f"{args.prefix}暂时只回显文本消息")
        return send_chat_text(chat_id, f"{args.prefix}{text}")

    def process_cli_event(payload: dict[str, Any]) -> bool:
        if payload.get("type") != "im.message.receive_v1":
            print(f"[skip] unsupported event type={payload.get('type')}", flush=True)
            return False

        message_id = payload.get("message_id") or payload.get("id")
        chat_id = str(payload.get("chat_id") or "")
        chat_type = str(payload.get("chat_type") or "unknown")
        message_type = str(payload.get("message_type") or "")
        sender_id = str(payload.get("sender_id") or "")
        content = str(payload.get("content") or "")

        if sender_id in bot_ids:
            print("[skip] app-sent event", flush=True)
            return False

        if args.chat_id and chat_id != args.chat_id:
            print(f"[skip] event from other chat chat_id={chat_id}", flush=True)
            return False

        print(
            f"[recv] source=cli-event event_id={payload.get('event_id')} "
            f"message_id={message_id} chat_id={chat_id} chat_type={chat_type} "
            f"type={message_type} sender={sender_id} text={content!r}",
            flush=True,
        )

        if chat_type != "p2p":
            group_reply = f"{at_user_text(sender_id)} {content}".strip()
            if message_type != "text":
                return reply_text(str(message_id), f"{at_user_text(sender_id)} 暂时只回显文本消息".strip())
            return reply_text(str(message_id), group_reply)

        if message_type != "text":
            return send_chat_text(chat_id, f"{args.prefix}暂时只回显文本消息")
        return send_chat_text(chat_id, f"{args.prefix}{content}")

    def handle_message(event: P2ImMessageReceiveV1) -> None:
        message = event.event.message if event.event else None
        sender = event.event.sender if event.event else None
        if message is None:
            print("[skip] event missing message", flush=True)
            return

        message_id = message.message_id
        message_type = message.message_type
        sender_type = sender.sender_type if sender else ""
        sender_id = ""
        if sender and sender.sender_id and sender.sender_id.open_id:
            sender_id = sender.sender_id.open_id

        print(
            f"[recv] message_id={message_id} chat_id={message.chat_id} "
            f"type={message_type} sender_type={sender_type} sender={sender_id}",
            flush=True,
        )

        process_message(
            message_id=message_id,
            chat_id=message.chat_id or "",
            chat_mode=message.chat_type or "unknown",
            msg_type=message_type,
            sender_type=sender_type,
            sender_id=sender_id,
            sender_name="用户",
            content=message.content,
            mentions=message.mentions,
            source="ws",
        )

    def request_stop(signum: int, _frame: Any) -> None:
        print(f"[signal] received {signum}, stopping", flush=True)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    print(f"[start] lark echo demo app_id={app_id} mode={args.mode}", flush=True)
    if args.mode == "cli-event":
        command = [
            "lark-cli",
            "event",
            "consume",
            "im.message.receive_v1",
            "--as",
            "bot",
        ]
        print("[ready] cli-event mode; @ the bot in the target group; Ctrl-C to stop", flush=True)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        def print_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                print(f"[lark-cli] {line.rstrip()}", file=sys.stderr, flush=True)

        stderr_thread = threading.Thread(target=print_stderr, name="lark-cli-stderr", daemon=True)
        stderr_thread.start()
        assert process.stdout is not None
        try:
            while not stop_event.is_set():
                line = process.stdout.readline()
                if line == "":
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    print(f"[event-parse-error] raw={line!r}", file=sys.stderr, flush=True)
                    continue
                process_cli_event(payload)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        return process.returncode or 0

    if args.mode == "ws":
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(handle_message)
            .build()
        )
        ws_client = lark.ws.Client(
            app_id=app_id,
            app_secret=app_secret,
            event_handler=event_handler,
            auto_reconnect=True,
            extra_ua_tags=["channel"],
        )
        print("[ready] websocket mode; send a text message to the bot; Ctrl-C to stop", flush=True)
        thread = threading.Thread(target=ws_client.start, name="lark-ws-client", daemon=True)
        thread.start()
        stop_event.wait()
        return 0

    if not args.chat_id:
        raise RuntimeError("poll mode requires --chat-id or BREAKWATER_DEMO_CHAT_ID")

    chat_mode = get_chat_mode(args.chat_id)
    print(f"[chat] chat_id={args.chat_id} chat_mode={chat_mode}", flush=True)

    if args.replay_latest:
        request = (
            ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(args.chat_id)
            .page_size(20)
            .sort_type("ByCreateTimeDesc")
            .build()
        )
        response = client.im.v1.message.list(request)
        if not response.success():
            print(f"[poll-error] code={response.code} msg={response.msg} request_id={request_id_of(response)}", file=sys.stderr, flush=True)
            return 1
        for item in response.data.items or []:
            sender = item.sender
            sender_type = sender.sender_type if sender else ""
            if sender_type == "app":
                continue
            handled = process_message(
                message_id=item.message_id,
                chat_id=args.chat_id,
                chat_mode=chat_mode,
                msg_type=item.msg_type,
                sender_type=sender_type,
                sender_id=sender.sender_id.open_id if sender and sender.sender_id else "",
                sender_name=getattr(sender, "name", "") or "用户",
                content=item.body.content if item.body else "",
                mentions=item.mentions,
                source="replay",
            )
            if handled:
                return 0
        print("[replay] no actionable non-app message found", flush=True)
        return 1

    seen: set[str] = set()
    print(f"[ready] polling chat_id={args.chat_id}; send a text message to the bot; Ctrl-C to stop", flush=True)
    seed_request = (
        ListMessageRequest.builder()
        .container_id_type("chat")
        .container_id(args.chat_id)
        .page_size(20)
        .sort_type("ByCreateTimeDesc")
        .build()
    )
    seed_response = client.im.v1.message.list(seed_request)
    if seed_response.success():
        seen.update(item.message_id for item in (seed_response.data.items or []) if item.message_id)
        print(f"[seed] marked {len(seen)} existing message(s) as seen", flush=True)
    else:
        print(f"[seed-error] code={seed_response.code} msg={seed_response.msg}", file=sys.stderr, flush=True)

    while not stop_event.is_set():
        request = (
            ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(args.chat_id)
            .page_size(10)
            .sort_type("ByCreateTimeDesc")
            .build()
        )
        response = client.im.v1.message.list(request)
        if not response.success():
            print(
                f"[poll-error] code={response.code} msg={response.msg} request_id={request_id_of(response)}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(args.poll_interval)
            continue

        items = list(response.data.items or [])
        for item in reversed(items):
            message_id = item.message_id
            if not message_id or message_id in seen:
                continue
            seen.add(message_id)

            sender = item.sender
            sender_type = sender.sender_type if sender else ""
            process_message(
                message_id=message_id,
                chat_id=args.chat_id,
                chat_mode=chat_mode,
                msg_type=item.msg_type,
                sender_type=sender_type,
                sender_id=sender.sender_id.open_id if sender and sender.sender_id else "",
                sender_name=getattr(sender, "name", "") or "用户",
                content=item.body.content if item.body else "",
                mentions=item.mentions,
                source="poll",
            )

        time.sleep(args.poll_interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
