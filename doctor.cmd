@echo off
REM Codex 同步 —— 环境自检（先跑这个）
setlocal
cd /d "%~dp0"
where python >nul 2>nul && (python cx_sync.py doctor %*) || (py -3 cx_sync.py doctor %*)
echo.
pause
endlocal
