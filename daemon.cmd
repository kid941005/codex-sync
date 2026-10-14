@echo off
REM 前台运行守护进程（可见输出，调试用）。Ctrl+C 退出。
REM 静默后台运行请双击 cx-daemon.vbs，或安装开机自启：install-autostart.cmd
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

"!PY!" "%~dp0cx_sync.py" daemon %*
