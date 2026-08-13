#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CC 手机监视/操作终端 — 手机实时看 Claude Code 进度，并可向专用手机会话发消息

读取 ~/.claude/projects/ 下的会话记录（JSONL transcript），提供移动端网页，
手机浏览器打开 http://<电脑IP>:8765 即可查看/操作。

只读监视:
    python cc_monitor.py
控制模式（可发消息，强制要求 --pin）:
    python cc_monitor.py --send --pin 1234
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RESULT_TRUNC = 2000      # 工具结果最大显示长度
SUMMARY_TRUNC = 300      # 工具入参摘要最大显示长度
RUNNING_WINDOW = 60      # 最后活动 N 秒内视为"运行中"
POLL_INTERVAL = 1.0      # 文件扫描间隔（秒）
DEFAULT_PHONE_SESSION = "1c0ffee0-0000-4000-8000-000000000001"  # 专用手机会话 UUID


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # 仅本地路由探测，不发送数据包
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def resolve_claude():
    """找到 claude 可执行文件；npm 安装的是 .CMD shim，需解析出真实 exe"""
    exe = shutil.which("claude")
    if not exe:
        return None
    exe = os.path.abspath(exe)
    if exe.lower().endswith((".cmd", ".bat")):
        for c in (
            os.path.join(os.path.dirname(exe), "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe"),
            os.path.join(os.path.dirname(exe), "..", "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe"),
        ):
            if os.path.isfile(c):
                return c
        return None
    return exe


def truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n] + "…(已截断)"


class Session:
    """单个会话文件的状态：字节游标 + 解析出的消息列表"""

    def __init__(self, sid, path, project):
        self.sid = sid
        self.path = path
        self.project = project
        self.cursor = 0          # 已从磁盘读取的字节数
        self.partial = b""       # 未完成的行（文件正在写入时可能半行）
        self.messages = []       # 展示用消息
        self.title = None
        self.cwd = None
        self.git_branch = None
        self.last_ts = None      # epoch 秒
        self.last_iso = None     # ISO 字符串

    def poll(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.cursor:          # 文件被重写/截断
            self.cursor = 0
            self.partial = b""
        if size == self.cursor:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self.cursor)
                data = f.read()
        except OSError:
            return
        self.cursor += len(data)
        self.partial += data
        lines = self.partial.split(b"\n")
        self.partial = lines[-1]
        for ln in lines[:-1]:
            if ln.strip():
                self._parse(ln)

    def _parse(self, raw):
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            return
        t = d.get("type")
        if d.get("timestamp"):
            try:
                dt = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00"))
                self.last_ts = dt.timestamp()
                self.last_iso = d["timestamp"]
            except ValueError:
                pass
        if d.get("cwd"):
            self.cwd = d["cwd"]
        if d.get("gitBranch"):
            self.git_branch = d["gitBranch"]
        if t == "ai-title" and d.get("aiTitle"):
            self.title = d["aiTitle"]
        elif t in ("user", "assistant", "attachment"):
            self.messages.append(self._to_message(t, d))

    def _to_message(self, t, d):
        m = {"ts": d.get("timestamp"), "role": t, "text": "", "thinking": None,
             "tools": [], "attachment": None}
        content = (d.get("message") or {}).get("content")
        if t == "user":
            if isinstance(content, str):
                m["text"] = content
            elif isinstance(content, list):
                texts, results = [], []
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        results.append(b)
                    else:
                        texts.append(str(b.get("text", "")))
                m["text"] = "\n".join(texts)
                self._attach_tool_results(results)
        elif t == "assistant":
            texts = []
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type")
                    if bt == "text":
                        texts.append(b.get("text", ""))
                    elif bt == "thinking":
                        m["thinking"] = b.get("thinking", "")
                    elif bt == "tool_use":
                        m["tools"].append({
                            "name": b.get("name", "tool"),
                            "summary": self._tool_summary(b),
                            "result": None,
                            "isError": False,
                        })
                    elif bt == "tool_result":
                        self._attach_tool_results([b])
            m["text"] = "\n".join(texts)
        elif t == "attachment":
            att = d.get("attachment") or {}
            m["attachment"] = {
                "kind": str(att.get("type", "attachment")),
                "content": truncate(att.get("content", ""), RESULT_TRUNC),
            }
        return m

    def _attach_tool_results(self, results):
        """把工具结果挂到最近一条尚无结果的 tool_use 上"""
        for b in results:
            for msg in reversed(self.messages):
                if msg["role"] == "assistant":
                    for tool in reversed(msg["tools"]):
                        if tool["result"] is None:
                            tool["result"] = self._result_text(b)
                            tool["isError"] = bool(b.get("is_error"))
                            break
                    else:
                        continue
                    break

    @staticmethod
    def _tool_summary(b):
        name = b.get("name", "")
        inp = b.get("input") or {}
        if name == "Bash":
            s = inp.get("command", "")
        elif name in ("Read", "Write", "Edit"):
            s = inp.get("file_path", "")
        elif name == "NotebookEdit":
            s = inp.get("notebook_path", "")
        elif name == "Glob":
            s = inp.get("pattern", "")
        elif name == "Grep":
            s = inp.get("pattern", "")
        elif name == "WebFetch":
            s = inp.get("url", "")
        elif name == "WebSearch":
            s = inp.get("query", "")
        elif name in ("TaskOutput", "TaskStop"):
            s = "task " + str(inp.get("task_id", ""))
        else:
            s = json.dumps(inp, ensure_ascii=False)
        return truncate(s, SUMMARY_TRUNC)

    @staticmethod
    def _result_text(b):
        c = b.get("content")
        if isinstance(c, str):
            s = c
        elif isinstance(c, list):
            s = "\n".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
        else:
            s = str(c)
        return truncate(s, RESULT_TRUNC)

    def fallback_title(self):
        for m in self.messages:
            if m["role"] == "user" and m["text"].strip():
                return truncate(m["text"].strip().splitlines()[0], 30)
        return self.sid[:8]


