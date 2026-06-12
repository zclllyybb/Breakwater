from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .db import AnalysisCase, Database, Slot
from .sources import (
    GITHUB_ISSUE_ANALYZE_SOURCE,
    JIRA_ANALYZE_SOURCE,
    JIRA_ISSUE_AUTO_ANALYZE_SOURCE,
    JIRA_STATUS_SUMMARY_SOURCE,
    LARK_JIRA_ANALYZE_SOURCE,
)


ISSUE_KEY_RE = re.compile(r"(?<![A-Z0-9_-])([A-Z][A-Z0-9]+-\d+)(?![A-Z0-9_-]|\.\d)", re.IGNORECASE)
GITHUB_ISSUE_URL_RE = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)", re.IGNORECASE)
GITHUB_ISSUE_REF_RE = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)(?!\d)", re.IGNORECASE)
SLOT_ID_RE = re.compile(r"\b(slot_[0-9a-f]{12})\b", re.IGNORECASE)
CASE_ID_RE = re.compile(r"\b(case_[a-z0-9_]+)\b", re.IGNORECASE)
CODEX_THREAD_ID_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b", re.IGNORECASE)
LARK_REFERENCE_MESSAGE_KEYS = frozenset(
    {
        "parent_id",
        "parent_message_id",
        "reply_to_message_id",
        "quote_message_id",
        "root_id",
        "thread_id",
    }
)


@dataclass(frozen=True)
class CaseReference:
    alias_type: str
    alias_key: str


@dataclass(frozen=True)
class CaseResolution:
    case: AnalysisCase
    references: tuple[CaseReference, ...]


