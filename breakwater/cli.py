from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from string import Template

import yaml

from .cases import CaseResolver
from .config import (
    AdminWebConfig,
    AppConfig,
    CodexConfig,
    DEFAULT_CODEX_WS,
    DEFAULT_DB_PATH,
    DEFAULT_DEVELOP_SKILL_DIR,
    DEFAULT_JIRA_STATUS_SUMMARY_TARGET_STATUSES,
    DEFAULT_JIRA_SKILL_DIR,
    DEFAULT_LARK_CLI_CONFIG,
    PROJECT_ROOT,
    DevelopConfig,
    GitHubConfig,
    JiraConfig,
    LarkConfig,
    ProxyConfig,
    db_path_from_env,
)
from .admin_web import BreakwaterAdminServer, hash_admin_password
from .db import Database, utc_now
from .github_adapter import GitHubRestClient
from .github_monitor import GitHubMonitor
from .jira_monitor import JiraMonitor
from .logging import configure_logging
from .runtime import apply_jira_token_to_env, apply_proxy_to_env, proxy_environment, recent_codex_threads
from .service import BreakwaterService
from .sources import GITHUB_ISSUE_ANALYZE_SOURCE, is_direct_jira_analysis_source
from .web import BreakwaterWebServer


DEFAULT_PID_FILE = PROJECT_ROOT / ".breakwater" / "breakwater.pid"
DEFAULT_LOG_FILE = PROJECT_ROOT / ".breakwater" / "logs" / "breakwater.log"
SENSITIVE_PROMPT_KEYS = {"JIRA_TOKEN", "jira_token", "GITHUB_TOKEN", "github_token"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="breakwater")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--log-level", default="INFO")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="run Lark -> Codex -> Lark demo service")
    add_runtime_args(serve_parser)
    add_lark_args(serve_parser)

    start_parser = subparsers.add_parser("start", help="start Breakwater as a background service")
    add_runtime_args(start_parser)
    add_lark_args(start_parser)
    start_parser.add_argument("--pid-file", type=Path, default=None)
    start_parser.add_argument("--log-file", type=Path, default=None)

    stop_parser = subparsers.add_parser("stop", help="stop the background Breakwater service")
    stop_parser.add_argument("--pid-file", type=Path, default=None)
    stop_parser.add_argument("--timeout", type=float, default=10.0)
    stop_parser.add_argument("--force", action="store_true")

    reply_parser = subparsers.add_parser("reply", help="record a reply for a Breakwater slot")
    reply_parser.add_argument("slot_id")
    reply_parser.add_argument("message_words", nargs="*")
    reply_parser.add_argument("--message", default=None)
    reply_parser.add_argument("--message-file", type=Path, default=None)

    status_parser = subparsers.add_parser("status", help="print recent slots")
    status_parser.add_argument("--limit", type=int, default=20)
    status_parser.add_argument("--json", action="store_true")

    cases_parser = subparsers.add_parser("cases", help="print recent analysis cases")
    cases_parser.add_argument("--limit", type=int, default=20)
    cases_parser.add_argument("--json", action="store_true")

    case_parser = subparsers.add_parser("case", help="print one analysis case timeline")
    case_parser.add_argument("identifier", help="case id, Jira key, slot id, or Codex session id")
    case_parser.add_argument("--json", action="store_true")

    case_show_parser = subparsers.add_parser("case-show", help="alias for case")
    case_show_parser.add_argument("identifier", help="case id, Jira key, slot id, or Codex session id")
    case_show_parser.add_argument("--json", action="store_true")

    subparsers.add_parser("init-db", help="initialize SQLite schema")

    web_parser = subparsers.add_parser("web", help="run status page only")
    web_parser.add_argument("--host", default="127.0.0.1")
    web_parser.add_argument("--port", type=int, default=8765)

    admin_web_parser = subparsers.add_parser("admin-web", help="run password-protected admin page only")
    admin_web_parser.add_argument("--host", default="127.0.0.1")
    admin_web_parser.add_argument("--port", type=int, default=8766)
    admin_web_parser.add_argument("--password-hash", default=None)
    admin_web_parser.add_argument("--session-secret", default=None)
    admin_web_parser.add_argument("--session-ttl-seconds", type=int, default=86400)
    admin_web_parser.add_argument("--jira-base-url", default=None)

    admin_hash_parser = subparsers.add_parser("admin-hash-password", help="create a Breakwater admin password hash")
    admin_hash_parser.add_argument("--password", default=None)

    automation_report_parser = subparsers.add_parser("automation-report", help="record structured automation metadata for a Jira slot")
    automation_report_parser.add_argument("slot_id")
    automation_report_parser.add_argument("--json-file", type=Path, required=True)

    jira_poll_parser = subparsers.add_parser("jira-poll", help="poll Jira once and record new issues/analyze comments")
    jira_poll_parser.add_argument("--jira-skill-dir", type=Path, default=None)
    jira_poll_parser.add_argument("--jira-token", default=None, help="defaults to JIRA_TOKEN")
    jira_poll_parser.add_argument("--project", action="append", default=None)
    jira_poll_parser.add_argument("--overlap-seconds", type=int, default=180)
    jira_poll_parser.add_argument("--record-new-issues", action="store_true", default=False)
    jira_poll_parser.add_argument("--auto-analyze-new-issues", action="store_true", default=False)
    jira_poll_parser.add_argument("--auto-analyze-project", action="append", default=None)
    jira_poll_parser.add_argument("--status-summary", action="store_true", default=False)
    jira_poll_parser.add_argument("--status-summary-project", action="append", default=None)
    jira_poll_parser.add_argument("--status-summary-target-status", action="append", default=None)
    jira_poll_parser.add_argument(
        "--bot-mention-key",
        "--jira-bot-mention-key",
        dest="bot_mention_key",
        action="append",
        default=None,
        help="Jira structured mention key/accountId/user key that should trigger analysis",
    )
    jira_poll_parser.add_argument("--bootstrap", action="store_true", help="initialize cursor without backfill when missing")
    jira_poll_parser.add_argument("--no-bootstrap", action="store_true", help="poll immediately even when cursor is missing")

    github_poll_parser = subparsers.add_parser("github-poll", help="poll GitHub once and record new issues")
    github_poll_parser.add_argument("--repo", action="append", default=None, help="GitHub repository in owner/name form")
    github_poll_parser.add_argument("--github-token", default=None, help="defaults to GITHUB_TOKEN or GH_TOKEN")
    github_poll_parser.add_argument("--api-url", default="https://api.github.com")
    github_poll_parser.add_argument("--overlap-seconds", type=int, default=180)
    github_poll_parser.add_argument("--page-size", type=int, default=100)
    github_poll_parser.add_argument("--auto-analyze", action="store_true", default=False)
    github_poll_parser.add_argument("--bootstrap", action="store_true", help="initialize cursor without backfill when missing")
    github_poll_parser.add_argument("--no-bootstrap", action="store_true", help="poll immediately even when cursor is missing")

    github_comment_parser = subparsers.add_parser("github-comment", help="post a GitHub issue comment for a Breakwater slot")
    github_comment_parser.add_argument("slot_id")
    github_comment_parser.add_argument("body_words", nargs="*")
    github_comment_parser.add_argument("--body", default=None)
    github_comment_parser.add_argument("--body-file", type=Path, default=None)
    github_comment_parser.add_argument("--github-token", default=None, help="defaults to GITHUB_TOKEN or GH_TOKEN")
    github_comment_parser.add_argument("--api-url", default="https://api.github.com")

    args = parser.parse_args(argv)
    configure_logging(args.log_level)

    if args.command == "init-db":
        db_path = resolve_db_path(args.db)
        Database(db_path).init()
        print(f"initialized {db_path}")
        return 0
    if args.command == "reply":
        return cmd_reply(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "cases":
        return cmd_cases(args)
    if args.command in {"case", "case-show"}:
        return cmd_case(args)
    if args.command == "jira-poll":
        return cmd_jira_poll(args)
    if args.command == "github-poll":
        return cmd_github_poll(args)
    if args.command == "github-comment":
        return cmd_github_comment(args)
    if args.command == "web":
        return cmd_web(args)
    if args.command == "admin-web":
        return cmd_admin_web(args)
    if args.command == "admin-hash-password":
        return cmd_admin_hash_password(args)
    if args.command == "automation-report":
        return cmd_automation_report(args)
    if args.command == "start":
        return cmd_start(args)
    if args.command == "stop":
        return cmd_stop(args)
    if args.command == "serve":
        return asyncio.run(cmd_serve(args))
    raise AssertionError(args.command)


def add_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help="service config file; defaults to ./config.yaml when present")
    parser.add_argument("--codex-url", default=None)
    parser.add_argument("--codex-model", default=None)
    parser.add_argument("--codex-effort", default=None, help="reasoning effort, e.g. low/medium/high/xhigh")
    parser.add_argument("--codex-sandbox", default=None, choices=("read-only", "workspace-write", "danger-full-access"))
    parser.add_argument("--codex-workspace", type=Path, default=None, help="workspace cwd passed to Codex app-server turns")
    parser.add_argument("--max-reply-retries", type=int, default=None)
    parser.add_argument("--codex-concurrency", type=int, default=None, help="maximum Codex tasks running at once")
    parser.add_argument("--turn-timeout", type=int, default=None)
    parser.add_argument("--no-start-codex-server", action="store_true", default=None)
    parser.add_argument("--jira-skill-dir", type=Path, default=None)
    parser.add_argument("--jira-token", default=None, help="Jira token for monitoring and Codex-launched jira-issue commands; defaults to JIRA_TOKEN")
    parser.add_argument("--no-jira", action="store_true", default=None)
    parser.add_argument("--jira-record-new-issues", dest="jira_record_new_issues", action="store_true", default=None)
    parser.add_argument("--no-jira-record-new-issues", dest="jira_record_new_issues", action="store_false", default=None)
    parser.add_argument("--jira-auto-analyze-new-issues", dest="jira_auto_analyze_new_issues", action="store_true", default=None)
    parser.add_argument("--no-jira-auto-analyze-new-issues", dest="jira_auto_analyze_new_issues", action="store_false", default=None)
    parser.add_argument("--jira-auto-analyze-project", action="append", default=None)
    parser.add_argument("--jira-status-summary", dest="jira_status_summary_enabled", action="store_true", default=None)
    parser.add_argument("--no-jira-status-summary", dest="jira_status_summary_enabled", action="store_false", default=None)
    parser.add_argument("--jira-status-summary-project", action="append", default=None)
    parser.add_argument("--jira-status-summary-target-status", action="append", default=None)
    parser.add_argument(
        "--jira-bot-mention-key",
        action="append",
        default=None,
        help="Jira structured mention key/accountId/user key that should trigger analysis",
    )
    parser.add_argument("--jira-project", action="append", default=None)
    parser.add_argument("--jira-poll-interval", type=float, default=None)
    parser.add_argument("--jira-overlap-seconds", type=int, default=None)
    parser.add_argument("--no-github", action="store_true", default=None)
    parser.add_argument("--github-repo", action="append", default=None, help="GitHub repository to monitor, owner/name")
    parser.add_argument("--github-auto-analyze", dest="github_auto_analyze", action="store_true", default=None)
    parser.add_argument("--no-github-auto-analyze", dest="github_auto_analyze", action="store_false", default=None)
    parser.add_argument("--github-poll-interval", type=float, default=None)
    parser.add_argument("--github-token", default=None, help="GitHub token for monitoring/commenting; defaults to GITHUB_TOKEN or GH_TOKEN")
    parser.add_argument("--web-host", default=None)
    parser.add_argument("--web-port", type=int, default=None)
    parser.add_argument("--no-web", action="store_true", default=None)
    parser.add_argument("--admin-web", dest="admin_web_enabled", action="store_true", default=None)
    parser.add_argument("--no-admin-web", dest="admin_web_enabled", action="store_false", default=None)
    parser.add_argument("--admin-web-host", default=None)
    parser.add_argument("--admin-web-port", type=int, default=None)
    parser.add_argument("--admin-password-hash", default=None)
    parser.add_argument("--admin-session-secret", default=None)
    parser.add_argument("--no-proxy", action="store_true", default=None)
    parser.add_argument("--proxy-http", default=None)
    parser.add_argument("--proxy-https", default=None)
    parser.add_argument("--proxy-all", default=None)


