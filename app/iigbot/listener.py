# -*- coding: utf-8 -*-
"""Фоновый бот-слушатель (обнаружение чатов) для десктоп- и веб-версии.

Запускается в отдельном потоке-демоне со своими соединениями к базе и Telegram.
"""
import threading

from .storage import Storage
from .telegram_api import Telegram
from . import bot as botmod


def start(secrets, cfg):
    """Запускает слушатель в фоне. Возвращает (thread, stop_event)."""
    stop = threading.Event()

    def run():
        try:
            token = (secrets.get("telegram_bot_token") or "").strip()
            if not token or "ВСТАВЬ" in token:
                print("[listener] не задан telegram_bot_token — обнаружение чатов выключено.")
                return
            db = Storage(cfg["db_path"])               # отдельное соединение к той же базе (WAL)
            tg = Telegram(token, timeout=cfg["poll_timeout"])
            username = tg.get_me().get("username")
            print("[listener] @{} слушает Telegram (обнаружение чатов).".format(username))
            botmod.run_loop(db, tg, cfg, username, stop_event=stop)
        except Exception as e:  # noqa: BLE001
            print("[listener] остановлен: {}".format(e))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, stop
