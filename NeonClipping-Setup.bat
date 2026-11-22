@echo off
cd /d "%~dp0"
title NeonClipping Setup
if not exist "%~dp0install.ps1" (
  echo install.ps1 was not found next to this file.
  echo Open a terminal in the cloned NeonClipping folder and run install.ps1 there.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
