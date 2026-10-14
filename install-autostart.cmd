@echo off
title codex-sync - INSTALL autostart
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
  echo [ERROR] python not found. Install Python or run WorkBuddy once.
  echo.
  pause
  exit /b 1
)

"!PY!" "%~dp0create-autostart.py" %*
echo.
pause
