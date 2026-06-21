@echo off
cd /d "%~dp0"
rem Слушатель Telegram (подкоманда bot) отдельным процессом.
rem Вывод ОБЯЗАТЕЛЬНО перенаправляем в файл: в onefile-GUI-сборке (console=False) sys.stdout=None,
rem и первый же print() в слушателе роняет его. С перенаправлением stdout валиден и слушатель живёт.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~dp0IIGReporter.exe' -ArgumentList 'bot' -RedirectStandardOutput '%~dp0listener.log' -RedirectStandardError '%~dp0listener-err.log'"