class Store:
    def __init__(self, root):
        self.root = root
        self.sessions = {}
        self.lock = threading.Lock()

    def poll(self):
        try:
            projects = os.listdir(self.root)
        except OSError:
            return
        new_sessions = []
        for proj in projects:
            pdir = os.path.join(self.root, proj)
            if not os.path.isdir(pdir):
                continue
            try:
                files = os.listdir(pdir)
            except OSError:
                continue
            for fn in files:
                if not fn.endswith(".jsonl"):
                    continue
                sid = fn[:-6]
                s = self.sessions.get(sid)
                if s is None:
                    s = Session(sid, os.path.join(pdir, fn), proj)
                    with self.lock:
                        self.sessions[sid] = s
                    new_sessions.append(f"{proj} / {sid[:8]}")
                s.poll()
        return new_sessions

    def overview(self):
        now = time.time()
        out = []
        with self.lock:
            for s in self.sessions.values():
                out.append({
                    "sessionId": s.sid,
                    "title": s.title or s.fallback_title(),
                    "project": s.project,
                    "cwd": s.cwd,
                    "branch": s.git_branch,
                    "lastTs": s.last_iso,
                    "msgCount": len(s.messages),
                    "running": bool(s.last_ts) and (now - s.last_ts) < RUNNING_WINDOW,
                })
        out.sort(key=lambda x: x["lastTs"] or "", reverse=True)
        return {"sessions": out}

    def messages(self, sid, after):
        with self.lock:
            s = self.sessions.get(sid)
            if s is None:
                return {"messages": [], "total": 0}
            msgs = s.messages
            return {
                "messages": msgs[after:],
                "total": len(msgs),
                "title": s.title or s.fallback_title(),
                "cwd": s.cwd,
                "branch": s.git_branch,
                "running": bool(s.last_ts) and (time.time() - s.last_ts) < RUNNING_WINDOW,
            }


