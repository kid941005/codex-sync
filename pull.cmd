@echo off
REM Codex 同步 —— 同步目录 -> 本机（请先完全退出 Codex）
setlocal
cd /d "%~dp0"
echo [提示] pull 会写入 state_*.sqlite，请先完全退出 Codex。
echo.
where python >nul 2>nul && (python cx_sync.py pull %*) || (py -3 cx_sync.py pull %*)
echo.
pause
endlocal
