# -*- coding: utf-8 -*-
"""CLI: разослать отчёты по всем привязанным клиентам (за прошлую неделю).

Без окна — для «Планировщика задач» Windows (например, Пн 09:00).
Запуск: run_weekly.bat  (или  python -m iigbot.run_weekly)
"""
import sys

from .settings import load_secrets, load_app_config, load_report_config
from .storage import Storage
from .telegram_api import Telegram
from . import report


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    secrets = load_secrets()
    cfg = load_app_config()
    rep = load_report_config()
    token = secrets.get("yandex_oauth_token")
    bot_token = (secrets.get("telegram_bot_token") or "").strip()
    if not token or not bot_token:
        print("Не заданы токены в secrets.json")
        return
    tg = Telegram(bot_token, timeout=20)
    db = Storage(cfg["db_path"])
    intro = rep.get("intro") or "Отчёт за прошлую неделю."
    note = rep.get("specialist_note") or "Через некоторое время специалист даст комментарий по этому отчёту."
    attr = rep.get("attribution_model") or "LSC"
    res = report.run_weekly(token, tg, db, intro, note, attr)
    print("Готово: отправлено {sent}, пропущено {skipped}, без чата {no_chat}, ошибок {errors}".format(**res))
    for d in res["details"]:
        if d.get("status") in ("error", "no_chat", "skipped"):
            print("  • {}: {} {}".format(d.get("login"), d.get("status"), d.get("reason", "")))


if __name__ == "__main__":
    main()