class SendManager:
    """管理专用手机会话：串行发送消息给 claude -p 子进程"""

    def __init__(self, root, phone_sid, phone_cwd, claude_exe):
        self.root = root
        self.phone_sid = phone_sid
        self.phone_cwd = phone_cwd
        self.claude_exe = claude_exe
        self._lock = threading.Lock()
        self.sending = False
        self.since = None
        self.proc = None
        self.last_error = None
        self._stopped = False

    def status(self):
        with self._lock:
            return {"sending": self.sending, "since": self.since,
                    "phoneSessionId": self.phone_sid, "lastError": self.last_error}

    def submit(self, text):
        text = (text or "").strip()
        if not text:
            return 400, {"error": "消息不能为空"}
        if len(text) > 4000:
            return 400, {"error": "消息过长（最多 4000 字符）"}
        with self._lock:
            if self.sending:
                return 409, {"error": "上一条消息还在处理中，请稍候"}
            self.last_error = None
            self.sending = True
            self.since = datetime.now(timezone.utc).isoformat()
        threading.Thread(target=self._run, args=(text,), daemon=True).start()
        return 202, {"ok": True, "since": self.since}

    def _transcript_exists(self):
        for proj in os.listdir(self.root):
            if os.path.isfile(os.path.join(self.root, proj, self.phone_sid + ".jsonl")):
                return True
        return False

    def _run(self, text):
        try:
            flag = "--resume" if self._transcript_exists() else "--session-id"
            cmd = [self.claude_exe, "-p", text, flag, self.phone_sid,
                   "--permission-mode", "bypassPermissions"]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with self._lock:
                self.proc = subprocess.Popen(
                    cmd, cwd=self.phone_cwd,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=flags)
            code = self.proc.wait()
            with self._lock:
                stopped = self._stopped
                self._stopped = False
            if code != 0 and not stopped:
                self._fail(f"子进程退出码 {code}")
        except FileNotFoundError:
            self._fail("找不到 claude 可执行文件，请检查 PATH")
        except Exception as e:
            self._fail(f"执行出错: {e}")
        finally:
            with self._lock:
                self.sending = False
                self.since = None
                self.proc = None

    def _fail(self, msg):
        print(f"[手机会话] {msg}")
        with self._lock:
            self.last_error = msg

    def stop(self):
        p = None
        for _ in range(30):  # 等待 _run 线程完成 Popen（最多 3 秒）
            with self._lock:
                p = self.proc
            if p is not None:
                break
            if not self.sending:
                return {"ok": True, "stopped": False}
            time.sleep(0.1)
        if p is None:
            return {"ok": True, "stopped": False}
        with self._lock:
            self._stopped = True
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                           capture_output=True, timeout=10)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        return {"ok": True, "stopped": True}


