# -*- coding: utf-8 -*-
"""Transcript 监控数据源：增量解析 ~/.claude/projects/*/*.jsonl

只读文件尾随（字节游标 + 半行缓冲），不干扰 Claude Code 写入。
"""
import json
import os
import threading
import time
from datetime import datetime

from webutil import pid_alive, truncate

RESULT_TRUNC = 2000      # 工具结果最大显示长度
SUMMARY_TRUNC = 300      # 工具入参摘要最大显示长度
RUNNING_WINDOW = 60      # 最后活动 N 秒内视为"运行中"（sessions 目录缺失时的回退）
POLL_INTERVAL = 1.0      # 文件扫描间隔（秒）

# CC 2.1+ 每个运行中的实例会在 ~/.claude/sessions/<pid>.json 登记当前会话/忙闲，
# 会话切换即时更新、进程退出即删除 —— 比"最近 60 秒有写入"精确得多。
SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "sessions")

# 系统噪音块：task-notification 等工具通知不该显示为聊天消息
NOISE_PREFIXES = ("<task-notification>", "<local-command-stdout>",
                  "<command-name>", "<command-message>", "<command-args>")
NOISE_TEXT_PREFIX = "[SYSTEM NOTIFICATION"


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
            m = self._to_message(t, d)
            if m is not None:      # 系统噪音块返回 None，不渲染
                self.messages.append(m)

    def _to_message(self, t, d):
        m = {"ts": d.get("timestamp"), "role": t, "text": "", "thinking": None,
             "tools": [], "attachment": None}
        content = (d.get("message") or {}).get("content")
        if t == "user":
            if isinstance(content, str):
                if self._is_noise(content):
                    return None     # <task-notification> 等系统通知，不显示
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

    @staticmethod
    def _is_noise(text):
        t = (text or "").strip()
        return t.startswith(NOISE_PREFIXES) or t.startswith(NOISE_TEXT_PREFIX)

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

    # ---- 交互式 CC 实例状态（~/.claude/sessions/*.json） ----
    def _tui_state(self):
        """返回 (tui, headless)：
        tui      = {sid: busy}   交互式实例（电脑 CC 窗口）当前所在会话及是否在生成
        headless = {sid}         无头 -p 实例（手机发送/脚本）：CC 内部队列会串行它们，无需拦截
        仅统计进程仍存活的条目；目录不存在（旧版 CC）时返回 (None, None) 由调用方回退。
        """
        try:
            files = os.listdir(SESSIONS_DIR)
        except OSError:
            return None, None
        tui, headless = {}, set()
        for fn in files:
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(SESSIONS_DIR, fn), encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            if d.get("kind") != "interactive":
                continue
            pid = d.get("pid")
            if not pid or not pid_alive(pid):
                continue
            sid = d.get("sessionId")
            if not sid:
                continue
            if d.get("entrypoint") == "sdk-cli":
                headless.add(sid)
            else:
                tui[sid] = tui.get(sid, False) or (d.get("status") == "busy")
        return tui, headless

    @staticmethod
    def _running(s, tui, headless):
        recent = bool(s.last_ts) and (time.time() - s.last_ts) < RUNNING_WINDOW
        if tui is None:
            return recent                    # 旧版 CC 无 sessions 目录：退回活动窗口
        if tui.get(s.sid):
            return True                      # 电脑交互 CC 正在该会话中生成
        if s.sid in headless:
            return False                     # 无头实例在写：队列串行，不视为占用
        return recent

    def overview(self):
        now = time.time()
        tui, headless = self._tui_state()
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
                    "running": self._running(s, tui, headless),
                })
        out.sort(key=lambda x: x["lastTs"] or "", reverse=True)
        return {"sessions": out}

    def messages(self, sid, after, limit=None):
        tui, headless = self._tui_state()
        with self.lock:
            s = self.sessions.get(sid)
            if s is None:
                return {"messages": [], "total": 0}
            msgs = s.messages
            total = len(msgs)
            seg = msgs[after:]
            if limit is not None:
                seg = seg[:limit]
            return {
                "messages": seg,
                "total": total,
                "title": s.title or s.fallback_title(),
                "cwd": s.cwd,
                "branch": s.git_branch,
                "running": self._running(s, tui, headless),
            }

    def session_info(self, sid):
        """发送管理器用：运行状态 + 交互实例占用情况 + 工作目录"""
        tui, headless = self._tui_state()
        with self.lock:
            s = self.sessions.get(sid)
        if s is None:
            return {"running": False, "tuiInSession": False, "tuiBusy": False,
                    "cwd": None, "title": None}
        return {
            "running": self._running(s, tui, headless),
            "tuiInSession": sid in (tui or {}),    # 电脑 CC 窗口正停留在这个会话（/clear 需避开）
            "tuiBusy": bool((tui or {}).get(sid)), # 电脑 CC 正在该会话中生成回复
            "cwd": s.cwd,
            "title": s.title or s.fallback_title(),
        }

    def reset_session(self, sid):
        """手机端 /clear：清空内存中的解析状态（transcript 文件由发送管理器备份重建）"""
        with self.lock:
            s = self.sessions.get(sid)
            if s is None:
                return False
            s.messages = []
            s.cursor = 0
            s.partial = b""
            s.title = None
            s.last_ts = None
            s.last_iso = None
            return True
