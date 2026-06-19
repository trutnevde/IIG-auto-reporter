@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0weekly_report.ps1"
echo.
echo Done. You can close this window.
pause
