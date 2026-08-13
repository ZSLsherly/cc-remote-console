# -*- coding: utf-8 -*-
"""WebSocket 服务：终端通道（会话 cookie 鉴权）

浏览器同源连接会自动携带 cookie（SameSite=Strict 天然防跨站 WS 劫持），
因此 WS 鉴权 = 校验 cc_session cookie，无需额外 CSRF。
"""
import asyncio
import json
import threading

from websockets.asyncio.server import serve


def _parse_cookies(header):
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, _, v = part.strip().partition("=")
            out[k] = v
    return out


class TermWS:
    def __init__(self, manager, auth_check, audit):
        self.manager = manager
        self.auth_check = auth_check   # (cookies: dict) -> bool
        self.audit = audit             # audit(event, detail, ip)

    async def handler(self, ws):
        cookies = _parse_cookies(ws.request.headers.get("Cookie", ""))
        ip = self._client_ip(ws)
        if not self.auth_check(cookies):
            await ws.close(code=4401, reason="unauthorized")
            return
        self.audit("终端连接", "", ip)
        loop = asyncio.get_running_loop()
        session = None
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                action = msg.get("action")
                if action == "attach":
                    term_id = msg.get("term_id") or self.manager.create_id()
                    session = self.manager.attach(
                        term_id, msg.get("rows", 24), msg.get("cols", 80))
                    session.clients.add((ws, loop))
                    await ws.send(json.dumps({
                        "type": "attached",
                        "term_id": session.term_id,
                        "history": session.history(),
                    }, ensure_ascii=False))
                elif action == "input" and session:
                    session.write(msg.get("data", ""))
                elif action == "resize" and session:
                    session.resize(msg.get("rows", 24), msg.get("cols", 80))
                elif action == "kill" and session:
                    session.kill()
                # 未知 action 忽略
        finally:
            if session:
                session.clients.discard((ws, loop))
            self.audit("终端断开", f"term_id={session.term_id if session else '-'}", ip)

    @staticmethod
    def _client_ip(ws):
        xff = ws.request.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return (ws.remote_address or ("-", 0))[0]


def start(host, port, term_ws):
    """在独立线程启动 asyncio WebSocket 服务"""
    loop = asyncio.new_event_loop()

    async def _run_forever():
        # websockets 16 新 API：serve() 必须在运行中的 loop 里调用，async with 管理生命周期
        async with serve(term_ws.handler, host, port):
            await asyncio.Future()   # 永不结束，直到进程退出

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run_forever())

    threading.Thread(target=run, daemon=True).start()
    return loop
