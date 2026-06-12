from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_IMAGE_PLACEHOLDER_RE = re.compile(r"\[Image:\s*(img_[^\]\s,]+)(?:\s*,\s*(\d+)x(\d+))?\]")


@dataclass(frozen=True)
class LarkImageAttachment:
    file_key: str
    width: int | None = None
    height: int | None = None
    placeholder: str = ""


@dataclass(frozen=True)
class LarkNormalizedContent:
    text: str
    images: tuple[LarkImageAttachment, ...] = ()


def normalize_lark_content(message_type: str, content: str, raw: dict[str, Any] | None = None) -> LarkNormalizedContent:
    if message_type == "post":
        post = _first_post_body(content, raw)
        if post is None:
            return LarkNormalizedContent(content, tuple(_placeholder_images(content)))
        text, images = _flatten_post_body(post)
        text = text or content
        return LarkNormalizedContent(text, tuple(_merge_images(images, _placeholder_images(text))))
    if message_type == "text":
        text = _text_content(content)
        return LarkNormalizedContent(text, tuple(_placeholder_images(text)))
    return LarkNormalizedContent(content)


def _text_content(content: str) -> str:
    decoded = _json_value(content)
    if isinstance(decoded, dict):
        text = decoded.get("text")
        if isinstance(text, str):
            return text
    return content


def _first_post_body(content: str, raw: dict[str, Any] | None) -> dict[str, Any] | None:
    for candidate in _post_candidates(content, raw or {}):
        body = _as_post_body(candidate)
        if body is not None:
            return body
    return None


def _post_candidates(content: str, raw: dict[str, Any]) -> list[Any]:
    candidates: list[Any] = [content]
    candidates.extend(
        _path(raw, path)
        for path in (
            ("content",),
            ("body",),
            ("body", "content"),
            ("message", "content"),
            ("message", "body"),
            ("message", "body", "content"),
            ("data", "message", "content"),
            ("data", "message", "body"),
            ("data", "message", "body", "content"),
            ("event", "message", "content"),
            ("event", "message", "body"),
            ("event", "message", "body", "content"),
        )
    )
    return [candidate for candidate in candidates if candidate not in (None, "")]


def _path(value: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _as_post_body(value: Any) -> dict[str, Any] | None:
    decoded = _json_value(value)
    if isinstance(decoded, dict) and isinstance(decoded.get("content"), list):
        return decoded
    if isinstance(decoded, dict):
        body = _json_value(decoded.get("body"))
        if isinstance(body, dict) and isinstance(body.get("content"), list):
            return body
        body_content = body.get("content") if isinstance(body, dict) else None
        body_decoded = _json_value(body_content)
        if isinstance(body_decoded, dict) and isinstance(body_decoded.get("content"), list):
            return body_decoded
    return None


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _flatten_post_body(body: dict[str, Any]) -> tuple[str, list[LarkImageAttachment]]:
    images: list[LarkImageAttachment] = []
    lines: list[str] = []
    title = str(body.get("title") or "").strip()
    if title:
        lines.append(title)
    for paragraph in body.get("content") or []:
        if not isinstance(paragraph, list):
            continue
        parts: list[str] = []
        for element in paragraph:
            if not isinstance(element, dict):
                continue
            tag = str(element.get("tag") or "")
            if tag in {"text", "a", "at"}:
                text = str(element.get("text") or element.get("name") or "")
                if text:
                    parts.append(text)
            elif tag == "img":
                image = _image_attachment(element)
                if image is not None:
                    images.append(image)
                    parts.append(image.placeholder)
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines), images


def _image_attachment(element: dict[str, Any]) -> LarkImageAttachment | None:
    file_key = str(element.get("image_key") or element.get("file_key") or "").strip()
    if not file_key:
        return None
    width = _positive_int(element.get("width"))
    height = _positive_int(element.get("height"))
    placeholder = _image_placeholder(file_key, width, height)
    return LarkImageAttachment(file_key=file_key, width=width, height=height, placeholder=placeholder)


def _placeholder_images(text: str) -> list[LarkImageAttachment]:
    images: list[LarkImageAttachment] = []
    for match in _IMAGE_PLACEHOLDER_RE.finditer(text):
        file_key = match.group(1)
        width = _positive_int(match.group(2))
        height = _positive_int(match.group(3))
        images.append(
            LarkImageAttachment(
                file_key=file_key,
                width=width,
                height=height,
                placeholder=_image_placeholder(file_key, width, height),
            )
        )
    return images


def _merge_images(
    primary: list[LarkImageAttachment],
    fallback: list[LarkImageAttachment],
) -> list[LarkImageAttachment]:
    images = list(primary)
    seen = {image.file_key for image in images}
    for image in fallback:
        if image.file_key in seen:
            continue
        images.append(image)
        seen.add(image.file_key)
    return images


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _image_placeholder(file_key: str, width: int | None, height: int | None) -> str:
    size = f", {width}x{height}" if width and height else ""
    return f"[Image: {file_key}{size}]"
