# -*- coding: utf-8 -*-
"""CLI: ежедневная выгрузка Директа в Google-таблицы ВСЕХ клиентов (headless, без окна).

Для GitHub Actions (cron) или Планировщика задач Windows. Не зависит от локальной базы:
клиентов берёт из таблиц, расшаренных на сервисный аккаунт (домен из заголовка ↔ логин из
agencyclients). Нужны только secrets.json (yandex_oauth_token) и sa_key.json рядом с программой.

Запуск:  python -m iigbot gsheets-sync            (только ленты + составной лист текущего месяца)
         python -m iigbot gsheets-sync --breakdowns   (+ пересоздать листы-разрезы за текущий месяц)
"""
import re
import sys

from .settings import load_secrets
from . import gsheets as G


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
    print("Старт выгрузки в Google-таблицы{}…".format(" (+ разрезы)" if do_break else ""))
    res = G.sync_all(token, log=print, do_breakdowns=do_break)
    ok = sum(1 for r in res if r.get("ok"))
    bad = len(res) - ok
    print("Готово: {} ок, {} с ошибкой, всего таблиц {}.".format(ok, bad, len(res)))
    return 0 if bad == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
