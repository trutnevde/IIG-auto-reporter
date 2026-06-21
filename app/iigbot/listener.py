# -*- coding: utf-8 -*-
"""Запуск фонового слушателя Telegram ОТДЕЛЬНЫМ процессом (подкоманда `bot`).

Почему процесс, а не поток: в onefile-сборке без консоли (console=False) фоновый
поток-слушатель внутри окна не работает надёжно (sys.stdout=None и особенности потоков
в PyInstaller). Отдельный процесс `IIGReporter.exe bot` с выводом в файл — проверенно рабочий
способ. От дублей защищает одиночный лок внутри самого `bot` (см. bot.main).
"""
import os
import sys
import subprocess

from .settings import BASE_DIR


def start(secrets, cfg=None):
    """Поднимает слушатель отдельным процессом. Возвращает Popen или None.

    Аргументы secrets/cfg оставлены для совместимости вызова — дочерний процесс (`bot`)
    грузит секреты и конфиг сам.
    """
    token = ((secrets or {}).get("telegram_bot_token") or "").strip()
    if not token or "ВСТАВЬ" in token:
        print("[listener] telegram_bot_token не задан — слушатель не запущен.")
        return None

    if getattr(sys, "frozen", False):
        args = [sys.executable, "bot"]          # тот же exe с аргументом bot
    else:
        args = [sys.executable, "-m", "iigbot", "bot"]

    try:
        logf = open(os.path.join(BASE_DIR, "listener.log"), "a", encoding="utf-8")
    except Exception:  # noqa: BLE001
        logf = subprocess.DEVNULL

    creationflags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW — без лишнего окна

    try:
        p = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT,
                             creationflags=creationflags)
        print("[listener] запущен отдельным процессом (pid {}), лог: listener.log".format(p.pid))
        return p
    except Exception as e:  # noqa: BLE001
        print("[listener] не удалось запустить: {}".format(e))
        return None