class Handler(BaseHTTPRequestHandler):
    store = None
    pin = None
    send_manager = None
    allow_reuse_address = True

    def log_message(self, fmt, *args):
        pass  # 静默请求日志，避免轮询刷屏

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json")

    def _pin_ok(self, q):
        return not self.pin or q.get("pin", [""])[0] == self.pin

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not self._pin_ok(q):
            self._json({"error": "需要 PIN"}, 401)
            return
        try:
            if u.path == "/":
                self._send(200, HTML, "text/html")
            elif u.path == "/api/overview":
                self._json(self.store.overview())
            elif u.path == "/api/messages":
                sid = q.get("session", [""])[0]
                after = 0
                try:
                    after = int(q.get("after", ["0"])[0])
                except ValueError:
                    pass
                self._json(self.store.messages(sid, after))
            elif u.path == "/api/status":
                if self.send_manager is not None:
                    d = self.send_manager.status()
                    d["control"] = True
                else:
                    d = {"sending": False, "since": None,
                         "phoneSessionId": None, "control": False}
                self._json(d)
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if not self._pin_ok(q):
            self._json({"error": "需要 PIN"}, 401)
            return
        if self.send_manager is None:
            self._json({"error": "控制模式未启用（启动时加 --send）"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            body = json.loads(raw) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            self._json({"error": "请求格式错误"}, 400)
            return
        if u.path == "/api/send":
            code, resp = self.send_manager.submit(body.get("text", ""))
            self._json(resp, code)
        elif u.path == "/api/stop":
            self._json(self.send_manager.stop())
        else:
            self._json({"error": "not found"}, 404)


HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0d1117">
<title>CC 监视器</title>
<style>
  :root {
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #e6edf3; --dim: #8b949e; --accent: #58a6ff;
    --ok: #3fb950; --err: #f85149; --user-bubble: #1f6feb;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { background: var(--bg); color: var(--text);
    font: 16px/1.6 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
  header { position: sticky; top: 0; z-index: 10; background: var(--card);
    border-bottom: 1px solid var(--border); padding: 10px 14px; }
  .hrow { display: flex; align-items: center; gap: 10px; }
  #dot { width: 10px; height: 10px; border-radius: 50%; background: var(--dim); flex: none; }
  #dot.run { background: var(--ok); animation: pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .35 } }
  h1 { font-size: 17px; font-weight: 600; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #status { font-size: 12px; color: var(--dim); flex: none; }
  #sel { width: 100%; margin-top: 8px; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 10px; font-size: 16px; }
  #meta { font-size: 12px; color: var(--dim); padding: 6px 14px 0; }
  #warn { display: none; font-size: 12px; color: var(--err); padding: 6px 14px 0; }
  main { padding: 12px 14px 96px; }
  .msg { margin-bottom: 12px; }
  .u { background: var(--user-bubble); color: #fff; border-radius: 14px 14px 4px 14px;
    padding: 10px 14px; max-width: 92%; margin-left: auto; white-space: pre-wrap; word-break: break-word; }
  .meta-line { font-size: 11px; color: var(--dim); margin: 2px 4px 4px; display: flex; gap: 8px; }
  .a { background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 14px; }
  .a .text { white-space: pre-wrap; word-break: break-word; }
  details.think { color: var(--dim); font-style: italic; font-size: 14px; }
  details.think summary { cursor: pointer; }
  .tool { border: 1px solid var(--border); border-radius: 8px; margin-top: 8px; overflow: hidden; }
  .tool.err { border-color: var(--err); }
  .tool-head { display: flex; gap: 8px; align-items: flex-start; padding: 8px 10px;
    background: #10141b; }
  .badge { flex: none; font-size: 11px; font-weight: 600; color: var(--accent);
    border: 1px solid var(--accent); border-radius: 4px; padding: 1px 6px; margin-top: 2px; }
  .badge.err { color: var(--err); border-color: var(--err); }
  .tool code { font: 13px/1.5 Consolas, "Courier New", monospace; color: var(--text);
    word-break: break-all; white-space: pre-wrap; }
  .tool details { border-top: 1px solid var(--border); }
  .tool summary { cursor: pointer; font-size: 13px; color: var(--dim); padding: 6px 10px; }
  .tool pre { font: 12px/1.5 Consolas, "Courier New", monospace; padding: 8px 10px;
    white-space: pre-wrap; word-break: break-word; max-height: 320px; overflow: auto; }
  .att { border: 1px dashed var(--border); border-radius: 8px; padding: 8px 10px; }
  .att summary { cursor: pointer; font-size: 13px; color: var(--dim); }
  .att pre { font: 12px/1.5 Consolas, monospace; margin-top: 6px; white-space: pre-wrap;
    word-break: break-word; max-height: 200px; overflow: auto; }
  .empty { color: var(--dim); text-align: center; padding: 40px 0; }
  #err { display: none; color: var(--err); text-align: center; padding: 20px; }
  #sendbar { display: none; position: fixed; left: 0; right: 0; bottom: 0;
    background: var(--card); border-top: 1px solid var(--border); padding: 8px 12px calc(8px + env(safe-area-inset-bottom));
    z-index: 20; }
  .srow { display: flex; gap: 8px; align-items: flex-end; }
  #inp { flex: 1; background: var(--bg); color: var(--text); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 12px; font-size: 16px; font-family: inherit;
    resize: none; min-height: 42px; max-height: 120px; line-height: 1.5; }
  #btn { flex: none; background: var(--accent); color: #fff; border: none; border-radius: 10px;
    padding: 11px 18px; font-size: 16px; font-weight: 600; }
  #btn:disabled { opacity: .5; }
  #banner { display: none; position: fixed; left: 12px; right: 12px; bottom: 78px;
    background: #3d1518; color: var(--err); border: 1px solid var(--err); border-radius: 8px;
    padding: 8px 12px; font-size: 13px; z-index: 21; }
</style>
</head>
<body>
<header>
  <div class="hrow">
    <span id="dot"></span>
    <h1>CC 监视器</h1>
    <span id="status">连接中…</span>
  </div>
  <select id="sel"></select>
  <div id="meta"></div>
  <div id="warn">手机发来的消息会自动执行（包括修改文件），请只发送可信任务</div>
</header>
<main id="main"></main>
<div id="err">无法连接服务器，请确认监视器已启动</div>
<div id="banner"></div>
<div id="sendbar">
  <div class="srow">
    <textarea id="inp" rows="1" placeholder="给 CC 发消息…（回车发送，Shift+回车换行）" maxlength="4000"></textarea>
    <button id="btn">发送</button>
  </div>
</div>
<script>
const main = document.getElementById('main');
const sel = document.getElementById('sel');
const dot = document.getElementById('dot');
const st = document.getElementById('status');
const meta = document.getElementById('meta');
const warn = document.getElementById('warn');
const errBox = document.getElementById('err');
const sendbar = document.getElementById('sendbar');
const inp = document.getElementById('inp');
const btn = document.getElementById('btn');
const banner = document.getElementById('banner');

let pin = sessionStorage.getItem('ccpin') || '';
let msgs = [];
let total = 0;
let cur = null;
let sessions = [];
let stick = true;
let phoneSid = null;
let control = false;
let sending = false;
let bannerTimer = null;
let lastErrKey = null;

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtTs(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleTimeString('zh-CN', {hour12: false});
}
async function api(path, params) {
  const p = new URLSearchParams(params || {});
  if (pin) p.set('pin', pin);
  const r = await fetch('/api/' + path + '?' + p.toString());
  if (r.status === 401) {
    const v = prompt('请输入访问 PIN');
    if (v) { pin = v; sessionStorage.setItem('ccpin', pin); }
    return null;
  }
  if (!r.ok) return null;
  return r.json();
}
function showBanner(txt) {
  banner.textContent = txt;
  banner.style.display = 'block';
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => { banner.style.display = 'none'; }, 6000);
}
function hideBanner() { banner.style.display = 'none'; }

