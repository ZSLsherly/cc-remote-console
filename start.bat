@echo off
cd /d "%~dp0"

REM ============ CC Console launcher (self-healing) ============
REM Step 1: if the service is already up and healthy, just open the page
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8765/api/whoami' -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop; exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
    echo [CC Console] Service already running. Opening dashboard...
    start http://localhost:8765
    echo Phone: https://ashly.tail41c6b0.ts.net
    pause
    exit /b 0
)

REM Step 2: kill any stale process holding ports 8765/8766
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8766" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
timeout /t 1 /nobreak >nul

REM Step 3: locate Git Bash (full paths so it works when double-clicked)
set "BASH="
where bash >nul 2>&1 && set "BASH=bash"
if not defined BASH if exist "D:\Git\usr\bin\bash.exe" set "BASH=D:\Git\usr\bin\bash.exe"
if not defined BASH if exist "C:\Program Files\Git\bin\bash.exe" set "BASH=C:\Program Files\Git\bin\bash.exe"
if not defined BASH (
    echo [ERROR] Git Bash not found. Install Git for Windows first.
    pause
    exit /b 1
)

REM Step 4: start the server (bash -lc loads DeepSeek env from profile)
echo [CC Console] Starting service. Keep this window open.
"%BASH%" -lc "python server/main.py"
pause
