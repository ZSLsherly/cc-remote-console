@echo off
chcp 65001 >nul
cd /d %USERPROFILE%
REM 手机远程终端：运行此脚本，用手机扫终端里的二维码，即可从任何地方连接并操作
REM （通过 bash -lc 启动，确保 DeepSeek 的 API 环境变量被加载）
REM 此窗口要保持打开，关闭窗口 = 结束远程会话
bash -lc "remote-claude start"
pause
