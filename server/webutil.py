# -*- coding: utf-8 -*-
"""公共工具：网络探测、claude 可执行文件解析、Git Bash 探测、字符串截断、Windows 通知"""
import os
import shutil
import socket
import subprocess
import time


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


def pid_alive(pid):
    """Windows 进程存活探测（OpenProcess SYNCHRONIZE，无需句柄权限）"""
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x100000, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def stop_tui(pid):
    """手机端强制终止电脑端的交互式 CC：

    先尝试向它所在的控制台发送 Ctrl+C（优雅中断当前生成，保留窗口）；
    2.5 秒后进程仍存活则 taskkill 强杀进程树（关闭电脑端 CC 窗口）。
    返回 'ctrl-c' | 'force' | 'failed'。
    """
    pid = int(pid)
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.FreeConsole()                      # 本服务进程先脱离自己的控制台
        if kernel32.AttachConsole(pid):             # 挂到目标 CC 所在控制台
            kernel32.SetConsoleCtrlHandler(None, True)  # 保护本进程不被 Ctrl+C 波及
            kernel32.GenerateConsoleCtrlEvent(0, 0)     # 0 = 发给该控制台全部进程
            kernel32.FreeConsole()
    except Exception:
        pass
    time.sleep(2.5)
    if pid_alive(pid):
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)
            return "force"
        except Exception:
            return "failed"
    return "ctrl-c"


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
