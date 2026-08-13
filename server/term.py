# -*- coding: utf-8 -*-
"""Web 终端会话管理：winpty (ConPTY) + 环形缓冲 + 断线重连

终端会话在服务端持久：手机切后台断 WS 不丢会话，重连回放环形缓冲后继续实时流。
"""
import asyncio
import collections
import json
import secrets
import threading
import time

import winpty

RING_SIZE = 3000          # 环形缓冲行数（重连回放上限）


class TerminalSession:
    def __init__(self, term_id, argv, cwd, env=None, rows=24, cols=80):
        self.term_id = term_id
        self.argv = argv
        self.ring = collections.deque(maxlen=RING_SIZE)
        self.clients = set()      # {(ws_conn, event_loop), ...}
        self.lock = threading.Lock()
        self.alive = True
        self.last_active = time.time()
        self.on_exit = None       # 由 manager 注入清理回调
        self.proc = winpty.PtyProcess.spawn(
            argv, cwd=cwd, env=env, dimensions=(rows, cols))
        self.rows, self.cols = rows, cols
        threading.Thread(target=self._read_loop, daemon=True).start()

    # ---- 读取线程（阻塞 read → 广播） ----
    def _read_loop(self):
        try:
            while True:
                data = self.proc.read(4096)
                if not data:
                    continue
                text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
                with self.lock:
                    self.ring.append(text)
                    self.last_active = time.time()
                self._broadcast({"type": "output", "data": text})
        except (EOFError, OSError, winpty.WinptyError):
            pass
        finally:
            with self.lock:
                self.alive = False
            self._broadcast({"type": "exit"})
            if self.on_exit:
                try:
                    self.on_exit(self)
                except Exception:
                    pass

    def _broadcast(self, msg):
        payload = json.dumps(msg, ensure_ascii=False)
        for ws, loop in list(self.clients):
            try:
                asyncio.run_coroutine_threadsafe(ws.send(payload), loop)
            except Exception:
                pass

    # ---- 控制面 ----
    def write(self, data):
        with self.lock:
            self.last_active = time.time()
        try:
            self.proc.write(data)
        except OSError:
            pass

    def resize(self, rows, cols):
        rows = max(1, int(rows)); cols = max(1, int(cols))
        try:
            self.proc.setwinsize(rows, cols)
            self.rows, self.cols = rows, cols
        except OSError:
            pass

    def history(self):
        with self.lock:
            return "".join(self.ring)

    def kill(self):
        try:
            self.proc.terminate(force=True)
        except OSError:
            pass
        # 读线程随后收到 EOF → alive=False → on_exit 清理

    def is_idle(self, ttl_seconds):
        with self.lock:
            return self.alive and (time.time() - self.last_active) > ttl_seconds


class TerminalManager:
    def __init__(self, argv, cwd, env=None, ttl_minutes=30):
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.ttl_seconds = ttl_minutes * 60
        self.sessions = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._reaper, daemon=True).start()

    def create_id(self):
        return secrets.token_hex(6)

    def attach(self, term_id, rows=24, cols=80):
        """连接已有会话；term_id 未知或已退出则新建"""
        s = self.get(term_id)
        if s is None:
            s = self._create(term_id, rows, cols)
        else:
            s.resize(rows, cols)
        return s

    def get(self, term_id):
        with self.lock:
            s = self.sessions.get(term_id)
        return s if s and s.alive else None

    def _create(self, term_id, rows, cols):
        s = TerminalSession(term_id, self.argv, self.cwd,
                            env=self.env, rows=rows, cols=cols)
        s.on_exit = self._remove
        with self.lock:
            self.sessions[term_id] = s
        return s

    def _remove(self, sess):
        with self.lock:
            if self.sessions.get(sess.term_id) is sess:
                del self.sessions[sess.term_id]

    def _reaper(self):
        while True:
            time.sleep(60)
            with self.lock:
                dead = [s for s in self.sessions.values() if s.is_idle(self.ttl_seconds)]
            for s in dead:
                try:
                    s.kill()
                except Exception:
                    pass
