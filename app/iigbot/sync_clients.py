# -*- coding: utf-8 -*-
"""CLI: подтянуть список клиентов из агентского аккаунта Директа в локальную базу.

Запуск:  run_sync_clients.bat   (или  python -m iigbot.sync_clients)
Существующие цели/атрибуцию у клиентов не затирает (обновляет только имя/источник).
"""
import sys

from .settings import load_app_config, load_secrets
from .storage import Storage
from . import yandex


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg = load_app_config()
    secrets = load_secrets()
    db = Storage(cfg["db_path"])
    try:
        clients = yandex.get_agency_clients(secrets["yandex_oauth_token"])
    except Exception as e:  # noqa: BLE001
        print("Ошибка обращения к API Директа: {}".format(e))
        return
    n = 0
    for c in clients:
        login = c.get("Login")
        if not login:
            continue
        db.upsert_client(login=login, name=c.get("ClientInfo") or login, source="yandex")
        n += 1
    print("Синхронизировано клиентов из Директа: {}".format(n))


if __name__ == "__main__":
    main()
