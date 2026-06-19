# -*- coding: utf-8 -*-
"""CLI: перенести текущий config.json (PowerShell-версии) в локальную базу бота.

Берёт клиентов (login, name, goals, attribution_model) и, если у клиента указан chat_id,
сразу создаёт привязку чат->клиент. Так текущая настройка не теряется при переходе.

Запуск:  run_import_config.bat   (или  python -m iigbot.import_config)
"""
import sys

from .settings import load_app_config, load_report_config
from .storage import Storage


def normalize_goals(goals):
    """Приводим оба формата config.json к единому виду [{'id','name'}]."""
    out = []
    for g in goals or []:
        if isinstance(g, dict):
            gid = str(g.get("id"))
            out.append({"id": gid, "name": g.get("name") or "Цель {}".format(gid)})
        else:
            out.append({"id": str(g), "name": "Цель {}".format(g)})
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg = load_app_config()
    rep = load_report_config()
    if not rep:
        print("config.json не найден рядом с weekly_report.ps1 — нечего импортировать.")
        return
    db = Storage(cfg["db_path"])
    attribution = rep.get("attribution_model")
    n_cli = n_bind = 0
    for c in rep.get("clients") or []:
        login = c.get("login")
        if not login:
            continue
        db.upsert_client(
            login=login,
            name=c.get("name") or login,
            goals=normalize_goals(c.get("goals")),
            attribution=attribution,
            source="config",
        )
        n_cli += 1
        chat_id = c.get("chat_id")
        if chat_id:
            try:
                db.set_binding(int(chat_id), login)
                n_bind += 1
            except (TypeError, ValueError):
                print("  ! не смог разобрать chat_id у клиента {}: {!r}".format(login, chat_id))
    print("Импортировано клиентов: {}, привязок: {}".format(n_cli, n_bind))


if __name__ == "__main__":
    main()
