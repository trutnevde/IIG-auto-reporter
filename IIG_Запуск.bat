@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Запуск IIG Reporter (слушатель Telegram + интерфейс)...
rem Слушатель Telegram — отдельным процессом, вывод в файл (иначе он падает в GUI-сборке)
call "%~dp0_iig_listener.bat"
rem Интерфейс — десктоп-окно
start "" "%~dp0IIGReporter.exe"
echo.
echo Готово. Окно интерфейса откроется само.
echo Слушатель Telegram работает в фоне (лог: listener.log).
timeout /t 4 >nul
