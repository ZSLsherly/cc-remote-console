@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM CC 远程控制台 v2 —— 监控 + 手机发送 + Web 终端
REM 通过 bash -lc 启动：继承 DeepSeek API 环境变量（终端和手机发送都依赖）
REM 首次运行会交互式设置登录密码（也可先执行: python server\main.py --set-passwd 你的密码）
bash -lc "python server/main.py"
pause