@dataclass(frozen=True)
class CaseResolutionResult:
    status: str
    references: tuple[CaseReference, ...]
    case: AnalysisCase | None = None
    candidates: tuple[AnalysisCase, ...] = ()
    unresolved: tuple[CaseReference, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.status == "resolved" and self.case is not None


@dataclass(frozen=True)
class CodexSlotPlan:
    prompt_name: str
    resume_thread_id: str | None
    prompt_context: dict[str, str]


class CaseResolver:
    """Resolves explicit user references to existing analysis cases."""

    def __init__(self, db: Database):
        self.db = db

    def references_from_lark_text(
        self,
        text: str,
        *,
        lark_message_ids: Iterable[str] = (),
    ) -> tuple[CaseReference, ...]:
        references: list[CaseReference] = []
        for issue_key in ISSUE_KEY_RE.findall(text):
            references.append(CaseReference("jira_issue_key", issue_key.upper()))
        for repo, issue_number in GITHUB_ISSUE_URL_RE.findall(text):
            references.append(CaseReference("github_issue", f"{_normalize_repo(repo)}#{int(issue_number)}"))
        for repo, issue_number in GITHUB_ISSUE_REF_RE.findall(text):
            references.append(CaseReference("github_issue", f"{_normalize_repo(repo)}#{int(issue_number)}"))
        for slot_id in SLOT_ID_RE.findall(text):
            references.append(CaseReference("slot_id", slot_id.lower()))
        for case_id in CASE_ID_RE.findall(text):
            references.append(CaseReference("case_id", case_id.lower()))
        for thread_id in CODEX_THREAD_ID_RE.findall(text):
            references.append(CaseReference("codex_thread_id", thread_id.lower()))
        for message_id in lark_message_ids:
            if message_id:
                references.append(CaseReference("lark_message_id", message_id))
        return _dedupe_references(references)

    def references_from_chat_name(self, chat_name: str | None) -> tuple[CaseReference, ...]:
        if not chat_name:
            return ()
        return _dedupe_references(
            CaseReference("jira_issue_key", issue_key.upper()) for issue_key in ISSUE_KEY_RE.findall(chat_name)
        )

    def resolve_lark_text(
        self,
        text: str,
        *,
        lark_message_ids: Iterable[str] = (),
        chat_name: str | None = None,
    ) -> CaseResolution | None:
        result = self.inspect_lark_text(text, lark_message_ids=lark_message_ids, chat_name=chat_name)
        if not result.is_resolved:
            return None
        assert result.case is not None
        return CaseResolution(case=result.case, references=result.references)

    def inspect_lark_text(
        self,
        text: str,
        *,
        lark_message_ids: Iterable[str] = (),
        chat_name: str | None = None,
    ) -> CaseResolutionResult:
        chat_name_references = self.references_from_chat_name(chat_name)
        if chat_name_references:
            return self._inspect_references(chat_name_references)
        references = self.references_from_lark_text(text, lark_message_ids=lark_message_ids)
        if not references:
            return CaseResolutionResult(status="no_reference", references=())

        return self._inspect_references(references)

    def _inspect_references(self, references: tuple[CaseReference, ...]) -> CaseResolutionResult:
        resolved: dict[str, AnalysisCase] = {}
        unresolved: list[CaseReference] = []
        for reference in references:
            case = self._resolve_reference(reference)
            if case is None:
                unresolved.append(reference)
                continue
            resolved[case.case_id] = case

        candidates = tuple(resolved.values())
        if len(resolved) == 1 and not unresolved:
            return CaseResolutionResult(status="resolved", references=references, case=candidates[0], candidates=candidates)
        if not resolved:
            return CaseResolutionResult(status="unknown", references=references, unresolved=tuple(unresolved))
        return CaseResolutionResult(
            status="ambiguous",
            references=references,
            candidates=candidates,
            unresolved=tuple(unresolved),
        )

    def resolve_identifier(self, identifier: str) -> CaseResolution | None:
        case = self.db.get_case(identifier)
        if case:
            return CaseResolution(case=case, references=(CaseReference("case_id", identifier),))
        return self.resolve_lark_text(identifier)

    def _resolve_reference(self, reference: CaseReference) -> AnalysisCase | None:
        if reference.alias_type == "case_id":
            return self.db.get_case(reference.alias_key) or self.db.find_case_by_alias(reference.alias_type, reference.alias_key)
        return self.db.find_case_by_alias(reference.alias_type, reference.alias_key)


class CodexSlotPlanner:
    """Builds the Codex prompt plan for a slot without running Codex."""

    def plan(
        self,
        slot: Slot,
        case: AnalysisCase | None,
        *,
        jira_marker: str,
        github_marker: str | None = None,
    ) -> CodexSlotPlan:
        resume_thread_id = case.latest_codex_thread_id if case else None
        if slot.source == JIRA_ANALYZE_SOURCE:
            prompt_name = "jira_analyze_continue" if resume_thread_id else "jira_analyze"
        elif slot.source == JIRA_ISSUE_AUTO_ANALYZE_SOURCE:
            prompt_name = "jira_issue_auto_analyze_continue" if resume_thread_id else "jira_issue_auto_analyze"
        elif slot.source == JIRA_STATUS_SUMMARY_SOURCE:
            prompt_name = "jira_status_summary_continue" if resume_thread_id else "jira_status_summary"
        elif slot.source == LARK_JIRA_ANALYZE_SOURCE:
            prompt_name = "lark_jira_analyze"
        elif slot.source == "lark_case_followup":
            prompt_name = "lark_case_followup"
        elif slot.source == GITHUB_ISSUE_ANALYZE_SOURCE:
            prompt_name = "github_issue_analyze_continue" if resume_thread_id else "github_issue_analyze"
        else:
            prompt_name = "lark_initial"
        jira_issue_key = slot.jira_issue_key or (case.scope_key if case and case.scope_type == "jira_issue" else "")
        github_repo = slot.github_repo or ""
        github_issue_number = str(slot.github_issue_number or "")

        return CodexSlotPlan(
            prompt_name=prompt_name,
            resume_thread_id=resume_thread_id,
            prompt_context={
                "case_id": case.case_id if case else "",
                "case_scope_type": case.scope_type if case else "",
                "case_scope_key": case.scope_key if case else "",
                "case_summary": case.summary if case and case.summary else "",
                "codex_thread_id": resume_thread_id or "",
                "delivery_target": slot.delivery_target or "",
                "jira_issue_key": jira_issue_key,
                "jira_comment_id": slot.jira_comment_id or "",
                "jira_comment_body": slot.incoming_text,
                "jira_marker": jira_marker,
                "github_repo": github_repo,
                "github_issue_number": github_issue_number,
                "github_issue_body": slot.incoming_text,
                "github_marker": github_marker or "",
                "lark_chat_id": slot.chat_id or "",
                "lark_message_id": slot.lark_message_id or "",
                "case_bound_lark_chat_id": case.bound_lark_chat_id if case else "",
                "case_bound_lark_chat_name": case.bound_lark_chat_name if case else "",
            },
        )


class ActiveCaseRegistry:
    """Tracks in-process slot and case claims for worker concurrency control."""

    def __init__(self) -> None:
        self.active_slot_ids: set[str] = set()
        self.active_case_ids: set[str] = set()

    def is_slot_active(self, slot_id: str) -> bool:
        return slot_id in self.active_slot_ids

    def claim(self, slot: Slot) -> bool:
        if slot.slot_id in self.active_slot_ids:
            return False
        if slot.case_id and slot.case_id in self.active_case_ids:
            return False
        self.active_slot_ids.add(slot.slot_id)
        if slot.case_id:
            self.active_case_ids.add(slot.case_id)
        return True

    def release(self, slot: Slot | None) -> None:
        if slot is None:
            return
        self.active_slot_ids.discard(slot.slot_id)
        if slot.case_id:
            self.active_case_ids.discard(slot.case_id)


def _dedupe_references(references: Iterable[CaseReference]) -> tuple[CaseReference, ...]:
    seen: set[tuple[str, str]] = set()
    deduped: list[CaseReference] = []
    for reference in references:
        key = (reference.alias_type, reference.alias_key)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(reference)
    return tuple(deduped)


def _normalize_repo(repo_full_name: str) -> str:
    return str(repo_full_name).strip().strip("/").lower()


def extract_lark_reference_message_ids(raw: dict[str, Any]) -> tuple[str, ...]:
    """Extract possible parent/reply message ids from raw Lark event payloads."""

    candidates: list[str] = []

    def visit(value: Any, *, depth: int = 0) -> None:
        if depth > 3:
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if key in LARK_REFERENCE_MESSAGE_KEYS and isinstance(item, str) and item:
                candidates.append(item)
        for nested_key in ("event", "message", "data"):
            nested = value.get(nested_key)
            if isinstance(nested, dict):
                visit(nested, depth=depth + 1)

    visit(raw)
    return tuple(dict.fromkeys(candidates))


def format_case_resolution_message(result: CaseResolutionResult) -> str:
    if result.status == "unknown":
        refs = ", ".join(_format_reference(reference) for reference in result.references)
        return f"我没有找到这些 case 引用：{refs}。请提供有效的 Jira key、slot id、case id 或 session id。"
    if result.status == "ambiguous":
        lines = ["我找到了多个可能的 case，请明确回复其中一个标识后我再继续："]
        for case in result.candidates:
            session = case.latest_codex_thread_id or "session pending"
            lines.append(f"- {case.scope_key} · {case.case_id} · {session}")
        if result.unresolved:
            unresolved = ", ".join(_format_reference(reference) for reference in result.unresolved)
            lines.append(f"未能识别的引用：{unresolved}")
        return "\n".join(lines)
    return "我无法确定要继续哪个 case，请提供 Jira key、slot id、case id 或 session id。"


def build_case_anchor(case: AnalysisCase, slot: Slot) -> str:
    session = slot.codex_thread_id or case.latest_codex_thread_id or "session pending"
    scope = case.scope_key or case.case_id
    return f"Case: {scope} · {slot.slot_id} · session {session}"


def build_case_summary(case: AnalysisCase, slot: Slot, *, outcome: str | None = None) -> str:
    parts = [
        f"case_id: {case.case_id}",
        f"scope: {case.scope_type}:{case.scope_key}",
        f"latest_slot: {slot.slot_id}",
        f"source: {slot.source}",
        f"delivery_target: {slot.delivery_target or ''}",
        f"latest_thread: {slot.codex_thread_id or case.latest_codex_thread_id or ''}",
        f"latest_turn: {slot.codex_turn_id or ''}",
        f"latest_input: {_compact(slot.incoming_text, 500)}",
    ]
    if outcome:
        parts.append(f"latest_outcome: {_compact(outcome, 800)}")
    if case.summary:
        parts.append("previous_summary:")
        parts.append(_compact(case.summary, 1600))
    return "\n".join(parts)


def _format_reference(reference: CaseReference) -> str:
    return f"{reference.alias_type}:{reference.alias_key}"


def _compact(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