def add_lark_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--chat-id", default=None, help="optional Lark chat_id filter")
    parser.add_argument("--lark-app-id", default=None)
    parser.add_argument("--lark-app-secret", default=None)
    parser.add_argument("--lark-cli-config", type=Path, default=None)


def build_config(args: argparse.Namespace) -> AppConfig:
    file_config, config_path = load_service_config(getattr(args, "config", None))
    codex_config = dict(file_config.get("codex") or {})
    lark_config = dict(file_config.get("lark") or {})
    jira_config = dict(file_config.get("jira") or {})
    github_config = dict(file_config.get("github") or {})
    develop_config = dict(file_config.get("develop") or {})
    web_config = dict(file_config.get("web") or {})
    admin_web_config = dict(file_config.get("admin_web") or file_config.get("admin") or {})
    queue_config = dict(file_config.get("queue") or {})
    proxy_config = dict(file_config.get("proxy") or {})

    codex = CodexConfig(
        ws_url=str(first_present(args.codex_url, codex_config.get("url"), codex_config.get("ws_url"), DEFAULT_CODEX_WS)),
        model=first_present(args.codex_model, codex_config.get("model")),
        effort=first_present(args.codex_effort, codex_config.get("effort")),
        sandbox=str(first_present(args.codex_sandbox, codex_config.get("sandbox"), "workspace-write")),
        max_reply_retries=int(first_present(args.max_reply_retries, codex_config.get("max_reply_retries"), 2)),
        turn_timeout_seconds=int(first_present(args.turn_timeout, codex_config.get("turn_timeout_seconds"), codex_config.get("turn_timeout"), 180)),
        start_server=resolve_enabled(args.no_start_codex_server, codex_config.get("start_server"), True),
    )
    lark = LarkConfig(
        app_id=first_present(getattr(args, "lark_app_id", None), lark_config.get("app_id")),
        app_secret=first_present(getattr(args, "lark_app_secret", None), lark_config.get("app_secret")),
        cli_config=resolve_path(first_present(getattr(args, "lark_cli_config", None), lark_config.get("cli_config"), DEFAULT_LARK_CLI_CONFIG), config_path),
        chat_id=first_present(getattr(args, "chat_id", None), lark_config.get("chat_id")),
    )
    projects = args.jira_project or jira_config.get("projects") or ()
    auto_analyze_projects = (
        getattr(args, "jira_auto_analyze_project", None)
        or jira_config.get("auto_analyze_projects")
        or jira_config.get("auto_analyze_project")
        or ()
    )
    status_summary_projects = (
        getattr(args, "jira_status_summary_project", None)
        or jira_config.get("status_summary_projects")
        or jira_config.get("status_summary_project")
        or ()
    )
    status_summary_target_statuses = (
        getattr(args, "jira_status_summary_target_status", None)
        or jira_config.get("status_summary_target_statuses")
        or jira_config.get("status_summary_target_status")
        or DEFAULT_JIRA_STATUS_SUMMARY_TARGET_STATUSES
    )
    bot_mention_keys = (
        getattr(args, "jira_bot_mention_key", None)
        or jira_config.get("bot_mention_keys")
        or jira_config.get("bot_mention_key")
        or jira_config.get("mention_keys")
        or jira_config.get("mention_key")
        or ()
    )
    jira = JiraConfig(
        enabled=resolve_enabled(args.no_jira, jira_config.get("enabled"), False),
        record_new_issues=bool(first_present(args.jira_record_new_issues, jira_config.get("record_new_issues"), jira_config.get("monitor_new_issues"), False)),
        auto_analyze_new_issues=bool(
            first_present(
                getattr(args, "jira_auto_analyze_new_issues", None),
                jira_config.get("auto_analyze_new_issues"),
                jira_config.get("auto_analyze"),
                False,
            )
        ),
        auto_analyze_projects=tuple(str(project) for project in as_list(auto_analyze_projects)),
        status_summary_enabled=bool(
            first_present(
                getattr(args, "jira_status_summary_enabled", None),
                jira_config.get("status_summary_enabled"),
                jira_config.get("status_summary"),
                False,
            )
        ),
        status_summary_projects=tuple(str(project) for project in as_list(status_summary_projects)),
        status_summary_target_statuses=tuple(str(status) for status in as_list(status_summary_target_statuses)),
        bot_mention_keys=tuple(str(key) for key in as_list(bot_mention_keys)),
        projects=tuple(str(project) for project in projects),
        poll_interval_seconds=float(first_present(args.jira_poll_interval, jira_config.get("poll_interval_seconds"), jira_config.get("poll_interval"), 60.0)),
        overlap_seconds=int(first_present(args.jira_overlap_seconds, jira_config.get("overlap_seconds"), 180)),
        page_size=int(first_present(jira_config.get("page_size"), 100)),
        token=first_present(args.jira_token, file_config.get("JIRA_TOKEN"), jira_config.get("token"), os.getenv("JIRA_TOKEN")),
        analysis_marker_prefix=str(first_present(jira_config.get("analysis_marker_prefix"), "Breakwater-Analysis-Slot")),
    )
    github_repo_config = github_config.get("repositories") or github_config.get("repos") or github_config.get("repository")
    github_repositories = tuple(str(repo) for repo in (getattr(args, "github_repo", None) or as_list(github_repo_config)))
    github = GitHubConfig(
        enabled=resolve_enabled(getattr(args, "no_github", None), github_config.get("enabled"), bool(github_repositories)),
        repositories=github_repositories,
        auto_analyze=bool(first_present(getattr(args, "github_auto_analyze", None), github_config.get("auto_analyze"), github_config.get("analyze"), False)),
        poll_interval_seconds=float(first_present(getattr(args, "github_poll_interval", None), github_config.get("poll_interval_seconds"), github_config.get("poll_interval"), 300.0)),
        overlap_seconds=int(first_present(github_config.get("overlap_seconds"), 180)),
        page_size=int(first_present(github_config.get("page_size"), 100)),
        token=first_present(getattr(args, "github_token", None), file_config.get("GITHUB_TOKEN"), github_config.get("token"), os.getenv("GITHUB_TOKEN"), os.getenv("GH_TOKEN")),
        api_url=str(first_present(github_config.get("api_url"), "https://api.github.com")),
        analysis_marker_prefix=str(first_present(github_config.get("analysis_marker_prefix"), "Breakwater-GitHub-Analysis-Slot")),
    )
    db_path = resolve_path(first_present(args.db, file_config.get("db_path"), file_config.get("db"), db_path_from_env()), config_path)
    develop = DevelopConfig(
        enabled=bool(first_present(develop_config.get("enabled"), True)),
        skill_dir=resolve_path(first_present(develop_config.get("skill_dir"), DEFAULT_DEVELOP_SKILL_DIR), config_path).resolve(),
    )
    jira_skill_dir = resolve_path(first_present(args.jira_skill_dir, jira_config.get("skill_dir"), DEFAULT_JIRA_SKILL_DIR), config_path).resolve()
    seed_variables = build_prompt_variables(file_config, db_path.resolve(), jira_skill_dir, develop.skill_dir, PROJECT_ROOT)
    codex_workspace_raw = first_present(
        getattr(args, "codex_workspace", None),
        codex_config.get("workspace"),
        codex_config.get("cwd"),
        PROJECT_ROOT,
    )
    codex_workspace = resolve_path(render_config_template(codex_workspace_raw, seed_variables), config_path).resolve()
    prompt_variables = build_prompt_variables(file_config, db_path.resolve(), jira_skill_dir, develop.skill_dir, codex_workspace)
    proxy = ProxyConfig(
        enabled=resolve_enabled(getattr(args, "no_proxy", None), proxy_config.get("enabled"), True),
        http=str(first_present(getattr(args, "proxy_http", None), proxy_config.get("http"), "http://127.0.0.1:7890")),
        https=str(first_present(getattr(args, "proxy_https", None), proxy_config.get("https"), "http://127.0.0.1:7890")),
        all=str(first_present(getattr(args, "proxy_all", None), proxy_config.get("all"), "socks5h://127.0.0.1:7890")),
        no_proxy=str(first_present(proxy_config.get("no_proxy"), "127.0.0.1,localhost")),
    )
    admin_web = AdminWebConfig(
        enabled=bool(first_present(getattr(args, "admin_web_enabled", None), admin_web_config.get("enabled"), False)),
        host=str(first_present(getattr(args, "admin_web_host", None), admin_web_config.get("host"), "127.0.0.1")),
        port=int(first_present(getattr(args, "admin_web_port", None), admin_web_config.get("port"), 8766)),
        password_hash=first_present(
            getattr(args, "admin_password_hash", None),
            admin_web_config.get("password_hash"),
            os.getenv("BREAKWATER_ADMIN_PASSWORD_HASH"),
        ),
        session_secret=first_present(
            getattr(args, "admin_session_secret", None),
            admin_web_config.get("session_secret"),
            os.getenv("BREAKWATER_ADMIN_SESSION_SECRET"),
        ),
        session_ttl_seconds=int(first_present(admin_web_config.get("session_ttl_seconds"), admin_web_config.get("session_ttl"), 86400)),
        jira_base_url=first_present(admin_web_config.get("jira_base_url"), admin_web_config.get("jira_url"), file_config.get("JIRA_URL"), os.getenv("JIRA_URL")),
    )
    return AppConfig(
        db_path=db_path.resolve(),
        codex_workspace=codex_workspace,
        codex=codex,
        lark=lark,
        jira=jira,
        github=github,
        develop=develop,
        proxy=proxy,
        admin_web=admin_web,
        jira_skill_dir=jira_skill_dir,
        prompt_variables=prompt_variables,
        codex_concurrency=max(1, int(first_present(args.codex_concurrency, queue_config.get("codex_concurrency"), queue_config.get("concurrency"), 5))),
        web_enabled=resolve_enabled(args.no_web, web_config.get("enabled"), True),
        web_host=str(first_present(args.web_host, web_config.get("host"), "127.0.0.1")),
        web_port=int(first_present(args.web_port, web_config.get("port"), 8765)),
    )