function msgEl(m) {
  const div = document.createElement('div');
  div.className = 'msg';
  const ts = '<div class="meta-line"><span>' + fmtTs(m.ts) + '</span></div>';
  if (m.role === 'user') {
    div.innerHTML = ts + '<div class="u">' + esc(m.text) + '</div>';
    return div;
  }
  if (m.role === 'attachment' && m.attachment) {
    div.innerHTML = ts + '<details class="att"><summary>附件 · ' + esc(m.attachment.kind)
      + '</summary><pre>' + esc(m.attachment.content) + '</pre></details>';
    return div;
  }
  let h = ts + '<div class="a">';
  if (m.thinking) h += '<details class="think"><summary>思考</summary>'
    + esc(m.thinking) + '</details>';
  if (m.text) h += '<div class="text">' + esc(m.text) + '</div>';
  for (const t of m.tools) {
    const errCls = t.isError ? ' err' : '';
    const okMark = t.isError ? '✗' : '✓';
    h += '<div class="tool' + errCls + '"><div class="tool-head">'
      + '<span class="badge' + errCls + '">' + esc(t.name) + '</span>'
      + '<code>' + esc(t.summary) + '</code>'
      + '<span style="color:var(--' + (t.isError ? 'err' : 'ok') + ')">' + okMark + '</span>'
      + '</div>';
    if (t.result !== null) {
      h += '<details' + (t.isError ? ' open' : '') + '><summary>结果' + (t.isError ? '（出错）' : '') + '</summary>'
        + '<pre>' + esc(t.result === '' ? '（无输出）' : t.result) + '</pre></details>';
    }
    h += '</div>';
  }
  h += '</div>';
  div.innerHTML = h;
  return div;
}

