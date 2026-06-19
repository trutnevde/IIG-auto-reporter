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
import sys

HELP = (
    "IIG Reporter\n"
    "  (без аргумента)  десктоп-окно\n"
    "  web              веб-версия (открывается в браузере)\n"
    "  weekly           рассылка отчётов (для Планировщика задач)\n"
    "  sync             подтянуть клиентов из Директа\n"
    "  import           импорт config.json\n"
    "  bot              только слушатель чатов (консоль)\n"
)


def main(argv=None):
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
    elif cmd in ("import", "import_config"):
        from . import import_config
        import_config.main()
    elif cmd == "bot":
        from . import bot
        bot.main()
    elif cmd in ("h", "help"):
        print(HELP)
    else:
        print("Неизвестная команда: {}\n".format(cmd) + HELP)


if __name__ == "__main__":
    main()
