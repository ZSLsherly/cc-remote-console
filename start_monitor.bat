@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM 修改 PIN：把 1234 改成你自己的数字 PIN（控制模式必须设置）
REM 需要只读监视时，去掉 --send 即可
bash -lc "python cc_monitor.py --port 8765 --send --pin 1234"
pause
