@echo off
REM Codex 同步 —— 本机 -> 同步目录（Codex 运行时也可执行，只读快照）
setlocal
cd /d "%~dp0"
where python >nul 2>nul && (python cx_sync.py push %*) || (py -3 cx_sync.py push %*)
echo.
pause
endlocal
