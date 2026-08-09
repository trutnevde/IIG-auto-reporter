# -*- coding: utf-8 -*-
"""CLI: ежедневная выгрузка Директа в Google-таблицы ВСЕХ клиентов (headless, без окна).

Для GitHub Actions (cron) или Планировщика задач Windows. Не зависит от локальной базы:
клиентов берёт из таблиц, расшаренных на сервисный аккаунт (домен из заголовка ↔ логин из
agencyclients). Нужны только secrets.json (yandex_oauth_token) и sa_key.json рядом с программой.

Запуск:  python -m iigbot gsheets-sync            (только ленты + составной лист текущего месяца)
         python -m iigbot gsheets-sync --breakdowns   (+ пересоздать листы-разрезы за текущий месяц)
         python -m iigbot gsheets-sync --notify       (+ написать в чат клиента, что отчёт обновлён)
"""
import os
import re
import sys

from .settings import load_secrets
from . import gsheets as G


def notify_map():
    """{логин: chat_id} для оповещений — из переменной IIG_SYNC_NOTIFY вида
    «porg-i26kekp2:-1003043371831,e-17442362:-100…».

    Через переменную, а не через базу: ежедневная выгрузка ходит в GitHub Actions, базы там нет.
    И не строкой в коде: id клиентских чатов в репозиторий не кладём.
    """
    out = {}
    for pair in (os.environ.get("IIG_SYNC_NOTIFY") or "").split(","):
        login, _, chat = pair.partition(":")
        if login.strip() and chat.strip():
            out[login.strip()] = chat.strip()
    return out


def notify(results):
    """Сообщение в чат клиента о том, что отчёт обновлён. Шлём только тем, кто явно указан
    в IIG_SYNC_NOTIFY, и только если выгрузка по нему прошла без ошибок."""
    who = notify_map()
    if not who:
        print("Оповещение: IIG_SYNC_NOTIFY не задан — некому писать.")
        return
    token = (load_secrets().get("telegram_bot_token") or "").strip()
    if not token or "ВСТАВЬ" in token:
        print("Оповещение: нет токена бота в secrets.json — пропуск.")
        return
    from .telegram_api import Telegram
    tg = Telegram(token)
    by_login = {r.get("login"): r for r in results if r.get("login")}
    for login, chat in who.items():
        r = by_login.get(login)
        if not r:
            print("Оповещение: {} в выгрузке не участвовал — пропуск.".format(login))
            continue
        if not r.get("ok"):
            print("Оповещение: {} выгрузился с ошибкой — не пишу.".format(login))
            continue
        text = ("Отчёт обновлён — в таблице свежие данные по рекламе.\n\n"
                "https://docs.google.com/spreadsheets/d/{}".format(r.get("sheet_id") or ""))
        try:
            tg.send_message(chat, text)
            print("Оповещение отправлено: {} -> {}".format(login, chat))
        except Exception as e:  # noqa: BLE001 — молчащий бот не должен ронять выгрузку
            print("Оповещение НЕ отправлено ({}): {}".format(login, str(e)[:120]))


def clean_token(raw):
    """Чистый ASCII-токен. Чистый токен — без изменений; если в секрет затесались стрей-символы
    (BOM/мусор при заливке из консоли), оставляем только валидные символы токена [A-Za-z0-9_.-].
    Никаких «умных» перекодировок — чтобы не выдать правдоподобно-неверный токен."""
    t = (raw or "").strip()
    if t.isascii():
        return t
    return re.sub(r"[^A-Za-z0-9_.\-]", "", t)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if not G.available():
        print("sa_key.json не найден рядом с программой — нет ключа Google.")
        return 1
    try:
        raw = load_secrets().get("yandex_oauth_token") or ""
    except Exception as e:  # noqa: BLE001
        print("Не удалось прочитать secrets.json: {}".format(e))
        return 1
    token = clean_token(raw)
    print("Токен: длина {} (исходно {}, ascii={})".format(len(token), len(raw), raw.isascii()))
    if not raw.isascii():  # диагностика порчи секрета: коды первых символов (не сам токен)
        print("DIAG ords[:24]: {}".format([ord(c) for c in raw[:24]]))
    if not token:
        print("Не задан/повреждён yandex_oauth_token в secrets.json")
        return 1

    flags = [a.lstrip("-").lower() for a in sys.argv[2:]]
    if any(f in ("check", "validate") for f in flags):
        # безопасная проверка токена: read-only вызов к Директу, в таблицы НИЧЕГО не пишем
        from . import yandex
        try:
            clients = yandex.get_agency_clients(token)
        except Exception as e:  # noqa: BLE001
            print("Токен НЕ работает: {}".format(e))
            return 1
        print("Токен ОК: клиентов в агентстве {}.".format(len(clients)))
        return 0

    do_break = any(f in ("breakdowns", "break") for f in flags)
    # Привязки таблиц и ручную разметку столбцов задают в кабинете — cron их читает из базы,
    # иначе ночная выгрузка размечала бы столбцы иначе, чем кнопка «Выгрузить».
    links, col_map = None, None
    try:
        from .storage import Storage
        db = Storage()
        links, col_map = db.client_sheets(), db.sheet_cols()
    except Exception as e:  # noqa: BLE001 — без базы синк всё равно работает по названиям
        print("База недоступна ({}), иду только по названиям таблиц".format(str(e)[:80]))
    print("Старт выгрузки в Google-таблицы{}…".format(" (+ разрезы)" if do_break else ""))
    res = G.sync_all(token, log=print, do_breakdowns=do_break, links=links, col_map=col_map)
    ok = sum(1 for r in res if r.get("ok"))
    bad = len(res) - ok
    print("Готово: {} ок, {} с ошибкой, всего таблиц {}.".format(ok, bad, len(res)))
    if "notify" in flags:
        notify(res)
    return 0 if bad == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
