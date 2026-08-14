# -*- coding: utf-8 -*-
"""向任意 CC 会话串行发送消息（claude -p --resume）

- 可对任意会话发送（手机挑中哪个会话就控制哪个）
- 会话独占：电脑 CC 停留在某会话时，手机消息自动排队，电脑切走后自动执行
  （同一会话两实例并发会让消息穿插进同一个上下文，互相干扰）
- 手机任务飞行中若电脑接管该会话：中止子进程并自动重试（≤2 次）
- 全局单飞行：同一时刻只跑一个发送任务
"""
import collections
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

from webutil import truncate

QUEUE_POLL = 3           # 排队轮询间隔（秒）
MAX_RETRIES = 2          # 被电脑接管打断后的自动重试上限

SID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# 手机端支持的 CC 斜杠命令
#  原生实现：/clear /help /skills /status /model /memory /export
#  其余 /xxx 视为 skill 调用：改写为自然语言，由模型通过技能系统执行
#  纯交互型命令（无头模式无法执行）：给出对应替代说明
INTERACTIVE_ONLY = {
    "/compact": "无头模式会在上下文接近上限时自动压缩，无需手动操作；"
                "如需立即压缩请到终端标签页运行 claude 后执行 /compact",
    "/cost": "无头模式无法查询用量统计；请到终端标签页运行 claude 后执行 /cost",
    "/context": "无头模式无法显示上下文窗口；请到终端标签页运行 claude 后执行 /context",
    "/usage": "无头模式无法查询用量；请到终端标签页运行 claude 后执行 /usage",
    "/config": "配置修改请到终端标签页运行 claude 后执行 /config",
    "/permissions": "权限设置请到终端标签页运行 claude 后执行 /permissions",
    "/agents": "子代理配置请到终端标签页运行 claude 后执行 /agents",
    "/resume": "无需该命令：用顶部下拉框即可切换任意会话",
    "/rewind": "无头模式不支持回退；需要时请到终端标签页运行 claude 后执行 /rewind",
    "/add-dir": "无头模式不支持添加目录；需要时请到终端标签页运行 claude 后执行 /add-dir",
    "/doctor": "诊断命令请到终端标签页运行 claude 后执行 /doctor",
    "/terminal-setup": "请到终端标签页运行 claude 后执行 /terminal-setup",
    "/login": "登录/账号操作请到终端标签页运行 claude 后执行",
    "/logout": "登录/账号操作请到终端标签页运行 claude 后执行",
    "/release-notes": "更新说明请到终端标签页运行 claude 后执行 /release-notes",
    "/bugs": "问题上报请到终端标签页运行 claude 后执行 /bugs",
    "/pr-comments": "PR 评论功能请到终端标签页运行 claude 后执行 /pr-comments",
    "/init": "项目初始化请到终端标签页运行 claude 后执行 /init",
}

# DeepSeek 后端可用模型（与 bash profile 中 ANTHROPIC_MODEL / HAIKU 映射一致）
MODELS = ["deepseek-v4-pro[1m]", "deepseek-v4-flash[1m]"]
DEFAULT_MODEL = "deepseek-v4-pro[1m]"

SKILL_DIRS = [
    os.path.join(os.path.expanduser("~"), ".claude", "skills"),        # 个人技能
    os.path.join(os.path.expanduser("~"), ".claude", "plugins", "cache"),  # 插件技能
]

HELP_TEXT = """手机端支持的斜杠命令：
/help — 本列表
/clear — 清空所选会话的上下文（旧记录备份为 .bak）
/skills — 列出全部可用技能
/status — 查看 claude 版本、模型后端、当前会话状态
/model — 查看/切换模型（如 /model deepseek-v4-flash[1m]）
/memory — 查看 CLAUDE.md 记忆内容
/export — 把所选会话导出为 Markdown 到电脑下载目录
/xxx — 其余以 / 开头的输入视为调用同名技能（如 /imagegen 描述文字）
纯交互命令（/cost /config /permissions 等）会给出替代说明。"""


