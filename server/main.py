# -*- coding: utf-8 -*-
"""CC 远程控制台 v2 — 监控 + 手机发送 + Web 终端（公网安全版）

安全架构：
  - 服务默认只绑定 127.0.0.1，由 tailscale serve 提供 Let's Encrypt HTTPS 反向代理
  - 登录：PBKDF2-HMAC-SHA256（21 万次迭代 + 随机盐）；密码只存哈希
  - 会话：256-bit 随机 token 存服务端，HttpOnly + SameSite=Strict + Secure cookie
  - 防护：登录失败锁out、限流、CSRF 双重提交、WS 握手 cookie 鉴权
  - 追溯：audit.log 记录登录/终端/发送/停止事件

用法：
  首次运行交互式设置密码:  python server/main.py
  重置密码:               python server/main.py --set-passwd 新密码
  局域网直连（明文，自担风险）: python server/main.py --lan
"""
import argparse
import collections
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from send import SendManager
from store import Store
from term import TerminalManager
import ws as wsmod
from webutil import find_bash, lan_ip, resolve_claude

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")
AUDIT_PATH = os.path.join(BASE_DIR, "audit.log")

DEFAULT_PHONE_SESSION = "1c0ffee0-0000-4000-8000-000000000001"  # 专用手机会话 UUID
SESSION_COOKIE = "cc_session"
CSRF_COOKIE = "cc_csrf"

DEFAULTS = {
    "user": "sherl",
    "http_port": 8765,
    "ws_port": 8766,
    "session_ttl_days": 7,
    "lockout_max_fails": 5,
    "lockout_minutes": 5,
    "terminal_idle_minutes": 30,
    "iterations": 210000,
}

MIME = {
    ".html": "text/html",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
    ".ico": "image/x-icon",
}


def make_hash(pw, salt_hex, iterations):
    return hashlib.pbkdf2_hmac(
        "sha256", pw.encode("utf-8"), bytes.fromhex(salt_hex), iterations).hex()


def load_config(args):
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in DEFAULTS.items():
            cfg.setdefault(k, v)
        if args.set_passwd:
            cfg["salt"] = secrets.token_hex(16)
            cfg["pw_hash"] = make_hash(args.set_passwd, cfg["salt"], cfg["iterations"])
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            print("[配置] 密码已重置")
        return cfg

    cfg = dict(DEFAULTS)
    print("=" * 52)
    print("首次运行：创建访问凭据（密码仅以哈希形式保存）")
    print("=" * 52)
    cfg["user"] = (args.user or input("用户名 [sherl]: ").strip()) or "sherl"
    if args.set_passwd:
        pw = args.set_passwd
    else:
        import getpass
        pw = getpass.getpass("设置密码（输入不回显）: ")
        pw2 = getpass.getpass("再次输入确认: ")
        if pw != pw2 or len(pw) < 8:
            print("[错误] 两次输入不一致或密码短于 8 位")
            sys.exit(1)
    cfg["salt"] = secrets.token_hex(16)
    cfg["pw_hash"] = make_hash(pw, cfg["salt"], cfg["iterations"])
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"[配置] 已生成 {CONFIG_PATH}")
    return cfg


