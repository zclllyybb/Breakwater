from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from .config import DEFAULT_JIRA_SKILL_DIR


DEFAULT_JIRA_URL = ""


class JiraRestClient:
    def __init__(
        self,
        skill_dir: Path = DEFAULT_JIRA_SKILL_DIR,
        page_size: int = 100,
        token: str | None = None,
        url: str | None = None,
        auth_mode: str | None = None,
        user: str | None = None,
    ):
        self.skill_dir = skill_dir
        self.page_size = page_size
        config = self._load_config()
        self.base_url = (url or config.get("JIRA_URL", DEFAULT_JIRA_URL)).rstrip("/")
        if not self.base_url:
            raise RuntimeError("missing JIRA_URL for Jira monitoring")
        self.auth_mode = auth_mode or config.get("JIRA_AUTH", "bearer")
        self.token = token or config.get("JIRA_TOKEN", "")
        self.user = user or config.get("JIRA_USER", "")
        if not self.token:
            raise RuntimeError("missing JIRA_TOKEN for Jira monitoring")
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.request_auth: HTTPBasicAuth | None = None
        if self.auth_mode == "basic":
            if not self.user:
                raise RuntimeError("JIRA_AUTH=basic requires JIRA_USER")
            self.request_auth = HTTPBasicAuth(self.user, self.token)
        else:
            self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    def search_all_issues(self, jql: str, fields: list[str]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        start_at = 0
        while True:
            payload = {
                "jql": jql,
                "fields": fields,
                "startAt": start_at,
                "maxResults": self.page_size,
            }
            data = self._post_json("/rest/api/2/search", payload)
            page_items = list(data.get("issues") or [])
            issues.extend(page_items)
            total = int(data.get("total", len(issues)))
            if not page_items or len(issues) >= total:
                break
            start_at += len(page_items)
        return issues

    def list_comments(self, issue_key: str) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        start_at = 0
        while True:
            data = self._get_json(
                f"/rest/api/2/issue/{issue_key}/comment",
                params={"startAt": start_at, "maxResults": self.page_size},
            )
            page_items = list(data.get("comments") or [])
            comments.extend(page_items)
            total = int(data.get("total", len(comments)))
            if not page_items or len(comments) >= total:
                break
            start_at += len(page_items)
        return comments

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(f"{self.base_url}{path}", auth=self.request_auth, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}{path}", auth=self.request_auth, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def _load_config(self) -> dict[str, str]:
        config: dict[str, str] = {}
        env_path = self.skill_dir / ".env"
        if not env_path.exists():
            env_path = Path.home() / ".env"
        if env_path.exists():
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    config[key] = value
        config.update(os.environ)
        return config
