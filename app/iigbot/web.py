# -*- coding: utf-8 -*-
"""Веб-версия: тот же интерфейс (ui.html), отдаётся по HTTP, действия идут через /api.

Не требует pywebview/WebView2 — открывается в обычном браузере. Удобно, когда десктоп-окно
недоступно. Слушает только localhost (127.0.0.1).
"""
import threading
import webbrowser

from flask import Flask, request, jsonify, Response

from .api import Api
from .settings import load_app_config, load_secrets, package_file
from . import listener


def create_app(api=None):
    api = api or Api()
    app = Flask(__name__)
    with open(package_file("ui.html"), encoding="utf-8") as f:
        ui_html = f.read()

    @app.route("/")
    def index():
        return Response(ui_html, mimetype="text/html; charset=utf-8")

    @app.route("/api/<method>", methods=["POST"])
    def call(method):
        if method.startswith("_"):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        fn = getattr(api, method, None)
        if not callable(fn):
            return jsonify({"ok": False, "error": "неизвестный метод: " + method}), 404
        args = request.get_json(force=True, silent=True)
        if args is None:
            args = []
        if not isinstance(args, list):
            args = [args]
        return jsonify(fn(*args))

    return app


def main(open_browser=True):
    cfg = load_app_config()
    port = int(cfg.get("web_port", 8077))

    try:
        listener.start(load_secrets(), cfg)   # фоновое обнаружение чатов
    except Exception as e:  # noqa: BLE001
        print("Слушатель не запущен: {}".format(e))

    app = create_app()
    url = "http://127.0.0.1:{}".format(port)
    print("Веб-версия IIG Reporter: {}  (Ctrl+C — выход)".format(url))
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()
