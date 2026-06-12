from __future__ import annotations

import argparse
import asyncio
import binascii
import json
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from breakwater.config import AppConfig, CodexConfig, GitHubConfig, JiraConfig, PROJECT_ROOT, ProxyConfig
from breakwater.lark_adapter import LarkEvent
from breakwater.service import LARK_SLOT_IMAGES_KEY, BreakwaterService


DEFAULT_WS_URL = "ws://127.0.0.1:17345"
DEFAULT_MODEL = "gpt-5.4-mini"
EXPECTED_TOKENS = ("BREAKWATER", "SYNC", "OK", "42", "GREEN")
IMAGE_KEY = "img_v3_regression_lark_cli_e2e"
MESSAGE_ID = "om_regression_lark_cli_e2e"
EVENT_ID = "event-regression-lark-cli-e2e"
CHAT_ID = "oc_regression_lark_cli_e2e"
SENDER_ID = "ou_regression_lark_cli_e2e"


FONT_5X7: dict[str, tuple[str, ...]] = {
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "W": ("10001", "10001", "10001", "10001", "10101", "11011", "10001"),
    "Y": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
}


@dataclass(frozen=True)
class RegressionResult:
    status: str
    slot_id: str
    thread_id: str
    turn_id: str
    reply_text: str
    lark_reply_text: str
    db_path: Path
    image_path: Path
    downloaded_image_path: Path
    duration_seconds: float


