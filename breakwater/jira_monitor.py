from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .config import DEFAULT_JIRA_STATUS_SUMMARY_TARGET_STATUSES, JiraConfig
from .db import Database
from .jira_adapter import JiraRestClient
from .mentions import jira_comment_body_text, jira_comment_mentions_target


LOG = logging.getLogger(__name__)
JIRA_TZ = timezone(timedelta(hours=8))
CURSOR_KEY = "jira.last_success_at"
AUTO_ANALYZE_SKIP_KEYWORDS = ("/no-analyze",)
NEW_ISSUE_FIELDS = [
    "key",
    "project",
    "summary",
    "status",
    "description",
    "environment",
    "labels",
    "components",
    "reporter",
    "assignee",
    "created",
    "updated",
]
AUTO_ANALYZE_CONTENT_FIELDS = ("summary", "description", "environment", "labels", "components")
STATUS_SUMMARY_FIELDS = [
    "key",
    "project",
    "summary",
    "status",
    "description",
    "environment",
    "labels",
    "components",
    "reporter",
    "assignee",
    "created",
    "updated",
]


class JiraClientProtocol(Protocol):
    def search_all_issues(self, jql: str, fields: list[str]) -> list[dict]: ...
    def list_comments(self, issue_key: str) -> list[dict]: ...


@dataclass(frozen=True)
class JiraPollResult:
    new_issues: int
    analyze_comments: int
    analyze_slots: int
    issue_analyze_slots: int
    status_summary_slots: int
    status_changed_issues: int
    changed_issues: int
    query_start: str
    cursor: str
    bootstrapped: bool = False


@dataclass(frozen=True)
class AutoAnalyzeDecision:
    should_analyze: bool
    reason: str
    keyword: str | None = None


