@echo off
REM 查看守护进程状态与最近日志。
cd /d "%~dp0"
setlocal enabledelayedexpansion

set "PY="
for /f "delims=" %%D in ('dir /b /ad /o-n "%USERPROFILE%\.workbuddy\binaries\python\versions" 2^>nul') do (
  if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\%%D\python.exe" (
    set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\%%D\python.exe"
  )
)
if not defined PY for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"
if not defined PY (
  echo [ERROR] python not found.
  pause
  exit /b 1
)

echo.
"!PY!" "%~dp0cx_sync.py" daemon --status
echo.
echo   ------------- daemon.log (tail) -------------
if exist "%LOCALAPPDATA%\codex-sync\daemon.log" (
  powershell -NoProfile -Command "Get-Content -Tail 25 -Encoding UTF8 '%LOCALAPPDATA%\codex-sync\daemon.log'"
) else (
  echo   ^(还没有日志文件^)
)
echo.
pause
