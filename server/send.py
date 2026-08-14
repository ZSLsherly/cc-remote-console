# -*- coding: utf-8 -*-
"""向任意 CC 会话串行发送消息（claude -p --resume）

- 可对任意会话发送（手机挑中哪个会话就控制哪个）
- 正在电脑终端活跃运行的会话拒绝介入（并发双写 transcript 会互相干扰）
- 全局单飞行：同一时刻只跑一个发送任务
"""
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

from webutil import truncate

SID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# 手机端支持的 CC 斜杠命令（无头 -p 模式下其余命令只是文本，会原样发给模型）
SUPPORTED_COMMANDS = {
    "/clear": "清空所选会话的对话上下文，开始全新对话（旧记录备份为 .bak 文件）",
    "/help": "查看手机端支持的斜杠命令",
}


class SendManager:
    def __init__(self, root, claude_exe, audit, notify=None,
                 session_check=None, default_cwd=None, on_cleared=None):
        self.root = root
        self.claude_exe = claude_exe
        self.audit = audit            # audit(event, detail, ip)
        self.notify = notify or (lambda title, body: None)   # 电脑端弹窗通知
        self.session_check = session_check or (lambda sid: {"running": False, "cwd": None})
        self.default_cwd = default_cwd or os.path.expanduser("~")
        self.on_cleared = on_cleared  # /clear 成功后回调（重置监视端内存状态）
        self._lock = threading.Lock()
        self.sending = False
        self.since = None
        self.proc = None
        self.last_error = None
        self._stopped = False
        self.current_sid = None
        self._last_finish_sid = None  # 手机自己刚写完的会话（活动窗口不算"电脑占用"）
        self._last_finish_ts = 0

    def status(self):
        with self._lock:
            return {"sending": self.sending, "since": self.since,
                    "lastError": self.last_error,
                    "currentSessionId": self.current_sid}

    def finished_recently(self, sid):
        """手机发送刚在该会话完成：其写入触发的活动窗口不应误拦下一次发送"""
        with self._lock:
            return self._last_finish_sid == sid and (time.time() - self._last_finish_ts) < 120

    def submit(self, sid, text, ip=""):
        text = (text or "").strip()
        if not SID_RE.match(sid or ""):
            return 400, {"error": "会话 ID 无效"}
        if not text:
            return 400, {"error": "消息不能为空"}
        if text.startswith("/"):
            return self._command(sid, text.strip().lower(), ip)
        if len(text) > 4000:
            return 400, {"error": "消息过长（最多 4000 字符）"}
        info = self.session_check(sid)
        if info.get("tuiBusy"):
            return 423, {"error": "电脑端 CC 正在该会话中处理任务，稍等片刻再发（处理完自动恢复）"}
        if info.get("running"):
            return 423, {"error": "该会话最近有电脑端活动，稍等片刻再发"}
        with self._lock:
            if self.sending:
                return 409, {"error": "上一条消息还在处理中，请稍候"}
            self.last_error = None
            self.sending = True
            self.since = datetime.now(timezone.utc).isoformat()
            self.current_sid = sid
        self.audit("手机发送", f"session={sid[:8]} text={truncate(text, 200)!r}", ip)
        self.notify("📱 收到手机指令", truncate(text, 80))
        threading.Thread(target=self._run, args=(sid, text, info.get("cwd")), daemon=True).start()
        return 202, {"ok": True, "since": self.since}

    # ---- 斜杠命令 ----
    def _command(self, sid, text, ip):
        if text == "/help":
            lines = "手机端支持的斜杠命令："
            for k, v in SUPPORTED_COMMANDS.items():
                lines += f"\n{k} — {v}"
            return 200, {"ok": True, "help": lines}
        if text == "/clear":
            info = self.session_check(sid)
            if info.get("tuiInSession"):
                return 423, {"error": "电脑端 CC 正停留在这个会话，切换走后再清空"}
            with self._lock:
                if self.sending:
                    return 409, {"error": "上一条消息还在处理中，请稍候"}
            path = self._find_transcript(sid)
            if path is None:
                return 200, {"ok": True, "cleared": False, "note": "该会话没有历史记录，无需清空"}
            backup = path + ".bak"
            try:
                # 原子重命名：下次发送检测不到旧文件，自动以 --session-id 同 sid 重建全新会话
                os.replace(path, backup)
            except OSError as e:
                return 500, {"error": f"清空失败: {e}"}
            if self.on_cleared:
                self.on_cleared(sid)
            self.audit("手机清空会话", f"session={sid[:8]} backup={os.path.basename(backup)}", ip)
            self.notify("🧹 手机清空会话", (info.get("title") or sid[:8]) + " 的上下文已清空（旧记录在 .bak）")
            return 200, {"ok": True, "cleared": True,
                         "note": "上下文已清空，下一条消息将开启全新对话"}
        return 400, {"error": "不支持的斜杠命令 " + text.split()[0] +
                     "（手机端仅支持 /clear、/help；其余命令请到电脑端 CC 执行）"}

    def _find_transcript(self, sid):
        try:
            for proj in os.listdir(self.root):
                p = os.path.join(self.root, proj, sid + ".jsonl")
                if os.path.isfile(p):
                    return p
        except OSError:
            pass
        return None

    def _transcript_exists(self, sid):
        return self._find_transcript(sid) is not None

    def _run(self, sid, text, cwd):
        try:
            flag = "--resume" if self._transcript_exists(sid) else "--session-id"
            cmd = [self.claude_exe, "-p", text, flag, sid,
                   "--permission-mode", "bypassPermissions"]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with self._lock:
                self.proc = subprocess.Popen(
                    cmd, cwd=cwd or self.default_cwd,
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
                self.current_sid = None
                self._last_finish_sid = sid
                self._last_finish_ts = time.time()

    def _fail(self, msg):
        print(f"[发送] {msg}")
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
