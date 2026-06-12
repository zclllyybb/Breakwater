from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import ProxyConfig


PROXY_ENV_KEYS = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)


def proxy_environment(config: ProxyConfig) -> dict[str, str]:
    if not config.enabled:
        return {}
    env = {
        "http_proxy": config.http,
        "https_proxy": config.https,
        "all_proxy": config.all,
        "no_proxy": config.no_proxy,
    }
    env.update({key.upper(): value for key, value in env.items()})
    return env


def apply_proxy_environment(config: ProxyConfig) -> dict[str, str]:
    env = proxy_environment(config)
    if not config.enabled:
        for key in PROXY_ENV_KEYS:
            os.environ.pop(key, None)
        return env
    os.environ.update(env)
    return env


def apply_proxy_to_env(env: dict[str, str], config: ProxyConfig) -> dict[str, str]:
    for key in PROXY_ENV_KEYS:
        env.pop(key, None)
    env.update(proxy_environment(config))
    return env


def jira_token_environment(token: str | None = None) -> dict[str, str]:
    effective_token = token or os.getenv("JIRA_TOKEN")
    return {"JIRA_TOKEN": str(effective_token)} if effective_token else {}


def apply_jira_token_to_env(env: dict[str, str], token: str | None = None) -> dict[str, str]:
    env.update(jira_token_environment(token))
    return env


def codex_state_db_path() -> Path:
    return Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "state_5.sqlite"


def recent_codex_threads(
    limit: int = 12,
    *,
    cwd: str | Path | None = None,
    breakwater_only: bool = False,
    service_name: str = "breakwater",
    thread_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    path = codex_state_db_path()
    if not path.exists():
        return []
    thread_id_list = [thread_id for thread_id in dict.fromkeys(thread_ids or []) if thread_id]
    if thread_ids is not None and not thread_id_list:
        return []
    uri = f"file:{path}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=0.2) as conn:
            conn.row_factory = sqlite3.Row
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
            filters: list[str] = ["archived = 0"]
            params: list[Any] = []
            if thread_id_list:
                placeholders = ", ".join("?" for _ in thread_id_list)
                filters.append(f"id IN ({placeholders})")
                params.extend(thread_id_list)
            if cwd is not None:
                filters.append("cwd = ?")
                params.append(str(cwd))
            if breakwater_only and not thread_id_list:
                if "service_name" in columns:
                    filters.append("service_name = ?")
                elif "serviceName" in columns:
                    filters.append("serviceName = ?")
                else:
                    filters.append("source = ?")
                params.append(service_name)
            where_clause = " WHERE " + " AND ".join(filters) if filters else ""
            params.append(limit)
            rows = conn.execute(
                f"""
                SELECT id, source, model, reasoning_effort, cwd, approval_mode,
                       sandbox_policy, title, preview, first_user_message, thread_source,
                       tokens_used, created_at_ms, updated_at_ms
                FROM threads
                {where_clause}
                ORDER BY updated_at_ms DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "id": row["id"],
            "source": row["source"],
            "model": row["model"],
            "reasoning_effort": row["reasoning_effort"],
            "cwd": row["cwd"],
            "approval_mode": row["approval_mode"],
            "sandbox_policy": row["sandbox_policy"],
            "title": row["title"],
            "preview": row["preview"],
            "first_user_message": row["first_user_message"],
            "thread_source": row["thread_source"],
            "tokens_used": row["tokens_used"],
            "created_at": _format_ms(row["created_at_ms"]),
            "updated_at": _format_ms(row["updated_at_ms"]),
        }
        for row in rows
    ]


def _format_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