def load_service_config(config_arg: Path | None) -> tuple[dict[str, object], Path | None]:
    config_path = config_arg
    if config_path is None:
        candidate = PROJECT_ROOT / "config.yaml"
        config_path = candidate if candidate.exists() else None
    if config_path is None:
        return {}, None
    config_path = config_path.expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"config file must contain a YAML mapping: {config_path}")
    return loaded, config_path


def build_prompt_variables(
    file_config: dict[str, object],
    db_path: Path,
    jira_skill_dir: Path,
    develop_skill_dir: Path,
    codex_workspace: Path,
) -> dict[str, str]:
    variables: dict[str, str] = {}
    flatten_prompt_values(file_config, "", variables)
    configured = file_config.get("prompt_variables") or file_config.get("variables") or {}
    if isinstance(configured, dict):
        for key, value in configured.items():
            if value is not None:
                variables[str(key)] = str(value)
    for key in SENSITIVE_PROMPT_KEYS:
        variables.pop(key, None)
    variables.update(
        {
            "PROJECT_ROOT": str(PROJECT_ROOT),
            "DB_PATH": str(db_path),
            "CODEX_WORKSPACE": str(codex_workspace),
            "codex_workspace": str(codex_workspace),
            "JIRA_SKILL_DIR": str(jira_skill_dir),
            "JIRA_SKILL_PATH": str(jira_skill_dir / "SKILL.md"),
            "DEVELOP_SKILL_DIR": str(develop_skill_dir),
            "DEVELOP_SKILL_PATH": str(develop_skill_dir / "SKILL.md"),
        }
    )
    variables.setdefault("DEVELOP_DEFAULT_REPOSITORY", str(codex_workspace))
    return expand_prompt_variables(variables)