class Auth:
    """认证：会话、防爆破锁out、限流、CSRF、审计日志"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()   # 可重入：audit() 可能在被锁保护的代码路径中调用
        self.sessions = {}          # token -> expires(epoch)
        self.fails = {}             # (ip, user) -> [count, lock_until]
        self.rate = {}              # ip -> deque(login 时间戳)
        self.csrf_tokens = {}       # token -> expires
        self._last_purge = time.time()

    # ---- 审计 ----
    def audit(self, event, detail="", ip=""):
        line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}Z | {ip or '-'} | {event} | {detail}"
        try:
            with self.lock:
                with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            pass

    def _purge(self):
        now = time.time()
        if now - self._last_purge < 300:
            return
        self._last_purge = now
        with self.lock:
            self.sessions = {t: e for t, e in self.sessions.items() if e > now}
            self.csrf_tokens = {t: e for t, e in self.csrf_tokens.items() if e > now}

    # ---- 登录 ----
    def login(self, user, pw, ip):
        self._purge()
        now = time.time()
        with self.lock:
            key = (ip, user)
            cnt, until = self.fails.get(key, (0, 0))
            locked = cnt >= self.cfg["lockout_max_fails"] and now < until
            if not locked:
                rate = self.rate.setdefault(ip, collections.deque())
                while rate and now - rate[0] > 60:
                    rate.popleft()
                if len(rate) >= 10:
                    return False, "请求过于频繁", None
                rate.append(now)
        if locked:
            self.audit("登录锁定", f"user={user}", ip)
            return False, "尝试次数过多，请 5 分钟后再试", None

        ok_user = hmac.compare_digest(user.encode(), self.cfg["user"].encode())
        ok_pw = ok_user and hmac.compare_digest(
            make_hash(pw, self.cfg["salt"], self.cfg["iterations"]).encode(),
            self.cfg["pw_hash"].encode())
        if not ok_pw:
            with self.lock:
                cnt, _ = self.fails.get(key, (0, 0))
                self.fails[key] = [cnt + 1, now + self.cfg["lockout_minutes"] * 60]
            self.audit("登录失败", f"user={user}", ip)
            return False, "用户名或密码错误", None
        with self.lock:
            self.fails.pop(key, None)
            token = secrets.token_urlsafe(32)
            self.sessions[token] = now + self.cfg["session_ttl_days"] * 86400
        self.audit("登录成功", f"user={user}", ip)
        return True, None, token

    def check(self, token):
        if not token:
            return False
        now = time.time()
        with self.lock:
            exp = self.sessions.get(token)
            if exp is None:
                return False
            if exp < now:
                del self.sessions[token]
                return False
        return True

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)

    # ---- CSRF（双重提交；令牌 1 小时有效，匿名期也签发以保护登录接口） ----
    def csrf_new(self):
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.csrf_tokens[token] = time.time() + 3600
        return token

    def csrf_valid(self, token):
        self._purge()
        with self.lock:
            return token in self.csrf_tokens


class Handler(BaseHTTPRequestHandler):
    auth = None
    store = None
    send_manager = None
    cfg = None

    # ---- 基础工具 ----
    def log_message(self, fmt, *args):
        pass  # 静默请求日志，事件走审计日志

    def _ip(self):
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def _https(self):
        return self.headers.get("X-Forwarded-Proto") == "https"

    def _cookies(self):
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, _, v = part.strip().partition("=")
                out[k] = v
        return out

    def _cookie_header(self, name, value, max_age, http_only=True):
        flags = [f"{name}={value}", "Path=/", f"Max-Age={max_age}", "SameSite=Strict"]
        if http_only:
            flags.append("HttpOnly")
        if self._https():
            flags.append("Secure")
        return "Set-Cookie: " + "; ".join(flags)

    def _base_headers(self):
        return [
            "X-Content-Type-Options: nosniff",
            "X-Frame-Options: DENY",
            "Referrer-Policy: no-referrer",
            "Content-Security-Policy: default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' ws: wss:; img-src 'self' data:; script-src 'self'",
        ]

    def _respond(self, code, body, ctype, extra_headers=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        headers = self._base_headers()
        headers.append(f"Content-Type: {ctype}; charset=utf-8")
        headers.append(f"Content-Length: {len(data)}")
        headers.append("Cache-Control: no-store")
        if extra_headers:
            headers.extend(extra_headers)
        try:
            self.send_response(code)
            for h in headers:
                self.send_header(*h.split(": ", 1))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200, cookies=None):
        self._respond(code, json.dumps(obj, ensure_ascii=False),
                      "application/json", cookies or [])

    def _authed(self):
        return self.auth.check(self._cookies().get(SESSION_COOKIE))

    # ---- 路由 ----
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/static/"):
                self._serve_static("index.html")
            elif path.startswith("/static/") or path in ("/manifest.json", "/sw.js", "/favicon.ico"):
                rel = path.lstrip("/")
                if rel.startswith("static/"):
                    rel = rel[len("static/"):]     # 文件在 STATIC_DIR 根下
                if rel == "favicon.ico":
                    rel = "icon-192.png"
                self._serve_static(rel)
            elif path == "/api/whoami":
                if self._authed():
                    self._json({"user": self.cfg["user"], "ws_url": self._ws_url()})
                else:
                    self._json({"error": "未登录"}, 401)
            elif path == "/api/overview":
                if not self._authed():
                    return self._json({"error": "未登录"}, 401)
                self._json(self.store.overview())
            elif path == "/api/messages":
                if not self._authed():
                    return self._json({"error": "未登录"}, 401)
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                sid = q.get("session", [""])[0]
                try:
                    after = int(q.get("after", ["0"])[0])
                except ValueError:
                    after = 0
                self._json(self.store.messages(sid, after))
            elif path == "/api/status":
                if not self._authed():
                    return self._json({"error": "未登录"}, 401)
                if self.send_manager is not None:
                    d = self.send_manager.status()
                    d["control"] = True
                else:
                    d = {"sending": False, "since": None,
                         "phoneSessionId": DEFAULT_PHONE_SESSION, "lastError": None,
                         "control": False}
                self._json(d)
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        # 所有 POST 先校验 CSRF（双重提交）
        csrf_ok = False
        try:
            cookies = self._cookies()
            csrf_ok = bool(cookies.get(CSRF_COOKIE)) and \
                hmac.compare_digest(
                    (self.headers.get("X-CSRF-Token") or "").encode(),
                    cookies[CSRF_COOKIE].encode()) and \
                self.auth.csrf_valid(cookies[CSRF_COOKIE])
        except (KeyError, AttributeError):
            csrf_ok = False
        if not csrf_ok:
            return self._json({"error": "CSRF 校验失败"}, 403)
        try:
            if path == "/api/login":
                body = self._read_json()
                ok, reason, token = self.auth.login(
                    body.get("user", ""), body.get("password", ""), self._ip())
                if not ok:
                    return self._json({"error": reason}, 401)
                cookies = [
                    self._cookie_header(
                        SESSION_COOKIE, token,
                        self.cfg["session_ttl_days"] * 86400, http_only=True),
                    self._cookie_header(
                        CSRF_COOKIE, self.auth.csrf_new(), 3600, http_only=False),
                ]
                return self._json({"ok": True, "user": self.cfg["user"]}, 200, cookies)
            if not self._authed():
                return self._json({"error": "未登录"}, 401)
            if path == "/api/logout":
                self.auth.logout(self._cookies().get(SESSION_COOKIE))
                self.auth.audit("登出", "", self._ip())
                return self._json({"ok": True})
            if path == "/api/send":
                if self.send_manager is None:
                    return self._json({"error": "claude 不可用，发送功能未启用"}, 503)
                code, resp = self.send_manager.submit(
                    self._read_json().get("text", ""), self._ip())
                return self._json(resp, code)
            if path == "/api/stop":
                if self.send_manager is None:
                    return self._json({"error": "claude 不可用，发送功能未启用"}, 503)
                return self._json(self.send_manager.stop(self._ip()))
            return self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            return json.loads(raw) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _ws_url(self):
        host = self.headers.get("Host") or f"127.0.0.1:{self.cfg['http_port']}"
        if self._https():
            return f"wss://{host}/term"
        return f"ws://{host.split(':')[0]}:{self.cfg['ws_port']}/term"

    # ---- 静态文件 ----
    def _serve_static(self, rel):
        if "\x00" in rel or ".." in rel.replace("\\", "/").split("/"):
            return self._json({"error": "bad path"}, 400)
        fp = os.path.abspath(os.path.join(STATIC_DIR, rel))
        if not fp.startswith(os.path.abspath(STATIC_DIR)) or not os.path.isfile(fp):
            return self._json({"error": "not found"}, 404)
        ext = os.path.splitext(fp)[1].lower()
        ctype = MIME.get(ext, mimetypes.guess_type(fp)[0] or "application/octet-stream")
        with open(fp, "rb") as f:
            data = f.read()
        extra = []
        if not self.auth.csrf_valid(self._cookies().get(CSRF_COOKIE)):
            extra.append(self._cookie_header(CSRF_COOKIE, self.auth.csrf_new(), 3600,
                                             http_only=False))
        self._respond(200, data, ctype, extra)


class Server(ThreadingHTTPServer):
    allow_reuse_address = False   # Windows 上防止两个服务同时绑定同一端口互抢请求


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import faulthandler
    faulthandler.enable()   # 崩溃/死锁时可通过 stderr 看到线程栈
    ap = argparse.ArgumentParser(description="CC 远程控制台 v2")
    ap.add_argument("--port", type=int, default=None, help="HTTP 端口（默认 config 值 8765）")
    ap.add_argument("--ws-port", type=int, default=None, help="WebSocket 端口（默认 config 值 8766）")
    ap.add_argument("--lan", action="store_true", help="绑定 0.0.0.0 供局域网直连（明文，自担风险）")
    ap.add_argument("--set-passwd", default=None, help="设置/重置密码（本地操作）")
    ap.add_argument("--user", default=None, help="首次运行时的用户名")
    ap.add_argument("--root", default=None, help="transcript 目录（默认 ~/.claude/projects）")
    args = ap.parse_args()

    cfg = load_config(args)
    http_port = args.port or cfg["http_port"]
    ws_port = args.ws_port or cfg["ws_port"]
    bind = "0.0.0.0" if args.lan else "127.0.0.1"

    root = args.root or os.path.join(os.path.expanduser("~"), ".claude", "projects")
    if not os.path.isdir(root):
        print(f"[错误] transcript 目录不存在: {root}")
        sys.exit(1)

    auth = Auth(cfg)
    store = Store(root)
    Handler.auth = auth
    Handler.store = store
    Handler.cfg = cfg

    # 手机会话发送（依赖 claude 可执行文件）
    claude_exe = resolve_claude()
    send_manager = None
    if claude_exe:
        send_manager = SendManager(root, DEFAULT_PHONE_SESSION,
                                   os.path.expanduser("~"), claude_exe, auth.audit)
        Handler.send_manager = send_manager
    else:
        print("[警告] 未找到 claude 命令，手机发送功能禁用（监控与终端仍可用）")

    # Web 终端（Git Bash 登录 shell 加载 DeepSeek 环境变量；无 Git Bash 回退 cmd）
    bash = find_bash()
    argv = [bash, "-l", "-i"] if bash else ["cmd.exe"]
    term_manager = TerminalManager(argv, os.path.expanduser("~"),
                                   ttl_minutes=cfg["terminal_idle_minutes"])
    term_ws = wsmod.TermWS(term_manager, lambda cookies: auth.check(cookies.get(SESSION_COOKIE)),
                           auth.audit)
    wsmod.start(bind, ws_port, term_ws)

    def poll_loop():
        while True:
            try:
                for s in store.poll():
                    print(f"[新会话] {s}")
            except Exception:
                pass
            time.sleep(1.0)

    threading.Thread(target=poll_loop, daemon=True).start()

    server = Server((bind, http_port), Handler)
    ip = lan_ip()
    print("=" * 56)
    print("CC 远程控制台 v2 已启动")
    print(f"  本机访问:  http://localhost:{http_port}")
    print(f"  绑定地址:  {bind}（{'局域网直连(明文!)' if args.lan else '仅本机，由 tailscale serve 代理'})")
    print(f"  终端通道:  ws://localhost:{ws_port}/term")
    print(f"  终端 shell:{'Git Bash (bash -l)' if bash else 'cmd'}")
    print(f"  手机会话:  {'已启用' if send_manager else '未启用（缺 claude）'}")
    print(f"  数据目录:  {root}")
    print(f"  审计日志:  {AUDIT_PATH}")
    print("-" * 56)
    print("公网访问：tailscale serve --bg http://127.0.0.1:%d" % http_port)
    print("  + tailscale serve --bg --set-path /term --websocket http://127.0.0.1:%d" % ws_port)
    print("之后手机（Tailscale App 登录同一账号）浏览器打开 https://<主机名>.ts.net")
    print("Ctrl+C 停止")
    print("=" * 56)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
