@echo off
REM Codex 同步 —— 打开本地 Web 面板（默认 http://127.0.0.1:8766）
setlocal
cd /d "%~dp0"
where python >nul 2>nul && (python cx_sync.py ui %*) || (py -3 cx_sync.py ui %*)
endlocal
