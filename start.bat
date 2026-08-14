@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 先检查服务是否已在运行：若已运行，直接打开页面，避免端口冲突
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo [CC 控制台] 服务已在运行，直接打开本机监控页面...
    start http://localhost:8765
    echo 手机访问: https://ashly.tail41c6b0.ts.net （需 Tailscale App 已连接）
    pause
    exit /b
)

REM 服务未运行：启动（bash -lc 加载 DeepSeek 环境变量，勿删）
echo [CC 控制台] 正在启动服务，窗口保持打开...
bash -lc "python server/main.py"
pause