function renderNew() {
  if (!cur || !msgs.length) return;
  const frag = document.createDocumentFragment();
  for (const m of msgs) frag.appendChild(msgEl(m));
  main.appendChild(frag);
  if (stick) main.scrollTop = main.scrollHeight;
}
main.addEventListener('scroll', () => {
  stick = main.scrollHeight - main.scrollTop - main.clientHeight < 120;
});

function fillSel() {
  const curId = sel.value;
  let html = '';
  for (const s of sessions) {
    html += '<option value="' + esc(s.sessionId) + '"' + (s.sessionId === curId ? ' selected' : '')
      + '>' + (s.sessionId === phoneSid ? '[手机] ' : '') + (s.running ? '● ' : '')
      + esc(s.title) + ' — ' + esc(s.project) + '</option>';
  }
  sel.innerHTML = html;
  if (!curId && sessions.length) {
    cur = sessions[0].sessionId;
    sel.value = cur;
    resetView();
  }
}
sel.addEventListener('change', () => { cur = sel.value; resetView(); });

function resetView() {
  msgs = [];
  total = 0;
  main.innerHTML = '<div class="empty">加载中…</div>';
  pollMsgs();
}

function applyMeta(s) {
  meta.textContent = (s.cwd ? s.cwd : '') + (s.branch ? '  [' + s.branch + ']' : '');
  dot.className = s.running ? 'run' : '';
  st.textContent = s.running ? '运行中' : '空闲';
  const isPhone = control && cur === phoneSid;
  warn.style.display = isPhone ? 'block' : 'none';
  sendbar.style.display = isPhone ? 'block' : 'none';
}

async function pollOverview() {
  const d = await api('overview');
  if (!d) return;
  sessions = d.sessions;
  if (control && phoneSid && !sessions.find(s => s.sessionId === phoneSid)) {
    sessions = sessions.concat([{sessionId: phoneSid, title: '手机会话',
      project: '专用会话', cwd: null, branch: null, lastTs: null, msgCount: 0, running: false}]);
  }
  const curInfo = sessions.find(s => s.sessionId === cur) || sessions[0];
  if (curInfo) applyMeta(curInfo);
  fillSel();
}

async function pollMsgs() {
  if (!cur) return;
  const d = await api('messages', {session: cur, after: total});
  if (!d) return;
  if (d.total !== total) {
    if (d.total < total) { main.innerHTML = ''; msgs = []; }  // 会话被重写
    msgs = msgs.concat(d.messages);
    total = d.total;
    if (!msgs.length) {
      main.innerHTML = control && cur === phoneSid
        ? '<div class="empty">手机会话：发第一条消息开始使用</div>'
        : '<div class="empty">暂无消息</div>';
    } else {
      renderNew();
    }
  }
  applyMeta(d);
}

