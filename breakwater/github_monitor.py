from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .config import GitHubConfig
from .db import Database, normalize_repo_full_name
from .github_adapter import GitHubRestClient


LOG = logging.getLogger(__name__)
BUG_ISSUE_TITLE_PREFIX = "[bug]"


class GitHubClientProtocol(Protocol):
    def list_repository_issues_since(self, repo_full_name: str, since_iso: str) -> list[dict]: ...


@dataclass(frozen=True)
class GitHubPollResult:
    new_issues: int
    analyze_slots: int
    repositories: tuple[str, ...]
    query_start: str
    cursor: str
    bootstrapped: bool = False


class GitHubMonitor:
    def __init__(self, db: Database, config: GitHubConfig, client: GitHubClientProtocol | None = None):
        self.db = db
        self.config = config
        self.client = client

    def poll_once(self, *, bootstrap_if_needed: bool = True, now: datetime | None = None) -> GitHubPollResult:
        poll_started = now or datetime.now(timezone.utc)
        if poll_started.tzinfo is None:
            poll_started = poll_started.replace(tzinfo=timezone.utc)
        poll_started = poll_started.astimezone(timezone.utc)

        repositories = tuple(normalize_repo_full_name(repo) for repo in self.config.repositories if str(repo).strip())
        if not repositories:
            return GitHubPollResult(0, 0, (), poll_started.isoformat(), poll_started.isoformat())

        bootstrapped = False
        if bootstrap_if_needed:
            missing = [repo for repo in repositories if self.db.get_state(_cursor_key(repo)) is None]
            if missing:
                for repo in missing:
                    self.db.set_state(_cursor_key(repo), poll_started.isoformat())
                    self.db.set_state(_bootstrap_key(repo), poll_started.isoformat())
                self.db.log_event(
                    "github",
                    "poll.bootstrapped",
                    "github monitor cursor initialized; existing history was not backfilled",
                    cursor=poll_started.isoformat(),
                    repositories=missing,
                )
                bootstrapped = True
                if len(missing) == len(repositories):
                    return GitHubPollResult(0, 0, repositories, poll_started.isoformat(), poll_started.isoformat(), bootstrapped=True)

        client = self.client or GitHubRestClient(
            token=self.config.token,
            api_url=self.config.api_url,
            page_size=self.config.page_size,
        )
        new_issues = 0
        analyze_slots = 0
        earliest_query_start = poll_started
        for repo in repositories:
            cursor_raw = self.db.get_state(_cursor_key(repo))
            cursor = datetime.fromisoformat(cursor_raw) if cursor_raw else poll_started
            if cursor.tzinfo is None:
                cursor = cursor.replace(tzinfo=timezone.utc)
            query_start = cursor.astimezone(timezone.utc) - timedelta(seconds=self.config.overlap_seconds)
            bootstrap_raw = self.db.get_state(_bootstrap_key(repo))
            bootstrap_at = datetime.fromisoformat(bootstrap_raw) if bootstrap_raw else datetime.min.replace(tzinfo=timezone.utc)
            if bootstrap_at.tzinfo is None:
                bootstrap_at = bootstrap_at.replace(tzinfo=timezone.utc)
            created_after = max(query_start, bootstrap_at.astimezone(timezone.utc))
            earliest_query_start = min(earliest_query_start, query_start)
            repo_new_issues, repo_analyze_slots = self._record_new_issues(client, repo, query_start, created_after)
            new_issues += repo_new_issues
            analyze_slots += repo_analyze_slots
            self.db.set_state(_cursor_key(repo), poll_started.isoformat())
            if bootstrap_raw is None:
                self.db.set_state(_bootstrap_key(repo), poll_started.isoformat())

        self.db.log_event(
            "github",
            "poll.completed",
            "github poll completed",
            cursor=poll_started.isoformat(),
            query_start=earliest_query_start.isoformat(),
            repositories=list(repositories),
            new_issues=new_issues,
            analyze_slots=analyze_slots,
        )
        LOG.info("github poll completed", extra={"component": "github"})
        return GitHubPollResult(
            new_issues=new_issues,
            analyze_slots=analyze_slots,
            repositories=repositories,
            query_start=earliest_query_start.isoformat(),
            cursor=poll_started.isoformat(),
            bootstrapped=bootstrapped,
        )

    def _record_new_issues(
        self,
        client: GitHubClientProtocol,
        repo_full_name: str,
        query_start: datetime,
        created_after: datetime,
    ) -> tuple[int, int]:
        issues = client.list_repository_issues_since(repo_full_name, _github_time(query_start))
        inserted = 0
        slots_created = 0
        for issue in issues:
            if issue.get("pull_request"):
                continue
            created_at = str(issue.get("created_at") or "")
            if parse_github_time(created_at) < created_after:
                continue
            issue_number = int(issue.get("number") or 0)
            if issue_number <= 0:
                continue
            title = str(issue.get("title") or "")
            if not is_bug_issue_title(title):
                continue
            labels = [label for label in list(issue.get("labels") or []) if isinstance(label, dict)]
            if self.db.insert_github_issue(
                repo_full_name=repo_full_name,
                issue_number=issue_number,
                node_id=str(issue.get("node_id") or issue.get("id") or ""),
                title=title,
                body=str(issue.get("body") or ""),
                author=str((issue.get("user") or {}).get("login") or ""),
                state=str(issue.get("state") or ""),
                html_url=str(issue.get("html_url") or ""),
                labels=labels,
                github_created_at=created_at,
                github_updated_at=str(issue.get("updated_at") or created_at),
                raw=issue,
            ):
                inserted += 1
                self.db.log_event(
                    "github",
                    "issue.created",
                    f"new GitHub issue {repo_full_name}#{issue_number}",
                    repo_full_name=repo_full_name,
                    issue_number=issue_number,
                    title=title,
                    github_created_at=created_at,
                )
            if self.config.auto_analyze:
                slot, slot_created = self.db.ensure_github_issue_slot_for_issue(repo_full_name, issue_number)
                if slot_created:
                    slots_created += 1
                    self.db.log_event(
                        "github",
                        "analyze.slot_created",
                        f"created Codex slot for GitHub issue {repo_full_name}#{issue_number}",
                        slot.slot_id if slot else None,
                        repo_full_name=repo_full_name,
                        issue_number=issue_number,
                    )
        return inserted, slots_created


def _cursor_key(repo_full_name: str) -> str:
    return f"github:{repo_full_name}.last_success_at"


def _bootstrap_key(repo_full_name: str) -> str:
    return f"github:{repo_full_name}.bootstrap_at"


def is_bug_issue_title(title: str) -> bool:
    return title.lower().startswith(BUG_ISSUE_TITLE_PREFIX)


def _github_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_github_time(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