class SendManager:
    def __init__(self, root, claude_exe, audit, notify=None,
                 session_check=None, default_cwd=None, on_cleared=None, store=None):
        self.root = root
        self.claude_exe = claude_exe
        self.audit = audit            # audit(event, detail, ip)
        self.notify = notify or (lambda title, body: None)   # 电脑端弹窗通知
        self.session_check = session_check or (lambda sid: {"running": False, "cwd": None})
        self.default_cwd = default_cwd or os.path.expanduser("~")
        self.on_cleared = on_cleared  # /clear 成功后回调（重置监视端内存状态）
        self.store = store            # /export 读取消息用
        self._models = {}             # sid -> 模型（/model 切换后生效）
        self._lock = threading.Lock()
        self.sending = False
        self.since = None
        self.proc = None
        self.last_error = None
        self._stopped = False
        self.current_sid = None
        self._last_finish_sid = None  # 手机自己刚写完的会话（活动窗口不算"电脑占用"）
        self._last_finish_ts = 0
        self._queue = collections.deque()   # 等待电脑让出会话的发送任务
        threading.Thread(target=self._queue_loop, daemon=True).start()

    def status(self):
        with self._lock:
            first = self._queue[0] if self._queue else None
            return {"sending": self.sending, "since": self.since,
                    "lastError": self.last_error,
                    "currentSessionId": self.current_sid,
                    "queued": len(self._queue),
                    "queueText": truncate(first["text"], 60) if first else None}

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
        return self._submit_text(sid, text, ip)

    def _submit_text(self, sid, text, ip=""):
        if len(text) > 4000:
            return 400, {"error": "消息过长（最多 4000 字符）"}
        info = self.session_check(sid)
        if info.get("tuiInSession"):
            # 会话独占：电脑 CC 停留该会话时排队，电脑切走后自动执行
            with self._lock:
                self._queue.append({"sid": sid, "text": text,
                                    "cwd": info.get("cwd"), "ip": ip, "retries": 0})
            self.audit("手机发送排队", f"session={sid[:8]} text={truncate(text, 200)!r}", ip)
            self.notify("📱 收到手机指令（已排队）", truncate(text, 80))
            self._tick()   # 电脑可能刚切走，立即尝试出队
            return 202, {"ok": True, "queued": True,
                         "note": "已排队：电脑端 CC 占用该会话中，电脑切走后自动执行（点发送键取消）"}
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
        item = {"sid": sid, "text": text, "cwd": info.get("cwd"), "ip": ip, "retries": 0}
        threading.Thread(target=self._run, args=(item,), daemon=True).start()
        return 202, {"ok": True, "since": self.since}

    # ---- 排队调度 ----
    def _queue_loop(self):
        while True:
            time.sleep(QUEUE_POLL)
            try:
                self._tick()
            except Exception:
                pass

    def _tick(self):
        """队列头任务在电脑让出会话后出队执行（全局单飞行）"""
        with self._lock:
            if self.sending or not self._queue:
                return
            item = self._queue[0]
        try:
            taken = self.session_check(item["sid"]).get("tuiInSession")
        except Exception:
            return                       # 检测失败就保守等待下一轮
        if taken:
            return
        started = False
        with self._lock:
            if not self.sending and self._queue and self._queue[0] is item:
                self._queue.popleft()
                self.sending = True
                self.since = datetime.now(timezone.utc).isoformat()
                self.current_sid = item["sid"]
                self.last_error = None
                started = True
        if not started:
            return
        self.audit("手机发送", f"session={item['sid'][:8]} text={truncate(item['text'], 200)!r}",
                   item["ip"])
        self.notify("📱 收到手机指令", truncate(item["text"], 80))
        threading.Thread(target=self._run, args=(item,), daemon=True).start()

    # ---- 斜杠命令 ----
    @staticmethod
    def _cmd(title, text):
        """命令结果：前端弹窗展示"""
        return {"ok": True, "cmd": title, "text": text}

    def _command(self, sid, text, ip):
        word = text.split()[0] if text.split() else text
        if word == "/help":
            return 200, self._cmd("手机端命令", HELP_TEXT)
        if word == "/clear":
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
        if word == "/skills":
            return 200, self._cmd("可用技能", self._list_skills(sid) or "未发现任何技能")
        if word == "/status":
            return 200, self._cmd("运行状态", self._status_text(sid))
        if word == "/model":
            return self._model_cmd(sid, text)
        if word == "/memory":
            return 200, self._cmd("记忆内容 (CLAUDE.md)", self._memory_text(sid))
        if word == "/export":
            return self._export_cmd(sid, ip)
        if word in INTERACTIVE_ONLY:
            return 200, self._cmd(word, INTERACTIVE_ONLY[word])
        # 其余 /xxx：视为 skill 调用，改写为自然语言让模型通过技能系统执行
        name = word[1:]
        rest = text[len(word):].strip()
        if rest:
            rewritten = f"请调用「{name}」技能完成以下要求：{rest}"
        else:
            rewritten = f"请调用「{name}」技能并先说明它的用途，再按技能说明执行"
        return self._submit_text(sid, rewritten, ip)

    # ---- 各命令实现 ----
    def _list_skills(self, sid):
        info = self.session_check(sid)
        bases = list(SKILL_DIRS)
        cwd = info.get("cwd")
        if cwd:   # 项目级技能（所选会话的工作目录）
            bases.insert(0, os.path.join(cwd, ".claude", "skills"))
        found = {}
        for base in bases:
            for dirpath, dirnames, filenames in os.walk(base):
                if "SKILL.md" not in filenames:
                    continue
                try:
                    with open(os.path.join(dirpath, "SKILL.md"), encoding="utf-8") as f:
                        head = f.read(1500)
                except OSError:
                    continue
                name = os.path.basename(dirpath)
                desc = self._skill_desc(head)
                found[name] = desc or "（无描述）"
        lines = [f"/{n} — {d}" for n, d in sorted(found.items())]
        lines.append("")
        lines.append("用法：在输入框直接以 /技能名 开头（可带要求文字），"
                     "或直接描述任务，模型会自动选用合适的技能。")
        return "\n".join(lines)

    @staticmethod
    def _skill_desc(head):
        """从 SKILL.md 头部 frontmatter 提取 description"""
        if not head.startswith("---"):
            return ""
        end = head.find("\n---", 3)
        if end < 0:
            return ""
        for ln in head[:end].splitlines():
            if ln.startswith("description:"):
                return ln[len("description:"):].strip().strip('"\'')
        return ""

    def _status_text(self, sid):
        info = self.session_check(sid)
        try:
            r = subprocess.run([self.claude_exe, "--version"],
                               capture_output=True, text=True, timeout=10,
                               encoding="utf-8", errors="replace")
            ver = (r.stdout or r.stderr or "").strip().splitlines()[-1]
        except Exception:
            ver = "未知"
        model = self._models.get(sid, DEFAULT_MODEL)
        return (f"claude 版本: {ver}\n"
                f"API 后端: {os.environ.get('ANTHROPIC_BASE_URL', '未设置')}\n"
                f"当前会话: {info.get('title') or sid[:8]} ({sid})\n"
                f"会话模型: {model}\n"
                f"工作目录: {info.get('cwd') or '未知'}\n"
                f"电脑 CC 是否停留此会话: {'是' if info.get('tuiInSession') else '否'}"
                + ("（正在生成）" if info.get('tuiBusy') else ""))

    def _model_cmd(self, sid, text):
        parts = text.split()
        if len(parts) == 1:
            cur = self._models.get(sid, DEFAULT_MODEL)
            return 200, self._cmd(
                "/model",
                f"当前会话模型: {cur}\n可用模型:\n" +
                "\n".join(f"  {m}" + ("（默认）" if m == DEFAULT_MODEL else "")
                          for m in MODELS) +
                "\n\n切换: /model <模型名>，例如 /model deepseek-v4-flash[1m]")
        model = parts[1]
        if model not in MODELS:
            return 400, {"error": f"未知模型 {model}；可用: {', '.join(MODELS)}"}
        self._models[sid] = model
        return 200, self._cmd("/model", f"已切换会话模型为 {model}（下一条消息起生效）")

    def _memory_text(self, sid):
        info = self.session_check(sid)
        cwd = info.get("cwd")
        paths = [("全局", os.path.join(os.path.expanduser("~"), ".claude", "CLAUDE.md"))]
        if cwd:
            paths.append(("项目", os.path.join(cwd, "CLAUDE.md")))
            paths.append(("项目 .claude", os.path.join(cwd, ".claude", "CLAUDE.md")))
        out = []
        for label, p in paths:
            if os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        out.append(f"【{label}】{p}\n" + truncate(f.read(), 3000))
                except OSError:
                    pass
        if not out:
            return "未找到任何 CLAUDE.md 记忆文件"
        return "\n\n".join(out)

    def _export_cmd(self, sid, ip):
        info = self.session_check(sid)
        msgs = self.store.messages(sid, 0)["messages"] if self.store else []
        if not msgs:
            return 200, self._cmd("/export", "该会话没有可导出的消息")
        lines = [f"# {info.get('title') or sid}\n", f"会话: {sid}\n"]
        for m in msgs:
            if m.get("role") == "user":
                if m.get("text"):
                    lines.append(f"\n## 我\n\n{m['text']}\n")
            elif m.get("role") == "assistant":
                if m.get("thinking"):
                    lines.append(f"\n> 思考: {m['thinking']}\n")
                if m.get("text"):
                    lines.append(f"\n## AI\n\n{m['text']}\n")
                for t in m.get("tools") or []:
                    lines.append(f"\n- 工具 {t['name']}: {t['summary']}"
                                 + (f"\n  → 结果: {t['result']}" if t.get("result") else ""))
        try:
            dl = os.path.join(os.path.expanduser("~"), "Downloads")
            os.makedirs(dl, exist_ok=True)
            fn = f"cc-export-{sid[:8]}-{time.strftime('%Y%m%d-%H%M%S')}.md"
            out = os.path.join(dl, fn)
            with open(out, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError as e:
            return 500, {"error": f"导出失败: {e}"}
        self.audit("手机导出会话", f"session={sid[:8]} file={fn}", ip)
        self.notify("📤 手机导出会话", f"{fn} 已保存到下载目录")
        return 200, self._cmd("/export", f"已导出到:\n{out}")

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

    def _run(self, item):
        sid, text, cwd = item["sid"], item["text"], item.get("cwd")
        ip, retries = item.get("ip", ""), item.get("retries", 0)
        try:
            flag = "--resume" if self._transcript_exists(sid) else "--session-id"
            cmd = [self.claude_exe, "-p", text]
            with self._lock:
                model = self._models.get(sid)
            if model:
                cmd += ["--model", model]
            cmd += [flag, sid, "--permission-mode", "bypassPermissions"]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with self._lock:
                self.proc = subprocess.Popen(
                    cmd, cwd=cwd or self.default_cwd,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=flags)
            taken = False
            code = None
            while True:   # 轮询等待，期间检测电脑是否接管该会话
                code = self.proc.poll()
                if code is not None:
                    break
                time.sleep(2)
                try:
                    if self.session_check(sid).get("tuiInSession"):
                        taken = True
                        break
                except Exception:
                    pass
            with self._lock:
                stopped = self._stopped
                self._stopped = False
            if taken and not stopped:
                self._kill_proc()
                self._requeue(item, sid, retries)
                return
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

    def _kill_proc(self):
        p = None
        with self._lock:
            p = self.proc
        if p is None:
            return
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                           capture_output=True, timeout=10)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def _requeue(self, item, sid, retries):
        """被电脑端接管打断：自动重新排队（有限次）"""
        if retries >= MAX_RETRIES:
            self._fail("任务多次被电脑端接管打断，已放弃（可稍后重发）")
            self.notify("⚠️ 手机任务失败", "多次被电脑端打断，已放弃")
            return
        with self._lock:
            self._queue.append({"sid": sid, "text": item["text"], "cwd": item.get("cwd"),
                                "ip": item.get("ip", ""), "retries": retries + 1})
        self.audit("手机任务被打断", f"session={sid[:8]} 电脑端接管，第 {retries + 1} 次自动重试",
                   item.get("ip", ""))
        self.notify("⏸ 手机任务暂停", "电脑端进入了该会话，任务已暂停并自动重试")

    def _fail(self, msg):
        print(f"[发送] {msg}")
        with self._lock:
            self.last_error = msg

    def stop(self, ip=""):
        with self._lock:
            cancelled = len(self._queue)
            self._queue.clear()      # 取消所有排队中的任务
        if cancelled:
            self.audit("手机取消排队", f"n={cancelled}", ip)
        p = None
        for _ in range(30):  # 等待 _run 线程完成 Popen（最多 3 秒）
            with self._lock:
                p = self.proc
            if p is not None:
                break
            if not self.sending:
                return {"ok": True, "stopped": False, "cancelled": cancelled}
            time.sleep(0.1)
        if p is None:
            return {"ok": True, "stopped": False, "cancelled": cancelled}
        with self._lock:
            self._stopped = True
        self.audit("手机停止", f"pid={p.pid}", ip)
        self._kill_proc()
        return {"ok": True, "stopped": True, "cancelled": cancelled}
