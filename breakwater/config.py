from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / ".breakwater" / "breakwater.db"
DEFAULT_CODEX_WS = "ws://127.0.0.1:17345"
DEFAULT_JIRA_SKILL_DIR = PROJECT_ROOT / "skills" / "jira-issue"
DEFAULT_DEVELOP_SKILL_DIR = PROJECT_ROOT / "skills" / "breakwater-develop"
DEFAULT_LARK_CLI_CONFIG = Path.home() / ".lark-cli" / "config.json"
DEFAULT_LARK_CLI_SECRET_DIR = Path.home() / ".local" / "share" / "lark-cli"
DEFAULT_PROXY_HTTP = "http://127.0.0.1:7890"
DEFAULT_PROXY_ALL = "socks5h://127.0.0.1:7890"
DEFAULT_JIRA_STATUS_SUMMARY_TARGET_STATUSES = ("Done", "Backlog")


@dataclass(frozen=True)
class CodexConfig:
    ws_url: str = DEFAULT_CODEX_WS
    model: str | None = None
    effort: str | None = None
    sandbox: str = "workspace-write"
    approval_policy: str = "never"
    max_reply_retries: int = 2
    turn_timeout_seconds: int = 180
    start_server: bool = True


@dataclass(frozen=True)
class LarkConfig:
    app_id: str | None = None
    app_secret: str | None = None
    cli_config: Path = DEFAULT_LARK_CLI_CONFIG
    chat_id: str | None = None


@dataclass(frozen=True)
class JiraConfig:
    enabled: bool = False
    record_new_issues: bool = False
    auto_analyze_new_issues: bool = False
    auto_analyze_projects: tuple[str, ...] = ()
    status_summary_enabled: bool = False
    status_summary_projects: tuple[str, ...] = ()
    status_summary_target_statuses: tuple[str, ...] = DEFAULT_JIRA_STATUS_SUMMARY_TARGET_STATUSES
    bot_mention_keys: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    poll_interval_seconds: float = 60.0
    overlap_seconds: int = 180
    page_size: int = 100
    token: str | None = None
    analysis_marker_prefix: str = "Breakwater-Analysis-Slot"


@dataclass(frozen=True)
class GitHubConfig:
    enabled: bool = False
    repositories: tuple[str, ...] = ()
    auto_analyze: bool = False
    poll_interval_seconds: float = 300.0
    overlap_seconds: int = 180
    page_size: int = 100
    token: str | None = None
    api_url: str = "https://api.github.com"
    analysis_marker_prefix: str = "Breakwater-GitHub-Analysis-Slot"


@dataclass(frozen=True)
class DevelopConfig:
    enabled: bool = True
    skill_dir: Path = DEFAULT_DEVELOP_SKILL_DIR


@dataclass(frozen=True)
class ProxyConfig:
    enabled: bool = True
    http: str = DEFAULT_PROXY_HTTP
    https: str = DEFAULT_PROXY_HTTP
    all: str = DEFAULT_PROXY_ALL
    no_proxy: str = "127.0.0.1,localhost"


@dataclass(frozen=True)
class AdminWebConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8766
    password_hash: str | None = None
    session_secret: str | None = None
    session_ttl_seconds: int = 86400
    jira_base_url: str | None = None


@dataclass(frozen=True)
class AppConfig:
    db_path: Path = DEFAULT_DB_PATH
    project_root: Path = PROJECT_ROOT
    codex_workspace: Path = PROJECT_ROOT
    codex: CodexConfig = CodexConfig()
    lark: LarkConfig = LarkConfig()
    jira: JiraConfig = JiraConfig()
    github: GitHubConfig = GitHubConfig()
    develop: DevelopConfig = DevelopConfig()
    proxy: ProxyConfig = ProxyConfig()
    admin_web: AdminWebConfig = AdminWebConfig()
    jira_skill_dir: Path = DEFAULT_JIRA_SKILL_DIR
    prompt_variables: dict[str, str] | None = None
    reply_poll_interval: float = 1.0
    codex_concurrency: int = 5
    web_enabled: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = 8765


def db_path_from_env(default: Path = DEFAULT_DB_PATH) -> Path:
    return Path(os.getenv("BREAKWATER_DB", str(default))).expanduser().resolve()
