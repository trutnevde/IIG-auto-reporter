# -*- coding: utf-8 -*-
"""Единая точка входа со всеми подкомандами.

  (без аргумента) / desktop  — нативное окно (pywebview)
  web                        — веб-версия в браузере
  weekly                     — рассылка отчётов по всем привязкам (для планировщика)
  sync                       — подтянуть клиентов из Директа
  import                     — импорт текущего config.json
  bot                        — только бот-слушатель в консоли

Используется и как `python -m iigbot ...`, и как собранный IIGReporter.exe.
"""
import os
import sys


def _ensure_stdio():
    """onefile-GUI-сборка (console=False): sys.stdout/stderr = None, и любой print() роняет
    процесс/поток. Перенаправляем вывод в файл рядом с программой — тогда ничего не падает."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    f = None
    try:
        from .settings import BASE_DIR
        f = open(os.path.join(BASE_DIR, "iig.log"), "a", encoding="utf-8", buffering=1)
    except Exception:
        try:
            f = open(os.devnull, "w")
        except Exception:
            return
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


HELP = (
    "IIG Reporter\n"
    "  (без аргумента)  десктоп-окно\n"
    "  web              веб-версия (открывается в браузере)\n"
    "  weekly           рассылка отчётов (для Планировщика задач)\n"
    "  autosync         суточное обслуживание: клиенты + цели + бюджеты (для cron)\n"
    "  sync             подтянуть клиентов из Директа\n"
    "  gsheets-sync     выгрузить Директ в Google-таблицы всех клиентов (cron/headless)\n"
    "  import           импорт config.json\n"
    "  bot              только слушатель чатов (консоль, long-polling)\n"
    "  webhook          вебхук для хостинга: webhook set <url> | delete | info\n"
    "  useradd          создать веб-аккаунт: useradd <email> <пароль> [Имя] [--admin]\n"
    "  users            список веб-аккаунтов\n"
    "  passwd           сменить пароль: passwd <email> <пароль>\n"
    "  assign           назначить клиентов: assign <email> <login> ...\n"
)


def main(argv=None):
    _ensure_stdio()
    try:
        from .settings import ensure_ca_bundle, ensure_secrets_template
        ensure_ca_bundle()              # стабильный CA рядом с exe (чинит падение слушателя по TLS)
        ensure_secrets_template()       # создаём шаблон secrets.json рядом, если его нет
    except Exception:
        pass
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = (args[0].lower().lstrip("-") if args else "desktop")

    if cmd in ("desktop", "gui", "app", ""):
        from . import desktop
        desktop.main()
    elif cmd in ("web", "w", "browser"):
        from . import web
        web.main()
    elif cmd in ("weekly", "run_weekly", "run-weekly", "send"):
        from . import run_weekly
        run_weekly.main()
    elif cmd in ("sync", "sync_clients"):
        from . import sync_clients
        sync_clients.main()
    elif cmd in ("autosync", "auto-sync", "daily"):
        # суточное обслуживание для cron: клиенты + цели + бюджеты (веб делает то же лениво)
        from .api import Api
        from .settings import load_secrets
        from . import budgets as B
        a = Api()
        r1 = a.sync_clients()
        print("клиенты:", (r1.get("data") or {}).get("synced", r1.get("error")))
        r2 = a.metrika_goals_bulk()
        d2 = r2.get("data") or {}
        print("цели: обновлены у {} из {} привязанных".format(
            d2.get("with_goals", "?"), d2.get("clients", "?")) if r2.get("ok") else "цели: " + str(r2.get("error")))
        tg = None
        try:
            tg = a._tg_client()
        except Exception:  # noqa: BLE001
            pass
        res = B.collect_and_alert(a.db, load_secrets()["yandex_oauth_token"], tg=tg)
        print("бюджеты: пул {}, активных {}, критичных {}".format(
            res.get("clients"), res.get("active"), res.get("critical")))
    elif cmd in ("gsheets-sync", "gsheets_sync", "gsheets", "gs-sync"):
        from . import gsheets_sync
        raise SystemExit(gsheets_sync.main())
    elif cmd in ("import", "import_config"):
        from . import import_config
        import_config.main()
    elif cmd == "bot":
        from . import bot
        bot.main()
    elif cmd in ("webhook", "hook"):
        from . import bot
        raise SystemExit(bot.webhook_command(args[1:]))
    elif cmd in ("useradd", "adduser", "create-admin"):
        from . import usercli
        raise SystemExit(usercli.useradd(args[1:]))
    elif cmd in ("users", "userlist"):
        from . import usercli
        raise SystemExit(usercli.users(args[1:]))
    elif cmd in ("passwd", "password"):
        from . import usercli
        raise SystemExit(usercli.passwd(args[1:]))
    elif cmd in ("useroff", "userdisable"):
        from . import usercli
        raise SystemExit(usercli.set_active(args[1:], False))
    elif cmd in ("useron", "userenable"):
        from . import usercli
        raise SystemExit(usercli.set_active(args[1:], True))
    elif cmd in ("assign", "assign-client"):
        from . import usercli
        raise SystemExit(usercli.assign(args[1:]))
    elif cmd in ("h", "help"):
        print(HELP)
    else:
        print("Неизвестная команда: {}\n".format(cmd) + HELP)


if __name__ == "__main__":
    main()
