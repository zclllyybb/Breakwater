from __future__ import annotations

import os
import subprocess
from typing import Any

import requests


DEFAULT_GITHUB_API_URL = "https://api.github.com"


class GitHubRestClient:
    def __init__(
        self,
        *,
        token: str | None = None,
        api_url: str = DEFAULT_GITHUB_API_URL,
        page_size: int = 100,
    ):
        self.base_url = api_url.rstrip("/")
        self.page_size = max(1, min(int(page_size), 100))
        self.token = token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or gh_auth_token()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        if self.token:
            self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def list_repository_issues_since(self, repo_full_name: str, since_iso: str) -> list[dict[str, Any]]:
        owner, repo = split_repo_full_name(repo_full_name)
        issues: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._get_json(
                f"/repos/{owner}/{repo}/issues",
                params={
                    "state": "all",
                    "since": since_iso,
                    "sort": "created",
                    "direction": "asc",
                    "per_page": self.page_size,
                    "page": page,
                },
            )
            page_items = list(data or [])
            issues.extend(page_items)
            if len(page_items) < self.page_size:
                break
            page += 1
        return issues

    def list_issue_comments(self, repo_full_name: str, issue_number: int) -> list[dict[str, Any]]:
        owner, repo = split_repo_full_name(repo_full_name)
        comments: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._get_json(
                f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
                params={"per_page": self.page_size, "page": page},
            )
            page_items = list(data or [])
            comments.extend(page_items)
            if len(page_items) < self.page_size:
                break
            page += 1
        return comments

    def create_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("missing GITHUB_TOKEN or GH_TOKEN for GitHub issue comment creation")
        owner, repo = split_repo_full_name(repo_full_name)
        return self._post_json(f"/repos/{owner}/{repo}/issues/{issue_number}/comments", {"body": body})

    def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(f"{self.base_url}{path}", json=payload, timeout=30)
        response.raise_for_status()
        return response.json()


def split_repo_full_name(repo_full_name: str) -> tuple[str, str]:
    parts = str(repo_full_name).strip().strip("/").split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"GitHub repository must be in owner/name form: {repo_full_name}")
    return parts[0], parts[1]


def gh_auth_token() -> str:
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError, TimeoutError):
        return ""
    return result.stdout.strip()
