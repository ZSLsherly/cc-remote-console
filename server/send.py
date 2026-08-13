# -*- coding: utf-8 -*-
"""手机会话管理：串行派发 claude -p 子进程（与终端会话隔离的专用会话）"""
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

from webutil import truncate


class SendManager:
    """管理专用手机会话：串行发送消息给 claude -p 子进程"""

    def __init__(self, root, phone_sid, phone_cwd, claude_exe, audit, notify=None):
        self.root = root
        self.phone_sid = phone_sid
        self.phone_cwd = phone_cwd
        self.claude_exe = claude_exe
        self.audit = audit            # audit(event, detail, ip)
        self.notify = notify or (lambda title, body: None)   # 电脑端弹窗通知
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

    def submit(self, text, ip=""):
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
        self.audit("手机发送", f"text={truncate(text, 200)!r}", ip)
        self.notify("📱 收到手机指令", truncate(text, 80))
        threading.Thread(target=self._run, args=(text,), daemon=True).start()
        return 202, {"ok": True, "since": self.since}

    def _transcript_exists(self):
        try:
            for proj in os.listdir(self.root):
                if os.path.isfile(os.path.join(self.root, proj, self.phone_sid + ".jsonl")):
                    return True
        except OSError:
            pass
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
                self.notify("⚠️ 手机任务失败", f"退出码 {code}")
            elif not stopped:
                self.notify("✅ 手机任务完成", "结果已同步到监视页面")
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

    def stop(self, ip=""):
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
        self.audit("手机停止", f"pid={p.pid}", ip)
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                           capture_output=True, timeout=10)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        return {"ok": True, "stopped": True}
