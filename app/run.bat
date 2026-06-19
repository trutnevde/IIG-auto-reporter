@echo off
rem Единый запуск (для тех, у кого установлен Python).
rem   run.bat            - десктоп-окно
rem   run.bat web        - веб-версия в браузере
rem   run.bat weekly     - рассылка отчётов
rem   run.bat sync       - клиенты из Директа
rem   run.bat import     - импорт config.json
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
python -m iigbot %*
