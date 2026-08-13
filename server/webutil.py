# -*- coding: utf-8 -*-
"""公共工具：网络探测、claude 可执行文件解析、Git Bash 探测、字符串截断、Windows 通知"""
import os
import shutil
import socket
import subprocess


def lan_ip():
    """探测本机局域网 IP（仅本地路由探测，不发送数据包）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n] + "…(已截断)"


def resolve_claude():
    """找到 claude 可执行文件；npm 安装的是 .CMD shim，需解析出真实 exe

    Windows 的 CreateProcess 不能直接执行 .CMD，必须找到 node_modules 里的 claude.exe。
    """
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


def find_bash():
    """探测 Git Bash 路径（终端默认 shell），找不到返回 None 由调用方回退 cmd"""
    exe = shutil.which("bash")   # 覆盖非标准安装路径（如 D:\Git\usr\bin）
    if exe and os.path.isfile(exe):
        return exe
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def windows_toast(title, body):
    """Windows 系统通知（PowerShell + Windows.UI.Notifications，零依赖）

    用于把手机端活动（收到指令/执行完成/失败）弹到电脑屏幕。
    """
    if os.name != "nt":
        return

    def q(s):
        return "'" + str(s).replace("'", "''") + "'"

    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        "$n = $t.GetElementsByTagName('text'); "
        f"$n.Item(0).AppendChild($t.CreateTextNode({q(title)})) | Out-Null; "
        f"$n.Item(1).AppendChild($t.CreateTextNode({q(body)})) | Out-Null; "
        "$x = [Windows.UI.Notifications.ToastNotification]::new($t); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('CC-Monitor').Show($x)"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass
