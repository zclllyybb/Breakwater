from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import hmac
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .db import Database, JiraAutomationRecord
from .sources import JIRA_ISSUE_AUTO_ANALYZE_SOURCE, JIRA_STATUS_SUMMARY_SOURCE


SESSION_COOKIE = "bw_admin_session"
PASSWORD_HASH_PREFIX = "pbkdf2_sha256"
ADMIN_VIEW_ALL = "all"
ADMIN_VIEW_WEEK = "week"
ADMIN_VIEW_CURRENT_WEEK = "current_week"
ADMIN_VIEW_CURRENT_MONTH = "current_month"
ADMIN_VIEW_LABELS = {
    ADMIN_VIEW_ALL: "全量",
    ADMIN_VIEW_WEEK: "近 7 天",
    ADMIN_VIEW_CURRENT_WEEK: "当前周",
    ADMIN_VIEW_CURRENT_MONTH: "当前月",
}


class BreakwaterAdminServer:
    def __init__(
        self,
        host: str,
        port: int,
        db: Database,
        *,
        password_hash: str,
        session_secret: str | None = None,
        session_ttl_seconds: int = 86400,
        jira_base_url: str | None = None,
    ):
        self.host = host
        self.port = port
        self.db = db
        self.password_hash = password_hash
        self.session_secret = (session_secret or password_hash).encode("utf-8")
        self.session_ttl_seconds = session_ttl_seconds
        self.jira_base_url = (jira_base_url or os.getenv("JIRA_URL") or "").rstrip("/")
        self._httpd: ThreadingHTTPServer | None = None

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                server._handle(self)

            def do_POST(self) -> None:  # noqa: N802
                server._handle(self)

            def log_message(self, fmt: str, *args: object) -> None:
                return

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        import threading

        thread = threading.Thread(target=self._httpd.serve_forever, name="breakwater-admin-web", daemon=True)
        thread.start()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(request.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/login":
            if request.command == "POST":
                self._handle_login(request)
            else:
                self._send_html(request, HTTPStatus.OK, self._login_page())
            return
        if path == "/logout" and request.command == "POST":
            self._send(request, HTTPStatus.SEE_OTHER, b"", {"Set-Cookie": _expired_cookie(), "Location": "/login"})
            return

        if not self._authenticated(request):
            if path.startswith("/api/"):
                self._send_json(request, HTTPStatus.UNAUTHORIZED, {"error": "authentication required"})
            else:
                self._send(request, HTTPStatus.SEE_OTHER, b"", {"Location": "/login"})
            return

        view = _admin_view(query)
        if path in {"/", "/auto-analysis", "/status-summaries"}:
            self._send_html(
                request,
                HTTPStatus.OK,
                self._dashboard_page(view),
            )
            return
        if path == "/api/auto-analysis":
            self._send_records_json(request, JIRA_ISSUE_AUTO_ANALYZE_SOURCE, view)
            return
        if path == "/api/status-summaries":
            self._send_records_json(request, JIRA_STATUS_SUMMARY_SOURCE, view)
            return
        if path == "/auto-analysis.csv":
            self._send_csv(request, JIRA_ISSUE_AUTO_ANALYZE_SOURCE, "auto-analysis", view)
            return
        if path == "/status-summaries.csv":
            self._send_csv(request, JIRA_STATUS_SUMMARY_SOURCE, "status-summaries", view)
            return
        self._send_json(request, HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _handle_login(self, request: BaseHTTPRequestHandler) -> None:
        length = int(request.headers.get("Content-Length") or "0")
        body = request.rfile.read(length).decode("utf-8", errors="replace")
        password = parse_qs(body).get("password", [""])[0]
        if not verify_admin_password(password, self.password_hash):
            self._send_html(request, HTTPStatus.UNAUTHORIZED, self._login_page(error="密码不正确"))
            return
        self._send(
            request,
            HTTPStatus.SEE_OTHER,
            b"",
            {
                "Set-Cookie": _session_cookie(self._make_session()),
                "Location": "/auto-analysis",
            },
        )

    def _authenticated(self, request: BaseHTTPRequestHandler) -> bool:
        cookie_header = request.headers.get("Cookie") or ""
        cookie = SimpleCookie(cookie_header)
        morsel = cookie.get(SESSION_COOKIE)
        return bool(morsel and self._verify_session(morsel.value))

    def _make_session(self) -> str:
        payload = json.dumps({"exp": int(time.time()) + self.session_ttl_seconds}, separators=(",", ":")).encode("utf-8")
        payload_b64 = _b64(payload)
        signature = hmac.new(self.session_secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
        return f"{payload_b64}.{_b64(signature)}"

    def _verify_session(self, value: str) -> bool:
        try:
            payload_b64, signature_b64 = value.split(".", 1)
            expected = _b64(hmac.new(self.session_secret, payload_b64.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(signature_b64, expected):
                return False
            payload = json.loads(base64.urlsafe_b64decode(_pad_b64(payload_b64)))
        except (ValueError, json.JSONDecodeError, OSError, binascii.Error):
            return False
        return int(payload.get("exp") or 0) >= int(time.time())

    def _send_records_json(self, request: BaseHTTPRequestHandler, source: str, view: str) -> None:
        records = [
            record_to_dict(record)
            for record in self._list_records(source, view)
        ]
        self._send_json(request, HTTPStatus.OK, {"records": records})

    def _send_csv(self, request: BaseHTTPRequestHandler, source: str, filename_part: str, view: str) -> None:
        rows = self._csv_rows(source, view)
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "issue_key",
                "jira_url",
                "slot_id",
                "subject_role",
                "subject_user",
                "report_status",
                "reason",
                "details",
                "created_at",
                "updated_at",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
        body = output.getvalue().encode("utf-8")
        self._send(
            request,
            HTTPStatus.OK,
            body,
            {
                "Content-Type": "text/csv; charset=utf-8",
                "Content-Disposition": f'attachment; filename="breakwater-{filename_part}-{view}.csv"',
            },
        )

    def _dashboard_page(self, view: str) -> str:
        auto_records = self._list_records(JIRA_ISSUE_AUTO_ANALYZE_SOURCE, view)
        status_records = self._list_records(JIRA_STATUS_SUMMARY_SOURCE, view)
        view_controls = self._view_controls(view)
        auto_section = self._records_section(
            title="提 Jira 时信息不全",
            description="自动初始分析判定材料不全的记录。负责人为 reporter。",
            records=auto_records,
            subject_label="Reporter",
            download_href=f"/auto-analysis.csv?view={escape(view)}",
        )
        status_section = self._records_section(
            title="关闭 Jira 时结论不全",
            description="状态收口总结判定缺少明确结论和解决办法的记录。负责人为 assignee。",
            records=status_records,
            subject_label="Assignee",
            download_href=f"/status-summaries.csv?view={escape(view)}",
        )
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Breakwater Admin</title>
  <style>
    body {{ margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f8fb; color: #162033; }}
    header {{ display: flex; justify-content: space-between; align-items: center; padding: 18px 28px; background: #10243e; color: white; }}
    main {{ padding: 24px 28px; display: grid; gap: 22px; }}
    a {{ color: #0c6fbd; text-decoration: none; }}
    .panel {{ background: white; border: 1px solid #dde5ef; border-radius: 8px; overflow: hidden; }}
    .panel-head {{ display: flex; justify-content: space-between; gap: 16px; padding: 16px 18px; border-bottom: 1px solid #edf1f7; }}
    .panel-head h2 {{ margin: 0; font-size: 18px; }}
    .panel-head p {{ margin: 4px 0 0; color: #667085; }}
    .panel-actions {{ display: flex; align-items: flex-start; gap: 10px; }}
    .count {{ align-self: start; border-radius: 999px; background: #eef6ff; color: #075985; padding: 3px 9px; }}
    .table-wrap {{ overflow: auto; max-height: 420px; }}
    table {{ width: 100%; min-width: 980px; border-collapse: collapse; }}
    th, td {{ padding: 11px 12px; border-bottom: 1px solid #edf1f7; text-align: left; vertical-align: top; }}
    th {{ position: sticky; top: 0; background: #f0f5fa; color: #506070; font-size: 12px; text-transform: uppercase; }}
    .status {{ display: inline-block; padding: 2px 8px; border-radius: 999px; background: #edf6ff; color: #075985; }}
    .bad {{ background: #fff1f2; color: #be123c; }}
    .good {{ background: #ecfdf5; color: #047857; }}
    .muted {{ color: #6b7280; }}
    .reason {{ max-width: 420px; white-space: pre-wrap; }}
    .slot {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; }}
    .actions {{ display: flex; align-items: center; gap: 12px; }}
    .tabs {{ display: flex; align-items: center; gap: 8px; }}
    .tab {{ display: inline-block; border: 1px solid rgba(255,255,255,.32); border-radius: 6px; padding: 7px 10px; color: #dbeafe; }}
    .tab.active {{ background: white; color: #10243e; border-color: white; }}
    .download {{ display: inline-block; border-radius: 6px; padding: 7px 10px; background: #0c6fbd; color: white; }}
    form {{ margin: 0; }}
    button {{ background: #e5edf6; border: 0; border-radius: 6px; padding: 8px 12px; cursor: pointer; }}
  </style>
</head>
<body>
  <header>
    <div><strong>Breakwater Admin</strong> · Jira 自动化质量记录 · {escape(ADMIN_VIEW_LABELS[view])}</div>
    <div class="actions">{view_controls}<form method="post" action="/logout"><button>退出</button></form></div>
  </header>
  <main>
    {auto_section}
    {status_section}
  </main>
</body>
</html>"""

    def _list_records(self, source: str, view: str) -> list[JiraAutomationRecord]:
        return self.db.list_jira_automation_records(
            source=source,
            limit=None,
            attention_only=True,
            created_after=_created_after_for_view(view),
        )

    def _csv_rows(self, source: str, view: str) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for record in self._list_records(source, view):
            rows.append(
                {
                    "issue_key": record.issue_key,
                    "jira_url": self._jira_issue_url(record.issue_key),
                    "slot_id": record.slot_id,
                    "subject_role": record.subject_role or "",
                    "subject_user": record.subject_user or "",
                    "report_status": record.report_status,
                    "reason": record.reason or record.error or "",
                    "details": json.dumps(record.details, ensure_ascii=False),
                    "created_at": record.created_at,
                    "updated_at": record.updated_at,
                }
            )
        return rows

    def _view_controls(self, view: str) -> str:
        links = []
        for value, label in ADMIN_VIEW_LABELS.items():
            link_class = "tab active" if view == value else "tab"
            links.append(
                f"<a class='{link_class}' href='/auto-analysis?view={escape(value)}'>{escape(label)}</a>"
            )
        return (
            "<nav class='tabs'>"
            + "".join(links)
            + "</nav>"
        )

    def _records_section(
        self,
        *,
        title: str,
        description: str,
        records: list[JiraAutomationRecord],
        subject_label: str,
        download_href: str,
    ) -> str:
        rows = "\n".join(
            self._record_row(record)
            for record in records
        )
        if not rows:
            rows = f"<tr><td colspan='6' class='muted'>暂无记录</td></tr>"
        return f"""<section class="panel">
  <div class="panel-head">
    <div>
      <h2>{escape(title)}</h2>
      <p>{escape(description)}</p>
    </div>
    <div class="panel-actions">
      <a class="download" href="{download_href}">下载 CSV</a>
      <span class="count">{len(records)}</span>
    </div>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Jira</th><th>Slot</th><th>{escape(subject_label)}</th><th>报告</th><th>原因</th><th>更新时间</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</section>"""

    def _record_row(self, record: JiraAutomationRecord) -> str:
        subject = escape(record.subject_user or "-")
        if record.subject_role:
            subject = f"{subject}<br><span class='muted'>{escape(record.subject_role)}</span>"
        jira_url = self._jira_issue_url(record.issue_key)
        jira_cell = (
            f"<a href='{escape(jira_url)}' target='_blank' rel='noreferrer'>{escape(record.issue_key)}</a>"
            if jira_url
            else escape(record.issue_key)
        )
        return (
            "<tr>"
            f"<td>{jira_cell}</td>"
            f"<td class='slot'>{escape(record.slot_id)}<br><span class='muted'>{escape(record.source)}</span></td>"
            f"<td>{subject}</td>"
            f"<td><span class='status'>{escape(record.report_status)}</span></td>"
            f"<td class='reason'>{escape(record.reason or record.error or '-')}</td>"
            f"<td>{escape(record.updated_at)}</td>"
            "</tr>"
        )

    def _jira_issue_url(self, issue_key: str) -> str:
        if not self.jira_base_url:
            return ""
        return f"{self.jira_base_url}/browse/{issue_key}"

    def _login_page(self, error: str | None = None) -> str:
        error_html = f"<p class='error'>{escape(error)}</p>" if error else ""
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Breakwater Admin Login</title>
  <style>
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #10243e; color: #172033; }}
    form {{ width: min(360px, calc(100vw - 40px)); background: white; border-radius: 8px; padding: 24px; box-shadow: 0 20px 60px rgba(0,0,0,.22); }}
    h1 {{ margin: 0 0 16px; font-size: 20px; }}
    label {{ display: block; margin-bottom: 8px; color: #516070; }}
    input {{ box-sizing: border-box; width: 100%; padding: 11px 12px; border: 1px solid #cad4e0; border-radius: 6px; }}
    button {{ width: 100%; margin-top: 16px; padding: 11px 12px; border: 0; border-radius: 6px; background: #0c6fbd; color: white; cursor: pointer; }}
    .error {{ color: #be123c; }}
  </style>
</head>
<body>
  <form method="post" action="/login">
    <h1>Breakwater Admin</h1>
    {error_html}
    <label for="password">密码</label>
    <input id="password" name="password" type="password" autocomplete="current-password" autofocus>
    <button type="submit">登录</button>
  </form>
</body>
</html>"""

    def _send_json(self, request: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self._send(request, status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), {"Content-Type": "application/json; charset=utf-8"})

    def _send_html(self, request: BaseHTTPRequestHandler, status: HTTPStatus, body: str) -> None:
        self._send(request, status, body.encode("utf-8"), {"Content-Type": "text/html; charset=utf-8"})

    def _send(self, request: BaseHTTPRequestHandler, status: HTTPStatus, body: bytes, headers: dict[str, str] | None = None) -> None:
        request.send_response(int(status))
        for key, value in {
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Content-Length": str(len(body)),
            **(headers or {}),
        }.items():
            request.send_header(key, value)
        request.end_headers()
        if body:
            request.wfile.write(body)


def record_to_dict(record: JiraAutomationRecord) -> dict[str, Any]:
    return {
        "slot_id": record.slot_id,
        "source": record.source,
        "issue_key": record.issue_key,
        "case_id": record.case_id,
        "trigger_key": record.trigger_key,
        "subject_user": record.subject_user,
        "subject_role": record.subject_role,
        "jira_comment_id": record.jira_comment_id,
        "comment_verified_at": record.comment_verified_at,
        "report_status": record.report_status,
        "missing_required_material": record.missing_required_material,
        "has_clear_resolution": record.has_clear_resolution,
        "reason": record.reason,
        "details": record.details,
        "raw_report": record.raw_report,
        "error": record.error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def hash_admin_password(password: str, *, salt: bytes | None = None, iterations: int = 200_000) -> str:
    if not password:
        raise ValueError("password must not be empty")
    actual_salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), actual_salt, iterations)
    return f"{PASSWORD_HASH_PREFIX}${iterations}${_b64(actual_salt)}${_b64(digest)}"


def verify_admin_password(password: str, encoded: str) -> bool:
    try:
        prefix, iterations_raw, salt_b64, digest_b64 = encoded.split("$", 3)
        if prefix != PASSWORD_HASH_PREFIX:
            return False
        iterations = int(iterations_raw)
        salt = base64.urlsafe_b64decode(_pad_b64(salt_b64))
        expected = base64.urlsafe_b64decode(_pad_b64(digest_b64))
    except (ValueError, OSError, binascii.Error):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def escape(value: object) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _admin_view(query: dict[str, list[str]]) -> str:
    value = (query.get("view") or [ADMIN_VIEW_ALL])[0]
    return value if value in ADMIN_VIEW_LABELS else ADMIN_VIEW_ALL


def _created_after_for_view(view: str, now: datetime | None = None) -> str | None:
    local_now = _local_now(now)
    if view == ADMIN_VIEW_ALL:
        return None
    if view == ADMIN_VIEW_WEEK:
        return (local_now.astimezone(timezone.utc) - timedelta(days=7)).isoformat()
    if view == ADMIN_VIEW_CURRENT_WEEK:
        start_local = _start_of_local_day(local_now) - timedelta(days=local_now.weekday())
        return start_local.astimezone(timezone.utc).isoformat()
    if view == ADMIN_VIEW_CURRENT_MONTH:
        start_local = _start_of_local_day(local_now).replace(day=1)
        return start_local.astimezone(timezone.utc).isoformat()
    return None


def _local_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now().astimezone()
    if now.tzinfo is None:
        return now.astimezone()
    return now


def _start_of_local_day(value: datetime) -> datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _session_cookie(value: str) -> str:
    return f"{SESSION_COOKIE}={value}; HttpOnly; SameSite=Strict; Path=/"


def _expired_cookie() -> str:
    return f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _pad_b64(value: str) -> bytes:
    return (value + "=" * (-len(value) % 4)).encode("ascii")
