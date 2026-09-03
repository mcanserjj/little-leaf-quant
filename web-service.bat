@echo off
setlocal
cd /d "%~dp0"
if /i "%~1"=="stop" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\web-service.ps1" stop
  exit /b %errorlevel%
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\web-service.ps1" start
if errorlevel 1 (
  pause
  exit /b 1
)
start "" "http://127.0.0.1:8011"