async function pollStatus() {
  const d = await api('status');
  if (!d) return;
  control = d.control || false;
  phoneSid = d.phoneSessionId || null;
  sending = d.sending || false;
  btn.disabled = sending;
  btn.textContent = sending ? '处理中…' : '发送';
  inp.disabled = sending;
  if (d.lastError && d.lastError !== lastErrKey) {
    lastErrKey = d.lastError;
    showBanner('发送失败：' + d.lastError);
  }
}

async function sendMsg() {
  if (sending) return;
  const t = inp.value.trim();
  if (!t) return;
  const p = new URLSearchParams();
  if (pin) p.set('pin', pin);
  let r;
  try {
    r = await fetch('/api/send?' + p.toString(), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: t})
    });
  } catch (e) { showBanner('网络错误：' + e.message); return; }
  if (r.status === 401) {
    const v = prompt('请输入访问 PIN');
    if (v) { pin = v; sessionStorage.setItem('ccpin', pin); }
    return;
  }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { showBanner(d.error || '发送失败 (' + r.status + ')'); return; }
  inp.value = '';
  hideBanner();
}

btn.addEventListener('click', sendMsg);
inp.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
});

(async function boot() {
  try {
    await pollStatus();
    await pollOverview();
    if (!cur && sessions.length) { cur = sessions[0].sessionId; resetView(); }
    else if (sessions.length) pollMsgs();
    setInterval(pollOverview, 10000);
    setInterval(pollMsgs, 2000);
    setInterval(pollStatus, 2000);
    errBox.style.display = 'none';
  } catch (e) {
    errBox.style.display = 'block';
  }
})();
</script>
</body>
</html>
"""


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="CC 手机监视/操作终端")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pin", default=None, help="访问 PIN（控制模式必须设置）")
    ap.add_argument("--root", default=None, help="transcript 目录（默认 ~/.claude/projects）")
    ap.add_argument("--send", action="store_true", help="启用控制模式：手机可向专用手机会话发消息")
    ap.add_argument("--phone-session-id", default=DEFAULT_PHONE_SESSION, help="专用手机会话 UUID")
    ap.add_argument("--phone-cwd", default=None, help="手机会话工作目录（默认 ~）")
    args = ap.parse_args()

    root = args.root
    if not root:
        root = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    if not os.path.isdir(root):
        print(f"[错误] transcript 目录不存在: {root}")
        sys.exit(1)

    claude_exe = None
    if args.send:
        if not args.pin:
            print("[错误] 控制模式（--send）必须同时设置 --pin")
            sys.exit(1)
        claude_exe = resolve_claude()
        if not claude_exe:
            print("[错误] 控制模式需要 claude 命令，请确认已安装并加入 PATH")
            sys.exit(1)

    store = Store(root)
    Handler.store = store
    Handler.pin = args.pin
    send_manager = None
    if args.send:
        phone_cwd = args.phone_cwd or os.path.expanduser("~")
        send_manager = SendManager(root, args.phone_session_id, phone_cwd, claude_exe)
        Handler.send_manager = send_manager

    def poll_loop():
        while True:
            try:
                for s in store.poll():
                    print(f"[新会话] {s}")
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)

    threading.Thread(target=poll_loop, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    ip = lan_ip()
    print("=" * 52)
    print("CC 监视器已启动")
    print(f"  本机访问:  http://localhost:{args.port}")
    print(f"  手机访问:  http://{ip}:{args.port}   (手机需连同一 WiFi)")
    print(f"  数据目录:  {root}")
    if args.send:
        print(f"  控制模式:  已启用（手机可发消息）")
        print(f"  手机会话:  {args.phone_session_id}")
        print(f"  PIN:       已开启（必须）")
    else:
        print(f"  控制模式:  未启用（只读监视，加 --send 启用）")
        print(f"  PIN:       {args.pin or '未开启'}")
    print("-" * 52)
    print("手机打不开时：检查 Windows 防火墙是否允许 Python 通过")
    print("（首次运行弹窗勾选\"专用网络\"，或到防火墙设置中手动放行）")
    print("Ctrl+C 停止")
    print("=" * 52)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