class RegressionLarkClient:
    app_id = "cli_bot"

    def __init__(self, *, image_bytes: bytes) -> None:
        self.image_bytes = image_bytes
        self.image_downloads: list[tuple[str, str, Path]] = []
        self.replies: list[tuple[str, str]] = []

    def reply_text(self, message_id: str, text: str) -> str:
        self.replies.append((message_id, text))
        return f"om_regression_reply_{len(self.replies)}"

    def get_chat_name(self, chat_id: str) -> str:
        return ""

    def message_mentions_bot(self, message_id: str) -> bool:
        return False

    def download_message_image(self, *, message_id: str, file_key: str, destination: Path) -> Path:
        self.image_downloads.append((message_id, file_key, destination))
        if message_id != MESSAGE_ID:
            raise AssertionError(f"unexpected message_id: {message_id}")
        if file_key != IMAGE_KEY:
            raise AssertionError(f"unexpected file_key: {file_key}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        path = destination.with_suffix(".png")
        path.write_bytes(self.image_bytes)
        return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a live Breakwater Lark image to Codex OCR regression.")
    parser.add_argument("--ws-url", default=DEFAULT_WS_URL, help=f"Codex app-server websocket URL. Default: {DEFAULT_WS_URL}")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model slug. Default: {DEFAULT_MODEL}")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".breakwater") / "regression-artifacts" / "lark-image-e2e",
        help="Directory for isolated regression DB, generated image, and artifacts.",
    )
    parser.add_argument("--timeout", type=int, default=180, help="Codex turn timeout in seconds.")
    parser.add_argument("--max-reply-retries", type=int, default=1, help="Breakwater reply retry count.")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable result object.")
    args = parser.parse_args()

    try:
        result = asyncio.run(run_regression(args))
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    normalized_reply = normalize_answer(result.lark_reply_text or result.reply_text)
    missing = [token for token in EXPECTED_TOKENS if token not in normalized_reply]
    if result.status != "replied":
        print(f"FAIL: slot status is {result.status!r}, expected 'replied'", file=sys.stderr)
        print(f"reply_text={result.reply_text}", file=sys.stderr)
        print(f"db_path={result.db_path}", file=sys.stderr)
        return 1
    if missing:
        print(f"FAIL: missing expected image token(s) in reply: {', '.join(missing)}", file=sys.stderr)
        print(f"reply_text={result.reply_text}", file=sys.stderr)
        print(f"lark_reply_text={result.lark_reply_text}", file=sys.stderr)
        print(f"image_path={result.image_path}", file=sys.stderr)
        print(f"db_path={result.db_path}", file=sys.stderr)
        return 1

    payload = {
        "status": "ok",
        "model": args.model,
        "ws_url": args.ws_url,
        "slot_id": result.slot_id,
        "thread_id": result.thread_id,
        "turn_id": result.turn_id,
        "reply_text": result.reply_text,
        "lark_reply_text": result.lark_reply_text,
        "db_path": str(result.db_path),
        "image_path": str(result.image_path),
        "downloaded_image_path": str(result.downloaded_image_path),
        "duration_seconds": round(result.duration_seconds, 3),
        "expected_tokens": list(EXPECTED_TOKENS),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("PASS: Breakwater Lark image e2e regression completed")
        print(f"model={args.model} slot_id={result.slot_id}")
        print(f"thread_id={result.thread_id} turn_id={result.turn_id}")
        print(f"reply_text={result.reply_text.strip()}")
        print(f"db_path={result.db_path}")
        print(f"downloaded_image_path={result.downloaded_image_path}")
        print(f"duration_seconds={payload['duration_seconds']}")
    return 0


async def run_regression(args: argparse.Namespace) -> RegressionResult:
    run_dir = create_run_dir(args.output_dir)
    db_path = run_dir / "breakwater-regression.db"
    image_path = run_dir / "lark-source-image.png"
    write_regression_image(image_path)

    lark_client = RegressionLarkClient(image_bytes=image_path.read_bytes())
    service = BreakwaterService(
        AppConfig(
            db_path=db_path,
            project_root=PROJECT_ROOT,
            codex_workspace=PROJECT_ROOT,
            codex=CodexConfig(
                ws_url=args.ws_url,
                model=args.model,
                start_server=False,
                turn_timeout_seconds=args.timeout,
                max_reply_retries=args.max_reply_retries,
            ),
            jira=JiraConfig(enabled=False),
            github=GitHubConfig(enabled=False),
            proxy=ProxyConfig(enabled=False),
            web_enabled=False,
            codex_concurrency=1,
            reply_poll_interval=0.05,
        )
    )
    service.db.init()
    service.lark_client = lark_client  # type: ignore[assignment]

    started = time.monotonic()
    await service._handle_lark_event(lark_cli_image_event())
    slot = next(slot for slot in service.db.recent_lark_slots(limit=5) if slot.lark_event_id == EVENT_ID)

    slot_raw = service.db.get_slot_raw(slot.slot_id)
    images = slot_raw.get(LARK_SLOT_IMAGES_KEY)
    if not isinstance(images, list) or not images:
        raise AssertionError("slot raw did not include breakwater_lark_images")
    if images[0].get("file_key") != IMAGE_KEY:
        raise AssertionError(f"unexpected slot image file_key: {images[0].get('file_key')}")

    await service._run_codex_for_slot(slot)
    await service._send_pending_replies_once()

    final_slot = service.db.get_slot(slot.slot_id)
    if final_slot is None:
        raise AssertionError(f"slot disappeared: {slot.slot_id}")
    if not lark_client.image_downloads:
        raise AssertionError("fake lark image download was not called")
    if not lark_client.replies:
        raise AssertionError("fake lark reply was not sent")

    downloaded = lark_client.image_downloads[0][2].with_suffix(".png")
    if not downloaded.exists():
        raise AssertionError(f"downloaded image does not exist: {downloaded}")

    cleanup_possible_reply_file(slot.slot_id)
    return RegressionResult(
        status=final_slot.status,
        slot_id=slot.slot_id,
        thread_id=final_slot.codex_thread_id or "",
        turn_id=final_slot.codex_turn_id or "",
        reply_text=final_slot.reply_text or "",
        lark_reply_text=lark_client.replies[-1][1],
        db_path=db_path,
        image_path=image_path,
        downloaded_image_path=downloaded,
        duration_seconds=time.monotonic() - started,
    )


def lark_cli_image_event() -> LarkEvent:
    content = (
        f"[Image: {IMAGE_KEY}]\n"
        "请读取这张图片，只回复图片中可见的大写英文和数字，按顺序用空格分隔，不要解释。"
    )
    raw: dict[str, Any] = {
        "type": "im.message.receive_v1",
        "event_id": EVENT_ID,
        "timestamp": "1781099716568",
        "id": MESSAGE_ID,
        "message_id": MESSAGE_ID,
        "create_time": "1781099716212",
        "chat_id": CHAT_ID,
        "chat_type": "p2p",
        "message_type": "post",
        "sender_id": SENDER_ID,
        "content": content,
    }
    return LarkEvent(
        event_id=EVENT_ID,
        message_id=MESSAGE_ID,
        chat_id=CHAT_ID,
        chat_type="p2p",
        message_type="post",
        sender_id=SENDER_ID,
        content=content,
        raw=raw,
    )


def create_run_dir(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = output_dir / time.strftime("%Y%m%d-%H%M%S")
    suffix = 0
    candidate = run_dir
    while candidate.exists():
        suffix += 1
        candidate = output_dir / f"{run_dir.name}-{suffix}"
    candidate.mkdir(parents=True)
    return candidate


def cleanup_possible_reply_file(slot_id: str) -> None:
    reply_file = PROJECT_ROOT / ".breakwater" / f"reply-{slot_id}.txt"
    if reply_file.exists():
        reply_file.unlink()


def normalize_answer(value: str) -> str:
    return " ".join(value.upper().replace("\n", " ").split())


def write_regression_image(path: Path) -> None:
    width, height = 960, 420
    pixels = bytearray([236, 240, 244] * width * height)
    draw_rect(pixels, width, 0, 0, width, 86, (28, 115, 79))
    draw_rect(pixels, width, 74, 126, 812, 232, (255, 255, 255))
    draw_rect(pixels, width, 74, 126, 812, 7, (31, 41, 55))
    draw_text(pixels, width, 118, 176, "BREAKWATER", scale=8, color=(17, 24, 39))
    draw_text(pixels, width, 118, 260, "SYNC OK 42", scale=7, color=(22, 101, 52))
    draw_text(pixels, width, 650, 260, "GREEN", scale=7, color=(22, 101, 52))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_png(width, height, pixels))


def draw_text(pixels: bytearray, image_width: int, x: int, y: int, text: str, *, scale: int, color: tuple[int, int, int]) -> None:
    cursor = x
    for char in text:
        glyph = FONT_5X7.get(char)
        if glyph is None:
            raise ValueError(f"missing glyph: {char!r}")
        for row, pattern in enumerate(glyph):
            for col, bit in enumerate(pattern):
                if bit == "1":
                    draw_rect(pixels, image_width, cursor + col * scale, y + row * scale, scale, scale, color)
        cursor += 6 * scale


def draw_rect(
    pixels: bytearray,
    image_width: int,
    x: int,
    y: int,
    width: int,
    height: int,
    color: tuple[int, int, int],
) -> None:
    for row in range(y, y + height):
        start = (row * image_width + x) * 3
        for col in range(width):
            offset = start + col * 3
            pixels[offset : offset + 3] = bytes(color)


def encode_png(width: int, height: int, rgb: bytearray) -> bytes:
    raw = bytearray()
    stride = width * 3
    for row in range(height):
        raw.append(0)
        start = row * stride
        raw.extend(rgb[start : start + stride])

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return len(data).to_bytes(4, "big") + body + binascii.crc32(body).to_bytes(4, "big")

    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw), level=9)) + chunk(b"IEND", b"")


if __name__ == "__main__":
    raise SystemExit(main())
