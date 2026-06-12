from __future__ import annotations

import html
import json
import logging
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


LOG = logging.getLogger(__name__)
StatusProvider = Callable[[], dict[str, Any]]


class BreakwaterWebServer:
    def __init__(self, host: str, port: int, status_provider: StatusProvider):
        self.host = host
        self.port = port
        self.status_provider = status_provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = self._handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, name="breakwater-web", daemon=True)
        self._thread.start()
        LOG.info("web status page started at http://%s:%s", self.host, self.port, extra={"component": "web"})

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=3)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        provider = self.status_provider

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/" or self.path.startswith("/?"):
                    self._send_html(render_status_page())
                    return
                if self.path == "/api/status":
                    self._send_json(provider())
                    return
                self.send_error(HTTPStatus.NOT_FOUND, "not found")

            def log_message(self, format: str, *args: Any) -> None:
                LOG.info("web: " + format, *args, extra={"component": "web"})

            def _send_json(self, payload: dict[str, Any]) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_html(self, markup: str) -> None:
                data = markup.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler


def render_status_page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Breakwater</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #18212f;
      --muted: #667085;
      --line: #d9e2ec;
      --bg: #f7fafc;
      --panel: #ffffff;
      --accent: #13a89e;
      --accent-2: #f6b94b;
      --bad: #d64545;
      --good: #138a54;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: radial-gradient(circle at top left, rgba(19, 168, 158, .16), transparent 32rem), var(--bg);
      color: var(--ink);
    }
    .startup-banner {
      width: 100%;
      padding: 10px clamp(18px, 5vw, 56px);
      background: #142235;
      color: #f8fafc;
      border-bottom: 1px solid rgba(255,255,255,.14);
      font-size: 13px;
      line-height: 1.4;
      overflow-wrap: anywhere;
    }
    header {
      padding: 28px clamp(18px, 5vw, 56px) 14px;
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
    }
    h1 { margin: 0; font-size: 32px; line-height: 1.05; letter-spacing: 0; }
    .sub { color: var(--muted); margin-top: 8px; font-size: 14px; }
    .pulse { display: inline-flex; align-items: center; gap: 8px; color: var(--good); font-size: 14px; }
    .pulse::before { content: ""; width: 9px; height: 9px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 0 5px rgba(19,168,158,.14); }
    main { padding: 10px clamp(18px, 5vw, 56px) 40px; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }
    .metric, .panel {
      background: rgba(255,255,255,.86);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 10px 28px rgba(31, 45, 61, .06);
    }
    .metric { padding: 16px; min-height: 92px; }
    .metric .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }
    .metric .value { margin-top: 8px; font-size: 30px; font-weight: 700; }
    .metric .hint { margin-top: 4px; color: var(--muted); font-size: 13px; }
    .layout { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(280px, .9fr); gap: 14px; }
    .codex-grid { display: grid; grid-template-columns: minmax(280px, .8fr) minmax(0, 1.2fr); gap: 14px; margin-bottom: 14px; }
    .focus-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-bottom: 14px; }
    .jira-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 14px; }
    .panel { overflow: hidden; display: flex; flex-direction: column; min-height: 0; }
    .panel h2 { margin: 0; padding: 15px 16px; font-size: 15px; border-bottom: 1px solid var(--line); }
    .scroll-window, .item-list, .events, .case-board {
      overflow: auto;
      max-height: 420px;
      scrollbar-gutter: stable;
    }
    .focus-grid .item-list, .codex-grid .item-list { max-height: 360px; }
    .layout .scroll-window, .layout .events, .layout .case-board { max-height: 650px; }
    .jira-grid .scroll-window { max-height: 420px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 12px 14px; border-bottom: 1px solid #edf2f7; vertical-align: top; }
    th { color: var(--muted); font-weight: 600; background: #fbfdff; }
    .slot { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .status { display: inline-flex; align-items: center; padding: 3px 8px; border-radius: 999px; background: #eef7f6; color: #08756f; font-weight: 600; font-size: 12px; }
    .status.failed { background: #fff1f1; color: var(--bad); }
    .status.queued { background: #fff8e8; color: #926200; }
    .status.running { background: #edf6ff; color: #1164a3; }
    .item-list { list-style: none; margin: 0; padding: 4px 14px 14px; }
    .item-list li { padding: 10px 0; border-bottom: 1px solid #edf2f7; }
    .item-title { display: flex; justify-content: space-between; gap: 10px; align-items: start; }
    .item-main { margin-top: 6px; font-size: 13px; line-height: 1.45; overflow-wrap: anywhere; }
    .tag { color: var(--muted); font-size: 12px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .muted { color: var(--muted); }
    .events { list-style: none; padding: 4px 14px 14px; margin: 0; }
	    .events li { padding: 10px 0; border-bottom: 1px solid #edf2f7; }
	    .event-top { display: flex; justify-content: space-between; gap: 12px; font-size: 12px; color: var(--muted); }
	    .event-msg { margin-top: 4px; font-size: 13px; }
	    .case-board { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; padding: 12px; }
	    .case-card { border: 1px solid #edf2f7; border-radius: 8px; background: #fff; padding: 12px; }
	    .case-head { display: flex; justify-content: space-between; gap: 12px; align-items: start; }
	    .case-title { font-weight: 700; overflow-wrap: anywhere; }
	    .case-meta { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 8px; color: var(--muted); font-size: 12px; }
	    .case-summary { margin-top: 8px; color: #344054; font-size: 13px; line-height: 1.45; overflow-wrap: anywhere; }
	    .case-timeline { margin-top: 10px; }
	    .case-timeline summary { cursor: pointer; color: var(--accent); font-size: 12px; font-weight: 700; }
	    .timeline-body { margin-top: 8px; max-height: 320px; overflow: auto; scrollbar-gutter: stable; }
	    .timeline { list-style: none; margin: 0; padding: 0; border-left: 2px solid #d7f1ee; }
	    .timeline li { padding: 0 0 11px 11px; border-bottom: 0; position: relative; }
	    .timeline li::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--accent); position: absolute; left: -5px; top: 4px; }
	    .timeline-top { display: flex; justify-content: space-between; gap: 10px; color: var(--muted); font-size: 12px; }
	    .timeline-text { margin-top: 3px; font-size: 13px; line-height: 1.42; overflow-wrap: anywhere; }
	    .thread-prompt { margin-top: 8px; }
    .thread-prompt summary { cursor: pointer; color: var(--accent); font-size: 12px; font-weight: 600; }
    .thread-prompt pre { margin: 8px 0 0; padding: 10px; max-height: 280px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; background: #f7fafc; border: 1px solid #edf2f7; border-radius: 6px; font-size: 12px; line-height: 1.45; scrollbar-gutter: stable; }
    @media (max-width: 900px) {
      header { align-items: start; flex-direction: column; }
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
	      .codex-grid { grid-template-columns: 1fr; }
	      .layout { grid-template-columns: 1fr; }
	      .focus-grid { grid-template-columns: 1fr; }
	      .jira-grid { grid-template-columns: 1fr; }
	      .case-board { grid-template-columns: 1fr; }
	    }
    @media (max-width: 520px) {
      .grid { grid-template-columns: 1fr; }
      th:nth-child(5), td:nth-child(5) { display: none; }
    }
  </style>
</head>
<body>
  <div class="startup-banner" id="startup-banner">Service started · loading...</div>
  <header>
    <div>
      <h1>Breakwater</h1>
      <div class="sub">Lark inbox, Codex queue, Jira-ready context</div>
    </div>
    <div class="pulse" id="heartbeat">connecting</div>
  </header>
  <main>
    <section class="grid" id="metrics"></section>
    <section class="codex-grid">
      <div class="panel">
        <h2>Codex App Server</h2>
        <ul class="item-list" id="codex-server" data-preserve-scroll data-state-key="codex-server"><li class="muted">Loading...</li></ul>
      </div>
      <div class="panel">
        <h2>Codex Conversations</h2>
        <ul class="item-list" id="codex-threads" data-preserve-scroll data-state-key="codex-threads"><li class="muted">Loading...</li></ul>
      </div>
    </section>
    <section class="focus-grid">
      <div class="panel">
        <h2>Active Tasks</h2>
        <ul class="item-list" id="active-tasks" data-preserve-scroll data-state-key="active-tasks"><li class="muted">Loading...</li></ul>
      </div>
      <div class="panel">
        <h2>Open Jira Analysis</h2>
        <ul class="item-list" id="open-jira-analyze" data-preserve-scroll data-state-key="open-jira-analyze"><li class="muted">Loading...</li></ul>
      </div>
      <div class="panel">
        <h2>Lark Messages</h2>
        <ul class="item-list" id="lark-messages" data-preserve-scroll data-state-key="lark-messages"><li class="muted">Loading...</li></ul>
      </div>
    </section>
    <section class="layout">
	      <div class="panel">
	        <h2>Cases</h2>
	        <div class="case-board" id="cases" data-preserve-scroll data-state-key="cases"><div class="muted">Loading...</div></div>
	      </div>
      <div class="panel">
        <h2>Recent Slots</h2>
        <div class="scroll-window" data-preserve-scroll data-state-key="slots-table">
          <table>
            <thead><tr><th>Slot</th><th>Session</th><th>Status</th><th>Message</th><th>Reply</th></tr></thead>
            <tbody id="slots"><tr><td colspan="5" class="muted">Loading...</td></tr></tbody>
          </table>
        </div>
      </div>
      <div class="panel">
        <h2>Event Stream</h2>
        <ul class="events" id="events" data-preserve-scroll data-state-key="events"></ul>
      </div>
    </section>
    <section class="jira-grid">
      <div class="panel">
        <h2>New GitHub Issues</h2>
        <div class="scroll-window" data-preserve-scroll data-state-key="github-issues-table">
          <table>
            <thead><tr><th>Issue</th><th>Status</th><th>Title</th><th>Created</th></tr></thead>
            <tbody id="github-issues"><tr><td colspan="4" class="muted">Loading...</td></tr></tbody>
          </table>
        </div>
      </div>
    </section>
    <section class="jira-grid">
      <div class="panel">
        <h2>New Jira Issues</h2>
        <div class="scroll-window" data-preserve-scroll data-state-key="jira-issues-table">
          <table>
            <thead><tr><th>Issue</th><th>Status</th><th>Summary</th><th>Created</th></tr></thead>
            <tbody id="jira-issues"><tr><td colspan="4" class="muted">Loading...</td></tr></tbody>
          </table>
        </div>
      </div>
      <div class="panel">
        <h2>Jira Analysis Comments</h2>
        <div class="scroll-window" data-preserve-scroll data-state-key="jira-comments-table">
          <table>
            <thead><tr><th>Issue</th><th>Status</th><th>Author</th><th>Comment</th></tr></thead>
            <tbody id="jira-comments"><tr><td colspan="4" class="muted">Loading...</td></tr></tbody>
          </table>
        </div>
      </div>
    </section>
  </main>
  <script>
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const badgeClass = (s) => s === 'failed' ? 'failed' : (s === 'queued' ? 'queued' : (s === 'running' ? 'running' : ''));
    const short = (s, n=150) => {
      const value = String(s ?? '');
      return value.length > n ? `${value.slice(0, n)}…` : value;
    };
    const timestamp = (value) => {
      if (!value) return 'unknown';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return `${date.toLocaleString()} · ${value}`;
    };
    const detailsState = new Map();
    const scrollState = new Map();
    const stateKey = (element) => element?.dataset?.stateKey || element?.id || '';
    const rememberUiState = () => {
      document.querySelectorAll('details[data-state-key]').forEach(details => {
        const key = stateKey(details);
        if (key) detailsState.set(key, details.open);
      });
      document.querySelectorAll('[data-preserve-scroll]').forEach(element => {
        const key = stateKey(element);
        if (key) scrollState.set(key, {top: element.scrollTop, left: element.scrollLeft});
      });
    };
    const restoreUiState = () => {
      document.querySelectorAll('details[data-state-key]').forEach(details => {
        const key = stateKey(details);
        if (key && detailsState.has(key)) details.open = Boolean(detailsState.get(key));
      });
      document.querySelectorAll('[data-preserve-scroll]').forEach(element => {
        const key = stateKey(element);
        const saved = key ? scrollState.get(key) : null;
        if (saved) {
          element.scrollTop = saved.top;
          element.scrollLeft = saved.left;
        }
      });
    };
    document.addEventListener('toggle', event => {
      const details = event.target;
      if (!(details instanceof HTMLDetailsElement)) return;
      const key = stateKey(details);
      if (key) detailsState.set(key, details.open);
    }, true);
    document.addEventListener('scroll', event => {
      const element = event.target;
      if (!(element instanceof HTMLElement) || !element.dataset.preserveScroll) return;
      const key = stateKey(element);
      if (key) scrollState.set(key, {top: element.scrollTop, left: element.scrollLeft});
    }, true);
    const sessionText = (slot) => slot.codex_thread_id || 'session pending';
    const slotText = (slot) => slot.incoming_text || (slot.source === 'jira_issue_auto_analyze' ? 'New issue auto-analysis; Jira content is read through jira-issue skill' : (slot.source === 'jira_status_summary' ? 'Jira status summary; Jira content is read through jira-issue skill' : ''));
    const slotItem = (slot) => `
      <li>
        <div class="item-title"><span class="tag">${esc(slot.slot_id)}</span><span class="status ${badgeClass(slot.status)}">${esc(slot.status)}</span></div>
        <div class="item-main">${esc(short(slotText(slot), 180))}</div>
        <div class="muted">${esc(slot.source)} · codex ${esc(slot.codex_status)} · reply ${esc(slot.reply_status)}</div>
        <div class="tag">${esc(sessionText(slot))}</div>
      </li>`;
    async function refresh() {
      rememberUiState();
      const res = await fetch('/api/status', {cache: 'no-store'});
      const data = await res.json();
      document.getElementById('heartbeat').textContent = `live · ${new Date().toLocaleTimeString()}`;
      const startupBanner = document.getElementById('startup-banner');
      startupBanner.textContent = `Service started · ${timestamp(data.started_at)}`;
      startupBanner.title = data.started_at || '';
      const q = data.queue;
      const counts = data.status_counts || {};
      const jira = data.jira || {};
      const jiraCounts = jira.counts || {};
      const github = data.github || {};
      const githubCounts = github.counts || {};
      const codex = data.codex || {};
      const larkListener = data.lark_listener || {};
      const larkHint = larkListener.last_event_at
        ? `last ${(larkListener.last_event_at || '').slice(11,19)} · restarts ${larkListener.restart_count || 0}`
        : (larkListener.last_error ? `error ${short(larkListener.last_error, 34)}` : `restarts ${larkListener.restart_count || 0}`);
      document.getElementById('metrics').innerHTML = [
        ['Running', q.running, `workers ${q.concurrency}`],
        ['Queued', q.queued, 'waiting for Codex'],
        ['Codex', codex.ready ? 'Ready' : 'Down', codex.model || 'model unset'],
        ['Lark Listener', larkListener.running ? 'Listening' : 'Down', larkHint],
        ['Jira Issues', jiraCounts.issues || 0, jira.auto_analyze_new_issues ? `auto ${(jira.auto_analyze_projects || []).join(', ')}` : (jira.record_new_issues ? ((jira.projects || []).join(', ') || 'watching') : 'disabled')],
        ['Jira Triggers', jiraCounts.analyze_comments || 0, `cursor ${String(jira.cursor || 'new').slice(11,19)}`],
        ['Jira Status', jira.status_summary_enabled ? 'On' : 'Off', jira.status_summary_enabled ? `${(jira.status_summary_projects || []).join(', ')} -> ${(jira.status_summary_target_statuses || []).join(', ')}` : 'summary disabled'],
        ['GitHub Issues', githubCounts.issues || 0, github.enabled ? ((github.repositories || []).join(', ') || 'watching') : 'disabled'],
        ['Replied', counts.replied || 0, 'sent or captured'],
        ['Failed', counts.failed || 0, 'needs attention'],
      ].map(([label, value, hint]) => `<div class="metric"><div class="label">${label}</div><div class="value">${value}</div><div class="hint">${hint}</div></div>`).join('');
      document.getElementById('codex-server').innerHTML = `
        <li>
          <div class="item-title"><span class="tag">${esc(codex.app_server_url || '')}</span><span class="status ${codex.ready ? '' : 'failed'}">${codex.ready ? 'ready' : 'not ready'}</span></div>
          <div class="item-main">${esc(codex.workspace || '')}</div>
          <div class="muted">model ${esc(codex.model || '')} · effort ${esc(codex.effort || '')} · sandbox ${esc(codex.sandbox || '')}</div>
        </li>
        <li>
          <div class="item-title"><span class="tag">proxy</span><span class="status ${codex.proxy_enabled ? '' : 'failed'}">${codex.proxy_enabled ? 'enabled' : 'disabled'}</span></div>
          <div class="item-main">${esc((codex.proxy_env || {}).https_proxy || '')}</div>
          <div class="muted">pid ${esc(codex.pid || 'external')} · owned ${esc(codex.owned)}</div>
        </li>`;
      document.getElementById('codex-threads').innerHTML = (codex.recent_threads || []).map(thread => `
        <li>
          <div class="item-title"><span class="tag">${esc(thread.id)}</span><span class="status">${esc(thread.model || 'model')}</span></div>
          <div class="item-main">${esc(short(thread.preview || thread.title, 220))}</div>
          <div class="muted">${esc(thread.cwd || '')} · ${esc(thread.source || '')} · ${esc(thread.updated_at || '')}</div>
          <details class="thread-prompt" data-state-key="thread:${esc(thread.id)}:prompt">
            <summary>First prompt</summary>
            <pre data-preserve-scroll data-state-key="thread:${esc(thread.id)}:prompt-scroll">${esc(thread.first_user_message || 'No first prompt recorded')}</pre>
          </details>
        </li>
      `).join('') || '<li class="muted">No Codex conversations recorded</li>';
      const runningSlots = data.running_slots || [];
      const queuedSlots = data.queued_slots || [];
      document.getElementById('active-tasks').innerHTML = [
        ...runningSlots.map(slotItem),
        ...queuedSlots.map(slot => slotItem({...slot, status: slot.status === 'running' ? slot.status : 'queued'})),
      ].join('') || '<li class="muted">No running or queued tasks</li>';
      document.getElementById('open-jira-analyze').innerHTML = (data.open_jira_analyze_slots || []).map(slot => `
        <li>
          <div class="item-title"><span class="tag">${esc(slot.jira_issue_key || slot.slot_id)}</span><span class="status ${badgeClass(slot.status)}">${esc(slot.status)}</span></div>
          <div class="item-main">${esc(short(slotText(slot), 190))}</div>
          <div class="muted">slot ${esc(slot.slot_id)} · comment ${esc(slot.jira_comment_id || '')}</div>
          <div class="tag">${esc(sessionText(slot))}</div>
        </li>
      `).join('') || '<li class="muted">No open Jira analysis requests</li>';
	      const larkMessages = data.lark_message_events || [];
	      document.getElementById('lark-messages').innerHTML = larkMessages.map(message => `
	        <li>
	          <div class="item-title"><span class="tag">${esc(message.chat_name || message.chat_type || 'lark')}</span><span class="status ${message.mentioned_bot ? '' : 'queued'}">${message.mentioned_bot ? 'mentioned' : 'context'}</span></div>
	          <div class="item-main">${esc(short(message.content, 190))}</div>
	          <div class="muted">sender ${esc(message.sender_id || '')} · handled ${esc(message.handled_slot_id || 'no')}</div>
	          <div class="tag">${esc((message.observed_at || '').slice(0, 19))}</div>
	        </li>
	      `).join('') || (data.lark_messages || []).map(slotItem).join('') || '<li class="muted">No Lark messages recorded</li>';
	      document.getElementById('cases').innerHTML = (data.case_timelines || []).map(t => {
	        const c = t.case || {};
	        const slots = t.slots || [];
	        const aliases = t.aliases || [];
	        const latest = slots[slots.length - 1] || {};
	        const hiddenCount = Math.max(0, Number(t.slot_count || slots.length) - slots.length);
	        return `
	        <article class="case-card">
	          <div class="case-head">
	            <div>
	              <div class="case-title">${esc(c.scope_key || c.title || c.case_id)}</div>
	              <div class="case-meta"><span>${esc(c.case_id)}</span><span>${esc(c.latest_codex_thread_id || 'session pending')}</span></div>
	            </div>
	            <span class="status ${badgeClass(c.status)}">${esc(c.status)}</span>
	          </div>
	          <div class="case-meta"><span>latest ${esc(c.latest_slot_id || '-')}</span><span>${esc(t.slot_count || slots.length)} turns</span><span>${esc(latest.source || 'no activity')}</span><span>lark ${esc(c.bound_lark_chat_name || c.bound_lark_chat_id || 'unbound')}</span></div>
	          <div class="case-summary">${esc(short(c.summary || 'No summary yet', 260))}</div>
	          <details class="case-timeline" data-state-key="case:${esc(c.case_id)}:timeline">
	            <summary>Case timeline</summary>
	            <div class="muted">aliases ${esc(aliases.map(a => `${a.alias_type}:${a.alias_key}`).join(' · '))}</div>
	            <div class="timeline-body" data-preserve-scroll data-state-key="case:${esc(c.case_id)}:timeline-scroll">
              <ul class="timeline">
                ${slots.map(slot => `<li><div class="timeline-top"><span>${esc(slot.source)} · ${esc(slot.slot_id)}</span><span>${esc(slot.status)} · ${esc(slot.created_at || '').slice(5,16)}</span></div><div class="timeline-text">${esc(short(slotText(slot), 180))}</div><div class="tag">${esc(slot.codex_thread_id || '')}</div></li>`).join('')}
              </ul>
            </div>
	            ${hiddenCount ? `<div class="muted">Showing latest ${slots.length} of ${esc(t.slot_count)} turns</div>` : ''}
	          </details>
	        </article>
	      `}).join('') || '<div class="muted">No cases yet</div>';
      document.getElementById('slots').innerHTML = (data.slots || []).map(slot => `
        <tr>
          <td class="slot">${esc(slot.slot_id)}</td>
          <td class="slot">${esc(sessionText(slot))}</td>
          <td><span class="status ${badgeClass(slot.status)}">${esc(slot.status)}</span><div class="muted">codex ${esc(slot.codex_status)}</div></td>
          <td>${esc(slotText(slot)).slice(0, 160)}</td>
          <td>${esc(slot.reply_text || slot.reply_status)}</td>
        </tr>`).join('') || '<tr><td colspan="5" class="muted">No slots yet</td></tr>';
      document.getElementById('events').innerHTML = (data.events || []).map(ev => `
        <li><div class="event-top"><span>${esc(ev.component)} · ${esc(ev.event_type)}</span><span>${esc(ev.created_at).slice(11,19)}</span></div><div class="event-msg">${esc(ev.message)}</div></li>
      `).join('') || '<li class="muted">No events yet</li>';
      document.getElementById('jira-issues').innerHTML = !(jira.record_new_issues || jira.auto_analyze_new_issues || jira.status_summary_enabled)
        ? '<tr><td colspan="4" class="muted">New issue monitoring is disabled</td></tr>'
        : (jira.recent_issues || []).map(issue => `
        <tr>
          <td class="slot">${esc(issue.issue_key)}</td>
          <td><span class="status ${badgeClass(issue.status)}">${esc(issue.status || (issue.slot_id ? 'queued' : 'observed'))}</span></td>
          <td>${esc(issue.summary).slice(0, 140)}<div class="muted">Jira status ${esc(issue.jira_status_name || '-')}</div></td>
          <td class="muted">${esc(issue.jira_created_at).slice(0, 16)}</td>
        </tr>
      `).join('') || '<tr><td colspan="4" class="muted">No new Jira issues recorded</td></tr>';
      document.getElementById('github-issues').innerHTML = !github.enabled
        ? '<tr><td colspan="4" class="muted">GitHub issue monitoring is disabled</td></tr>'
        : (github.recent_issues || []).map(issue => `
        <tr>
          <td class="slot">${esc(issue.repo_full_name)}#${esc(issue.issue_number)}</td>
          <td><span class="status ${badgeClass(issue.status)}">${esc(issue.status || 'observed')}</span></td>
          <td>${esc(short(issue.title, 140))}</td>
          <td class="muted">${esc(issue.github_created_at).slice(0, 16)}</td>
        </tr>
      `).join('') || '<tr><td colspan="4" class="muted">No new GitHub issues recorded</td></tr>';
      const analyzeComments = (jira.recent_analyze_comments || []).slice(0, 20);
      document.getElementById('jira-comments').innerHTML = analyzeComments.map(comment => `
        <tr>
          <td class="slot">${esc(comment.issue_key)}</td>
          <td><span class="status ${badgeClass(comment.status)}">${esc(comment.status || 'observed')}</span></td>
          <td>${esc(comment.author)}</td>
          <td>${esc(comment.body).slice(0, 150)}</td>
        </tr>
      `).join('') || '<tr><td colspan="4" class="muted">No Jira analysis comments recorded</td></tr>';
      restoreUiState();
    }
    refresh().catch(() => { document.getElementById('heartbeat').textContent = 'offline'; });
    setInterval(() => refresh().catch(() => { document.getElementById('heartbeat').textContent = 'offline'; }), 2000);
  </script>
</body>
</html>"""


def escape_text(value: str) -> str:
    return html.escape(value, quote=True)
