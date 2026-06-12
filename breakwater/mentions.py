from __future__ import annotations

import json
import re
from typing import Any, Iterable


JIRA_WIKI_MENTION_RE = re.compile(r"\[~(?P<identifier>[^\]\s]+)\]")
MENTION_ID_KEYS = (
    "open_id",
    "user_id",
    "union_id",
    "app_id",
    "accountId",
    "account_id",
    "key",
    "name",
    "displayName",
    "id",
    "text",
)


def normalized_mention_targets(values: Iterable[object | None]) -> tuple[str, ...]:
    targets: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        for candidate in (text, text.removeprefix("@")):
            key = candidate.casefold()
            if candidate and key not in seen:
                targets.append(candidate)
                seen.add(key)
    return tuple(targets)


def structured_mention_identifiers(raw: Any) -> set[str]:
    identifiers: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str) and value:
            identifiers.add(value)
            if value.startswith("@") and len(value) > 1:
                identifiers.add(value[1:])

    def collect_mention_node(node: dict[str, Any]) -> None:
        for key in MENTION_ID_KEYS:
            value = node.get(key)
            if isinstance(value, dict):
                for nested_key in MENTION_ID_KEYS:
                    add(value.get(nested_key))
            else:
                add(value)
        for nested_key in ("attrs", "user", "userInfo", "id"):
            nested = node.get(nested_key)
            if isinstance(nested, dict):
                collect_mention_node(nested)

    def visit(value: Any, *, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, list):
            for item in value:
                visit(item, depth=depth + 1)
            return
        if not isinstance(value, dict):
            return

        mentions = value.get("mentions")
        if isinstance(mentions, list):
            for mention in mentions:
                if isinstance(mention, dict):
                    collect_mention_node(mention)

        if str(value.get("type") or value.get("tag") or "").lower() in {"mention", "at"}:
            collect_mention_node(value)

        for child in value.values():
            if isinstance(child, (dict, list)):
                visit(child, depth=depth + 1)

    visit(raw)
    return identifiers


def mentions_any_target(raw: Any, targets: Iterable[object | None]) -> bool:
    normalized = normalized_mention_targets(targets)
    if not normalized:
        return False
    target_set = {target.casefold() for target in normalized}
    return any(identifier.casefold() in target_set for identifier in structured_mention_identifiers(raw))


def jira_comment_mentions_target(comment: dict[str, Any], targets: Iterable[object | None]) -> bool:
    normalized = normalized_mention_targets(targets)
    if not normalized:
        return False
    target_set = {target.casefold() for target in normalized}
    if mentions_any_target(comment, normalized):
        return True

    body = comment.get("body")
    if not isinstance(body, str):
        return False
    for match in JIRA_WIKI_MENTION_RE.finditer(body):
        identifier = match.group("identifier")
        candidates = [identifier]
        if identifier.lower().startswith("accountid:"):
            candidates.append(identifier.split(":", 1)[1])
        if any(candidate.casefold() in target_set for candidate in candidates):
            return True
    return False


def jira_comment_body_text(comment: dict[str, Any]) -> str:
    body = comment.get("body")
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False) if body is not None else ""