def render_config_template(value: object, variables: dict[str, str]) -> str:
    return Template(str(value)).safe_substitute(variables)


def flatten_prompt_values(value: object, prefix: str, output: dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            next_prefix = f"{prefix}_{key}" if prefix else str(key)
            flatten_prompt_values(child, next_prefix, output)
        return
    if isinstance(value, (list, tuple)):
        output[prefix.upper()] = ", ".join(str(item) for item in value)
        return
    if prefix and value is not None:
        output[prefix.upper()] = str(value)


def expand_prompt_variables(variables: dict[str, str]) -> dict[str, str]:
    expanded = dict(variables)
    for _ in range(5):
        changed = False
        for key, value in list(expanded.items()):
            new_value = Template(value).safe_substitute(expanded)
            if new_value != value:
                expanded[key] = new_value
                changed = True
        if not changed:
            break
    return expanded


def first_present(*values):
    for value in values:
        if value is not None:
            return value
    return None


def as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def resolve_enabled(no_flag: bool | None, configured: object, default: bool) -> bool:
    if no_flag is True:
        return False
    if configured is None:
        return default
    return bool(configured)


def resolve_path(value: object, config_path: Path | None) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    base = config_path.parent if config_path is not None else PROJECT_ROOT
    return base / path


def resolve_db_path(value: Path | None) -> Path:
    return (value or db_path_from_env(DEFAULT_DB_PATH)).expanduser().resolve()


async def cmd_serve(args: argparse.Namespace) -> int:
    service = BreakwaterService(build_config(args))
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, service._stop.set)
    await service.serve()
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    pid_file = resolve_daemon_path(args.pid_file, DEFAULT_PID_FILE)
    log_file = resolve_daemon_path(args.log_file, DEFAULT_LOG_FILE)
    existing = read_pid_file(pid_file)
    if existing and process_alive(existing["pid"]):
        print(f"Breakwater already running pid={existing['pid']} log={existing.get('log_file', log_file)}")
        return 0
    config = build_config(args)
    env = os.environ.copy()
    apply_proxy_to_env(env, config.proxy)
    apply_jira_token_to_env(env, config.jira.token)
    log_file = unique_log_file(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    command = build_serve_command(args)
    with log_file.open("ab") as output:
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    time.sleep(0.5)
    if process.poll() is not None:
        print(f"Breakwater failed to start, exit={process.returncode}, log={log_file}", file=sys.stderr)
        return 1
    pid_file.write_text(
        json.dumps(
            {
                "pid": process.pid,
                "log_file": str(log_file),
                "command": command,
                "started_at": time.time(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Breakwater started pid={process.pid} log={log_file}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    pid_file = resolve_daemon_path(args.pid_file, DEFAULT_PID_FILE)
    existing = read_pid_file(pid_file)
    if not existing:
        print(f"Breakwater is not running; pid file not found: {pid_file}")
        return 0
    pid = int(existing["pid"])
    if not process_alive(pid):
        pid_file.unlink(missing_ok=True)
        print(f"Breakwater pid file was stale; removed {pid_file}")
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if not process_alive(pid):
            pid_file.unlink(missing_ok=True)
            print(f"Breakwater stopped pid={pid}")
            return 0
        time.sleep(0.2)
    if args.force:
        os.kill(pid, signal.SIGKILL)
        pid_file.unlink(missing_ok=True)
        print(f"Breakwater killed pid={pid}")
        return 0
    print(f"Breakwater did not stop within {args.timeout:.1f}s; use --force if needed", file=sys.stderr)
    return 1


def cmd_reply(args: argparse.Namespace) -> int:
    if args.message_file:
        message = args.message_file.read_text()
    elif args.message is not None:
        message = args.message
    else:
        message = " ".join(args.message_words).strip()
    if not message:
        print("reply message is empty", file=sys.stderr)
        return 2
    db = Database(resolve_db_path(args.db))
    db.init()
    slot = db.get_slot(args.slot_id)
    if slot and is_direct_jira_analysis_source(slot.source):
        db.log_event(
            "jira",
            "analysis.breakwater_reply_rejected",
            "jira analysis slots must be completed by commenting through the jira-issue skill",
            args.slot_id,
        )
        print("Jira analysis slots must be completed by adding a Jira comment with the Breakwater marker", file=sys.stderr)
        return 3
    if slot and slot.source == GITHUB_ISSUE_ANALYZE_SOURCE:
        db.log_event(
            "github",
            "analysis.breakwater_reply_rejected",
            "github analysis slots must be completed by commenting through the GitHub issue comment path",
            args.slot_id,
        )
        print("github_issue_analyze slots must be completed by adding a GitHub comment with the Breakwater marker", file=sys.stderr)
        return 3
    db.record_reply_request(args.slot_id, message)
    db.log_event("reply", "reply.requested", "reply requested by CLI", args.slot_id, reply_message=message)
    print(f"reply recorded for {args.slot_id}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    db = Database(resolve_db_path(args.db))
    db.init()
    slots = db.list_slots(args.limit)
    if args.json:
        print(json.dumps([slot.__dict__ for slot in slots], ensure_ascii=False, indent=2))
        return 0
    for slot in slots:
        session = slot.codex_thread_id or "session-pending"
        print(
            f"{slot.slot_id} status={slot.status} codex={slot.codex_status} reply={slot.reply_status} "
            f"attempts={slot.codex_attempts} session={session} text={slot.incoming_text[:80]!r}"
        )
    return 0


def cmd_cases(args: argparse.Namespace) -> int:
    db = Database(resolve_db_path(args.db))
    db.init()
    cases = db.list_cases(args.limit)
    if args.json:
        print(json.dumps([case.__dict__ for case in cases], ensure_ascii=False, indent=2))
        return 0
    for case in cases:
        session = case.latest_codex_thread_id or "session-pending"
        lark_group = case.bound_lark_chat_name or case.bound_lark_chat_id or "-"
        print(
            f"{case.case_id} scope={case.scope_type}:{case.scope_key} status={case.status} "
            f"latest_slot={case.latest_slot_id or '-'} session={session} lark_group={lark_group!r} title={case.title!r}"
        )
    return 0


def cmd_case(args: argparse.Namespace) -> int:
    db = Database(resolve_db_path(args.db))
    db.init()
    resolution = CaseResolver(db).resolve_identifier(args.identifier)
    if resolution is None:
        print(f"case not found or ambiguous: {args.identifier}", file=sys.stderr)
        return 1
    case = resolution.case
    aliases = db.list_case_aliases(case.case_id)
    slots = db.list_case_slots(case.case_id)
    payload = {
        "case": case.__dict__,
        "aliases": aliases,
        "slots": [slot.__dict__ for slot in slots],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    session = case.latest_codex_thread_id or "session-pending"
    lark_group = case.bound_lark_chat_name or case.bound_lark_chat_id or "-"
    print(
        f"{case.case_id} scope={case.scope_type}:{case.scope_key} status={case.status} "
        f"latest_slot={case.latest_slot_id or '-'} session={session} lark_group={lark_group!r} title={case.title!r}"
    )
    print("aliases:")
    for alias in aliases:
        print(f"  {alias['alias_type']}={alias['alias_key']}")
    print("slots:")
    for slot in slots:
        slot_session = slot.codex_thread_id or "-"
        print(
            f"  {slot.slot_id} source={slot.source} status={slot.status} codex={slot.codex_status} "
            f"reply={slot.reply_status} session={slot_session} text={slot.incoming_text[:80]!r}"
        )
    return 0


def build_serve_command(args: argparse.Namespace) -> list[str]:
    command = [sys.executable, "-m", "breakwater.cli"]
    if args.db is not None:
        command += ["--db", str(args.db)]
    if args.log_level:
        command += ["--log-level", str(args.log_level)]
    command.append("serve")
    for name, flag in (
        ("config", "--config"),
        ("codex_url", "--codex-url"),
        ("codex_model", "--codex-model"),
        ("codex_effort", "--codex-effort"),
        ("codex_sandbox", "--codex-sandbox"),
        ("codex_workspace", "--codex-workspace"),
        ("max_reply_retries", "--max-reply-retries"),
        ("codex_concurrency", "--codex-concurrency"),
        ("turn_timeout", "--turn-timeout"),
        ("jira_skill_dir", "--jira-skill-dir"),
        ("jira_token", "--jira-token"),
        ("jira_poll_interval", "--jira-poll-interval"),
        ("jira_overlap_seconds", "--jira-overlap-seconds"),
        ("web_host", "--web-host"),
        ("web_port", "--web-port"),
        ("admin_web_host", "--admin-web-host"),
        ("admin_web_port", "--admin-web-port"),
        ("admin_password_hash", "--admin-password-hash"),
        ("admin_session_secret", "--admin-session-secret"),
        ("proxy_http", "--proxy-http"),
        ("proxy_https", "--proxy-https"),
        ("proxy_all", "--proxy-all"),
        ("chat_id", "--chat-id"),
        ("lark_app_id", "--lark-app-id"),
        ("lark_app_secret", "--lark-app-secret"),
        ("lark_cli_config", "--lark-cli-config"),
    ):
        value = getattr(args, name, None)
        if value is not None:
            command += [flag, str(value)]
    for value in getattr(args, "jira_project", None) or []:
        command += ["--jira-project", str(value)]
    for value in getattr(args, "jira_auto_analyze_project", None) or []:
        command += ["--jira-auto-analyze-project", str(value)]
    for value in getattr(args, "jira_status_summary_project", None) or []:
        command += ["--jira-status-summary-project", str(value)]
    for value in getattr(args, "jira_status_summary_target_status", None) or []:
        command += ["--jira-status-summary-target-status", str(value)]
    for value in getattr(args, "jira_bot_mention_key", None) or []:
        command += ["--jira-bot-mention-key", str(value)]
    for name, flag in (
        ("no_start_codex_server", "--no-start-codex-server"),
        ("no_jira", "--no-jira"),
        ("jira_record_new_issues", "--jira-record-new-issues"),
        ("jira_auto_analyze_new_issues", "--jira-auto-analyze-new-issues"),
        ("jira_status_summary_enabled", "--jira-status-summary"),
        ("admin_web_enabled", "--admin-web"),
        ("no_web", "--no-web"),
        ("no_proxy", "--no-proxy"),
    ):
        if getattr(args, name, None):
            command.append(flag)
    if getattr(args, "jira_record_new_issues", None) is False:
        command.append("--no-jira-record-new-issues")
    if getattr(args, "jira_auto_analyze_new_issues", None) is False:
        command.append("--no-jira-auto-analyze-new-issues")
    if getattr(args, "jira_status_summary_enabled", None) is False:
        command.append("--no-jira-status-summary")
    if getattr(args, "admin_web_enabled", None) is False:
        command.append("--no-admin-web")
    return command


def resolve_daemon_path(value: Path | None, default: Path) -> Path:
    return (value or default).expanduser().resolve()


def unique_log_file(base: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    suffix = base.suffix or ".log"
    stem = base.stem if base.suffix else base.name
    return base.with_name(f"{stem}-{timestamp}{suffix}")


def read_pid_file(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["pid"] = int(data["pid"])
        return data
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cmd_jira_poll(args: argparse.Namespace) -> int:
    db = Database(resolve_db_path(args.db))
    db.init()
    projects = tuple(args.project or ())
    monitor = JiraMonitor(
        db,
        JiraConfig(
            projects=projects,
            overlap_seconds=args.overlap_seconds,
            token=args.jira_token or os.getenv("JIRA_TOKEN"),
            record_new_issues=args.record_new_issues,
            auto_analyze_new_issues=args.auto_analyze_new_issues,
            auto_analyze_projects=tuple(args.auto_analyze_project or ()),
            status_summary_enabled=args.status_summary,
            status_summary_projects=tuple(args.status_summary_project or ()),
            status_summary_target_statuses=tuple(args.status_summary_target_status or DEFAULT_JIRA_STATUS_SUMMARY_TARGET_STATUSES),
            bot_mention_keys=tuple(args.bot_mention_key or ()),
        ),
        args.jira_skill_dir or DEFAULT_JIRA_SKILL_DIR,
    )
    bootstrap = True
    if args.no_bootstrap:
        bootstrap = False
    if args.bootstrap:
        bootstrap = True
    result = monitor.poll_once(bootstrap_if_needed=bootstrap)
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_github_poll(args: argparse.Namespace) -> int:
    db = Database(resolve_db_path(args.db))
    db.init()
    repositories = tuple(args.repo or ())
    monitor = GitHubMonitor(
        db,
        GitHubConfig(
            repositories=repositories,
            auto_analyze=args.auto_analyze,
            overlap_seconds=args.overlap_seconds,
            page_size=args.page_size,
            token=args.github_token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"),
            api_url=args.api_url,
        ),
    )
    bootstrap = True
    if args.no_bootstrap:
        bootstrap = False
    if args.bootstrap:
        bootstrap = True
    result = monitor.poll_once(bootstrap_if_needed=bootstrap)
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_github_comment(args: argparse.Namespace) -> int:
    if args.body_file:
        body = args.body_file.read_text()
    elif args.body is not None:
        body = args.body
    else:
        body = " ".join(args.body_words).strip()
    if not body:
        print("github comment body is empty", file=sys.stderr)
        return 2
    db = Database(resolve_db_path(args.db))
    db.init()
    slot = db.get_slot(args.slot_id)
    if slot is None:
        print(f"slot not found: {args.slot_id}", file=sys.stderr)
        return 2
    if slot.source != "github_issue_analyze":
        print("github-comment only supports github_issue_analyze slots", file=sys.stderr)
        return 3
    if not slot.github_repo or not slot.github_issue_number:
        print("slot does not contain a GitHub repo and issue number", file=sys.stderr)
        return 3
    client = GitHubRestClient(
        token=args.github_token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"),
        api_url=args.api_url,
    )
    comment = client.create_issue_comment(slot.github_repo, slot.github_issue_number, body)
    comment_id = str(comment.get("id") or "")
    db.record_reply_request(args.slot_id, body)
    db.log_event(
        "github",
        "comment.created",
        f"posted GitHub issue comment for {slot.github_repo}#{slot.github_issue_number}",
        args.slot_id,
        repo_full_name=slot.github_repo,
        issue_number=slot.github_issue_number,
        comment_id=comment_id,
    )
    print(f"github comment recorded for {args.slot_id}: {comment_id}")
    return 0


def cmd_automation_report(args: argparse.Namespace) -> int:
    try:
        report = json.loads(args.json_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"failed to read automation report: {exc}", file=sys.stderr)
        return 2
    if not isinstance(report, dict):
        print("automation report must be a JSON object", file=sys.stderr)
        return 2
    db = Database(resolve_db_path(args.db))
    db.init()
    ok, message = db.record_jira_automation_report(args.slot_id, report)
    if not ok:
        print(message, file=sys.stderr)
        return 3
    db.log_event("jira", "automation.report_recorded", message, args.slot_id, report_kind=report.get("kind"))
    print(f"automation report recorded for {args.slot_id}")
    return 0


def cmd_admin_hash_password(args: argparse.Namespace) -> int:
    password = args.password
    if password is None:
        password = sys.stdin.readline().rstrip("\n")
    try:
        print(hash_admin_password(password))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def cmd_admin_web(args: argparse.Namespace) -> int:
    password_hash = args.password_hash or os.getenv("BREAKWATER_ADMIN_PASSWORD_HASH")
    if not password_hash:
        print("admin password hash is required; use admin-hash-password first", file=sys.stderr)
        return 2
    db = Database(resolve_db_path(args.db))
    db.init()
    server = BreakwaterAdminServer(
        args.host,
        args.port,
        db,
        password_hash=password_hash,
        session_secret=args.session_secret or os.getenv("BREAKWATER_ADMIN_SESSION_SECRET"),
        session_ttl_seconds=args.session_ttl_seconds,
        jira_base_url=args.jira_base_url or os.getenv("JIRA_URL"),
    )
    server.start()
    print(f"Breakwater admin page: http://{args.host}:{args.port}")
    try:
        signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    db = Database(resolve_db_path(args.db))
    db.init()
    started_at = utc_now()

    def provider() -> dict[str, object]:
        codex_thread_ids = db.recent_codex_thread_ids(limit=100)
        case_timelines = db.case_timelines(limit=25, slots_per_case=30)
        return {
            "started_at": started_at,
            "queue": {"concurrency": 0, "queued": 0, "running": 0, "queued_slot_ids": [], "active_slot_ids": []},
            "status_counts": db.count_slots_by_status(),
            "slots": [slot.__dict__ for slot in db.list_slots(limit=25)],
            "running_slots": [],
            "queued_slots": [],
            "open_jira_analyze_slots": [slot.__dict__ for slot in db.open_jira_analyze_slots(limit=200)],
            "lark_messages": [slot.__dict__ for slot in db.recent_lark_slots(limit=100)],
            "lark_message_events": [message.__dict__ for message in db.recent_lark_messages(limit=120)],
            "cases": [item["case"] for item in case_timelines],
            "case_timelines": case_timelines,
            "codex": {
                "app_server_url": DEFAULT_CODEX_WS,
                "ready_url": "",
                "ready": False,
                "owned": False,
                "pid": None,
                "model": None,
                "effort": None,
                "sandbox": None,
                "approval_policy": None,
                "workspace": str(PROJECT_ROOT),
                "start_server": False,
                "proxy_enabled": False,
                "proxy_env": {},
                "recent_threads": recent_codex_threads(
                    limit=12,
                    cwd=PROJECT_ROOT,
                    breakwater_only=True,
                    thread_ids=codex_thread_ids,
                ),
            },
            "jira": {
                "enabled": False,
                "record_new_issues": False,
                "auto_analyze_new_issues": False,
                "auto_analyze_projects": [],
                "status_summary_enabled": False,
                "status_summary_projects": [],
                "status_summary_target_statuses": [],
                "bot_mention_keys": [],
                "projects": [],
                "poll_interval_seconds": 0,
                "overlap_seconds": 0,
                "cursor": db.get_state("jira.last_success_at"),
                "counts": db.jira_counts(),
                "recent_issues": [issue.__dict__ for issue in db.recent_jira_issues(limit=10)],
                "recent_analyze_comments": [comment.__dict__ for comment in db.recent_jira_comments(limit=20)],
            },
            "github": {
                "enabled": False,
                "repositories": [],
                "auto_analyze": False,
                "poll_interval_seconds": 0,
                "overlap_seconds": 0,
                "cursors": {},
                "counts": db.github_counts(),
                "recent_issues": [issue.__dict__ for issue in db.recent_github_issues(limit=10)],
            },
            "events": db.recent_events(limit=30),
        }

    server = BreakwaterWebServer(args.host, args.port, provider)
    server.start()
    print(f"Breakwater status page: http://{args.host}:{args.port}")
    try:
        signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
