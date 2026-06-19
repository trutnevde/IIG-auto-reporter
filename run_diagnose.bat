@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0diagnose.ps1"
echo.
echo Готово. Смотри вывод выше и файл diagnose.log. Окно можно закрыть.
pause
