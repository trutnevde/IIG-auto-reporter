# -*- coding: utf-8 -*-
"""Десктоп-версия: нативное окно (pywebview) с тем же интерфейсом, что и веб-версия.

Внутри окна работает фоновый бот-слушатель (обнаружение чатов). Если WebView2/окно
недоступно — подскажет запустить веб-версию.
"""
import sys

from .api import Api
from .settings import load_secrets, load_app_config, package_file
from . import listener


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        import webview  # импорт здесь, чтобы веб-режим/CLI не требовали pywebview
    except ImportError:
        print("pywebview не установлен. Запусти веб-версию:  python -m iigbot web")
        return

    try:
        secrets = load_secrets()
    except Exception as e:  # noqa: BLE001
        print("secrets.json не найден/битый: {} — открываю окно, впиши токены в secrets.json".format(e))
        secrets = {}
    cfg = load_app_config()

    listener.start(secrets, cfg)   # слушатель чатов — отдельным процессом

    api = Api()
    with open(package_file("ui.html"), encoding="utf-8") as f:
        html = f.read()

    webview.create_window(
        "IIG Reporter",
        html=html,
        js_api=api,
        width=1240, height=820, min_size=(1000, 640),
    )
    webview.start()


if __name__ == "__main__":
    main()