class JiraMonitor:
    def __init__(self, db: Database, config: JiraConfig, skill_dir, client: JiraClientProtocol | None = None):
        self.db = db
        self.config = config
        self.skill_dir = skill_dir
        self.client = client

    def poll_once(self, *, bootstrap_if_needed: bool = True, now: datetime | None = None) -> JiraPollResult:
        poll_started = now or datetime.now(timezone.utc)
        if poll_started.tzinfo is None:
            poll_started = poll_started.replace(tzinfo=timezone.utc)
        poll_started = poll_started.astimezone(timezone.utc)

        cursor_raw = self.db.get_state(CURSOR_KEY)
        if cursor_raw is None and bootstrap_if_needed:
            self.db.set_state(CURSOR_KEY, poll_started.isoformat())
            self.db.log_event(
                "jira",
                "poll.bootstrapped",
                "jira monitor cursor initialized; existing history was not backfilled",
                cursor=poll_started.isoformat(),
                projects=list(self.config.projects),
                record_new_issues=self.config.record_new_issues,
                auto_analyze_new_issues=self.config.auto_analyze_new_issues,
                auto_analyze_projects=list(self._auto_analyze_projects()),
                status_summary_enabled=self.config.status_summary_enabled,
                status_summary_projects=list(self._status_summary_projects()),
                status_summary_target_statuses=list(self._status_summary_target_statuses()),
            )
            return JiraPollResult(0, 0, 0, 0, 0, 0, 0, poll_started.isoformat(), poll_started.isoformat(), bootstrapped=True)

        cursor = datetime.fromisoformat(cursor_raw) if cursor_raw else poll_started
        if cursor.tzinfo is None:
            cursor = cursor.replace(tzinfo=timezone.utc)
        query_start = cursor.astimezone(timezone.utc) - timedelta(seconds=self.config.overlap_seconds)
        client = self.client or JiraRestClient(self.skill_dir, page_size=self.config.page_size, token=self.config.token)

        should_record_new_issues = self.config.record_new_issues or self.config.auto_analyze_new_issues
        new_issues, issue_analyze_slots = self._record_new_issues(client, query_start) if should_record_new_issues else (0, 0)
        status_changed_issues, status_summary_slots = self._record_status_summaries(client, query_start)
        changed_issues, analyze_comments, comment_analyze_slots = self._record_analyze_comments(client, query_start)
        analyze_slots = comment_analyze_slots + issue_analyze_slots + status_summary_slots

        self.db.set_state(CURSOR_KEY, poll_started.isoformat())
        self.db.log_event(
            "jira",
            "poll.completed",
            "jira poll completed",
            cursor=poll_started.isoformat(),
            query_start=query_start.isoformat(),
            projects=list(self.config.projects),
            record_new_issues=self.config.record_new_issues,
            auto_analyze_new_issues=self.config.auto_analyze_new_issues,
            auto_analyze_projects=list(self._auto_analyze_projects()),
            status_summary_enabled=self.config.status_summary_enabled,
            status_summary_projects=list(self._status_summary_projects()),
            status_summary_target_statuses=list(self._status_summary_target_statuses()),
            new_issues=new_issues,
            analyze_comments=analyze_comments,
            analyze_slots=analyze_slots,
            issue_analyze_slots=issue_analyze_slots,
            status_summary_slots=status_summary_slots,
            status_changed_issues=status_changed_issues,
            changed_issues=changed_issues,
        )
        LOG.info(
            "jira poll completed",
            extra={"component": "jira"},
        )
        return JiraPollResult(
            new_issues=new_issues,
            analyze_comments=analyze_comments,
            analyze_slots=analyze_slots,
            issue_analyze_slots=issue_analyze_slots,
            status_summary_slots=status_summary_slots,
            status_changed_issues=status_changed_issues,
            changed_issues=changed_issues,
            query_start=query_start.isoformat(),
            cursor=poll_started.isoformat(),
        )

    def _record_new_issues(self, client: JiraClientProtocol, query_start: datetime) -> tuple[int, int]:
        projects = self._new_issue_projects()
        if not projects:
            self.db.log_event(
                "jira",
                "issue.poll_skipped",
                "no Jira projects selected for new issue polling",
                projects=list(self.config.projects),
                auto_analyze_projects=list(self.config.auto_analyze_projects),
                record_new_issues=self.config.record_new_issues,
                auto_analyze_new_issues=self.config.auto_analyze_new_issues,
            )
            return 0, 0
        jql = (
            f"project in ({self._project_jql(projects)}) "
            f'AND created >= "{self._jql_time(query_start)}" '
            "ORDER BY created ASC"
        )
        issues = client.search_all_issues(jql, NEW_ISSUE_FIELDS)
        inserted = 0
        slots_created = 0
        for issue in issues:
            fields = issue.get("fields") or {}
            issue_key = str(issue.get("key") or "").upper()
            if not issue_key:
                continue
            project_key = self._project_key(issue, fields).upper()
            if project_key not in projects:
                continue
            created_at = str(fields.get("created") or "")
            if created_at and parse_jira_time(created_at) < query_start:
                continue
            status = _jira_status(fields)
            observation = self.db.observe_jira_issue(
                issue_key=issue_key,
                project_key=project_key,
                summary=str(fields.get("summary") or ""),
                jira_created_at=created_at,
                jira_updated_at=str(fields.get("updated") or created_at),
                raw=issue,
                status_name=status["name"],
                status_id=status["id"],
                status_category_key=status["category_key"],
                reporter=_jira_user_display(fields.get("reporter")),
                assignee=_jira_user_display(fields.get("assignee")),
            )
            issue_inserted = observation.inserted
            if issue_inserted:
                inserted += 1
                self.db.log_event(
                    "jira",
                    "issue.created",
                    f"new Jira issue {issue_key}",
                    issue_key=issue_key,
                    project_key=project_key,
                    summary=str(fields.get("summary") or ""),
                    jira_created_at=created_at,
                )
            decision = self._auto_analyze_decision(fields=fields, project_key=project_key)
            if decision.should_analyze:
                slot, slot_created = self.db.ensure_jira_issue_analyze_slot_for_issue(issue_key)
                if slot_created:
                    slots_created += 1
                    self.db.log_event(
                        "jira",
                        "issue_analyze.slot_created",
                        f"created Codex slot for new Jira issue {issue_key}",
                        slot.slot_id if slot else None,
                        issue_key=issue_key,
                        project_key=project_key,
                    )
                elif slot is None:
                    self.db.log_event(
                        "jira",
                        "issue_analyze.slot_missing",
                        f"failed to create Codex slot for new Jira issue {issue_key}",
                        issue_key=issue_key,
                        project_key=project_key,
                    )
            elif decision.reason == "skip_keyword" and issue_inserted:
                self.db.log_event(
                    "jira",
                    "issue_analyze.skipped",
                    f"skipped auto-analysis for new Jira issue {issue_key}",
                    issue_key=issue_key,
                    project_key=project_key,
                    reason=decision.reason,
                    keyword=decision.keyword,
                )
        return inserted, slots_created

    def _record_status_summaries(self, client: JiraClientProtocol, query_start: datetime) -> tuple[int, int]:
        if not self.config.status_summary_enabled:
            return 0, 0
        projects = self._status_summary_projects()
        targets = self._status_summary_target_statuses()
        if not projects or not targets:
            self.db.log_event(
                "jira",
                "status_summary.poll_skipped",
                "no Jira projects or target statuses selected for status summaries",
                projects=list(projects),
                target_statuses=list(targets),
            )
            return 0, 0

        jql = (
            f"project in ({self._project_jql(projects)}) "
            f'AND updated >= "{self._jql_time(query_start)}" '
            "ORDER BY updated ASC"
        )
        issues = client.search_all_issues(jql, STATUS_SUMMARY_FIELDS)
        status_changed = 0
        slots_created = 0
        target_set = {status.casefold() for status in targets}
        for issue in issues:
            fields = issue.get("fields") or {}
            issue_key = str(issue.get("key") or "").upper()
            if not issue_key:
                continue
            project_key = self._project_key(issue, fields).upper()
            if project_key not in projects:
                continue
            updated_at = str(fields.get("updated") or fields.get("created") or "")
            if updated_at and parse_jira_time(updated_at) < query_start:
                continue
            status = _jira_status(fields)
            observation = self.db.observe_jira_issue(
                issue_key=issue_key,
                project_key=project_key,
                summary=str(fields.get("summary") or ""),
                jira_created_at=str(fields.get("created") or updated_at),
                jira_updated_at=updated_at,
                raw=issue,
                status_name=status["name"],
                status_id=status["id"],
                status_category_key=status["category_key"],
                reporter=_jira_user_display(fields.get("reporter")),
                assignee=_jira_user_display(fields.get("assignee")),
            )
            current_status = observation.current_status_name or ""
            if not observation.status_changed or current_status.casefold() not in target_set:
                continue
            status_changed += 1
            skip_keyword = self._matching_auto_analyze_skip_keyword(fields)
            if skip_keyword:
                self.db.log_event(
                    "jira",
                    "status_summary.skipped",
                    f"skipped Jira status summary for {issue_key}",
                    issue_key=issue_key,
                    project_key=project_key,
                    previous_status=observation.previous_status_name,
                    status=current_status,
                    reason="skip_keyword",
                    keyword=skip_keyword,
                )
                continue
            slot, slot_created = self.db.ensure_jira_status_summary_slot_for_issue(
                issue_key,
                previous_status_name=observation.previous_status_name,
                status_name=current_status,
                jira_updated_at=updated_at,
            )
            if slot_created:
                slots_created += 1
                self.db.log_event(
                    "jira",
                    "status_summary.slot_created",
                    f"created Codex slot for Jira status summary {issue_key} -> {current_status}",
                    slot.slot_id if slot else None,
                    issue_key=issue_key,
                    project_key=project_key,
                    previous_status=observation.previous_status_name,
                    status=current_status,
                    status_id=observation.current_status_id,
                    status_category_key=observation.current_status_category_key,
                )
            elif slot is None:
                self.db.log_event(
                    "jira",
                    "status_summary.slot_missing",
                    f"failed to create Codex slot for Jira status summary {issue_key}",
                    issue_key=issue_key,
                    project_key=project_key,
                    previous_status=observation.previous_status_name,
                    status=current_status,
                )
        return status_changed, slots_created

    def _record_analyze_comments(self, client: JiraClientProtocol, query_start: datetime) -> tuple[int, int, int]:
        projects = self._monitored_projects()
        if not projects:
            self.db.log_event(
                "jira",
                "comment.poll_skipped",
                "no Jira projects selected for analysis comment polling",
            )
            return 0, 0, 0
        jql = (
            f"project in ({self._project_jql(projects)}) "
            f'AND updated >= "{self._jql_time(query_start)}" '
            "ORDER BY updated ASC"
        )
        issues = client.search_all_issues(jql, ["key", "project", "summary", "updated"])
        inserted = 0
        slots_created = 0
        changed_issues = 0
        for issue in issues:
            fields = issue.get("fields") or {}
            issue_key = str(issue.get("key") or "").upper()
            if not issue_key:
                continue
            project_key = self._project_key(issue, fields).upper()
            if project_key not in projects:
                continue
            changed_issues += 1
            for comment in client.list_comments(issue_key):
                body = jira_comment_body_text(comment)
                trigger = self._analysis_comment_trigger(comment, body)
                if trigger is None:
                    continue
                created_at = str(comment.get("created") or "")
                updated_at = str(comment.get("updated") or created_at)
                if max(parse_jira_time(created_at), parse_jira_time(updated_at)) < query_start:
                    continue
                comment_id = str(comment.get("id") or "")
                if not comment_id:
                    continue
                author = str((comment.get("author") or {}).get("displayName") or "")
                comment_inserted = self.db.insert_jira_comment(
                    comment_id=comment_id,
                    issue_key=issue_key,
                    project_key=project_key,
                    author=author,
                    body=body,
                    jira_created_at=created_at,
                    jira_updated_at=updated_at,
                    raw=comment,
                )
                if comment_inserted:
                    inserted += 1
                    self.db.log_event(
                        "jira",
                        "comment.analyze",
                        f"Jira analysis comment {issue_key}#{comment_id}",
                        issue_key=issue_key,
                        project_key=project_key,
                        comment_id=comment_id,
                        trigger=trigger,
                        author=author,
                        jira_created_at=created_at,
                        jira_updated_at=updated_at,
                    )
                slot, slot_created = self.db.ensure_jira_analyze_slot_for_comment(comment_id)
                if slot_created:
                    slots_created += 1
                    self.db.log_event(
                        "jira",
                        "analyze.slot_created",
                        f"created Codex slot for Jira analysis comment {issue_key}#{comment_id}",
                        slot.slot_id if slot else None,
                        issue_key=issue_key,
                        project_key=project_key,
                        comment_id=comment_id,
                        trigger=trigger,
                    )
                elif slot is None:
                    self.db.log_event(
                        "jira",
                        "analyze.slot_missing",
                        f"failed to create Codex slot for Jira analysis comment {issue_key}#{comment_id}",
                        issue_key=issue_key,
                        project_key=project_key,
                        comment_id=comment_id,
                        trigger=trigger,
                    )
        return changed_issues, inserted, slots_created

    def _analysis_comment_trigger(self, comment: dict, body: str) -> str | None:
        if self._is_breakwater_analysis_comment(body):
            return None
        if body.lstrip().lower().startswith("/analyze"):
            return "slash_analyze"
        if jira_comment_mentions_target(comment, self.config.bot_mention_keys):
            return "bot_mention"
        return None

    def _is_breakwater_analysis_comment(self, body: str) -> bool:
        marker_prefix = self.config.analysis_marker_prefix.strip()
        return bool(marker_prefix and marker_prefix in body)

    def _project_jql(self, projects: tuple[str, ...] | None = None) -> str:
        return ", ".join(f'"{project}"' for project in (projects or self._monitored_projects()))

    def _monitored_projects(self) -> tuple[str, ...]:
        return tuple(str(project).upper() for project in self.config.projects)

    def _auto_analyze_projects(self) -> tuple[str, ...]:
        projects = self.config.auto_analyze_projects or self.config.projects
        monitored = set(self._monitored_projects())
        selected: list[str] = []
        for project in projects:
            normalized = str(project).upper()
            if normalized in monitored and normalized not in selected:
                selected.append(normalized)
        return tuple(selected)

    def _new_issue_projects(self) -> tuple[str, ...]:
        if self.config.record_new_issues:
            return self._monitored_projects()
        return self._auto_analyze_projects()

    def _status_summary_projects(self) -> tuple[str, ...]:
        projects = self.config.status_summary_projects or self.config.projects
        monitored = set(self._monitored_projects())
        selected: list[str] = []
        for project in projects:
            normalized = str(project).upper()
            if normalized in monitored and normalized not in selected:
                selected.append(normalized)
        return tuple(selected)

    def _status_summary_target_statuses(self) -> tuple[str, ...]:
        statuses = self.config.status_summary_target_statuses or DEFAULT_JIRA_STATUS_SUMMARY_TARGET_STATUSES
        selected: list[str] = []
        seen: set[str] = set()
        for status in statuses:
            normalized = str(status).strip()
            key = normalized.casefold()
            if normalized and key not in seen:
                selected.append(normalized)
                seen.add(key)
        return tuple(selected)

    def _auto_analyze_decision(self, *, fields: dict, project_key: str) -> AutoAnalyzeDecision:
        if not self.config.auto_analyze_new_issues:
            return AutoAnalyzeDecision(False, "disabled")
        if project_key.upper() not in self._auto_analyze_projects():
            return AutoAnalyzeDecision(False, "project_not_selected")
        keyword = self._matching_auto_analyze_skip_keyword(fields)
        if keyword:
            return AutoAnalyzeDecision(False, "skip_keyword", keyword)
        return AutoAnalyzeDecision(True, "selected")

    def _matching_auto_analyze_skip_keyword(self, fields: dict[str, Any]) -> str | None:
        content = "\n".join(_text_values(fields.get(key)) for key in AUTO_ANALYZE_CONTENT_FIELDS).casefold()
        for keyword in AUTO_ANALYZE_SKIP_KEYWORDS:
            if keyword.casefold() in content:
                return keyword
        return None

    def _jql_time(self, value: datetime) -> str:
        return value.astimezone(JIRA_TZ).strftime("%Y-%m-%d %H:%M")

    def _project_key(self, issue: dict, fields: dict) -> str:
        project = fields.get("project") if isinstance(fields, dict) else None
        if isinstance(project, dict) and project.get("key"):
            return str(project["key"])
        return str(issue.get("key") or "").split("-", 1)[0]


def parse_jira_time(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _text_values(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(_text_values(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(_text_values(item) for item in value.values())
    return str(value)


def _jira_status(fields: dict[str, Any]) -> dict[str, str | None]:
    status = fields.get("status")
    if not isinstance(status, dict):
        return {"name": None, "id": None, "category_key": None}
    category = status.get("statusCategory")
    category_key = category.get("key") if isinstance(category, dict) else None
    return {
        "name": str(status.get("name")) if status.get("name") else None,
        "id": str(status.get("id")) if status.get("id") else None,
        "category_key": str(category_key) if category_key else None,
    }


def _jira_user_display(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("displayName", "name", "emailAddress", "key", "accountId"):
        text = str(value.get(key) or "").strip()
        if text:
            return text
    return None
