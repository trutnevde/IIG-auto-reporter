# -*- coding: utf-8 -*-
"""Веб-версия: тот же интерфейс (ui.html), отдаётся по HTTP, действия идут через /api.

Многопользовательский режим: вход по email+паролю (аккаунты заводит админ), серверные сессии,
каждый видит только своих клиентов. Легаси-десктоп по-прежнему создаёт Api() напрямую без
пользователя (владелец = «всё»).

Публичные методы (`/api/login|logout|me`) не требуют входа. Остальные `/api/<method>` — только
залогиненным; вызов идёт на Api, привязанный к текущему пользователю (кэш на пользователя, чтобы
фоновое состояние рассылки переживало между запросами-опросами).
"""
import os
import threading
import webbrowser

from flask import Flask, request, jsonify, Response, g

from .api import Api
from .settings import load_app_config, load_secrets, package_file
from . import auth, listener


# Api на пользователя: один инстанс на id (агентские кэши/фоновая рассылка живут между запросами).
_apis = {}
_apis_lock = threading.Lock()


def _api_for(user):
    key = user["id"] if user else "_agency"
    with _apis_lock:
        a = _apis.get(key)
        if a is None:
            a = Api(user=user)
            _apis[key] = a
        else:
            a.user = user   # свежие role/active/name на случай изменений
        return a


def _secret_key(db):
    """Постоянный ключ сессий: генерируем один раз и храним в kv (переживает рестарт)."""
    val = db.get_kv("web_secret")
    if not val:
        val = os.urandom(32).hex()
        db.set_kv("web_secret", val)
    return val


def _safe_user(u):
    if not u:
        return None
    return {"id": u["id"], "email": u["email"], "name": u.get("name"), "role": u.get("role")}


def create_app(api=None):
    base = api or Api()          # агентский Api (без пользователя) — для входа/сессий/сида
    app = Flask(__name__)
    app.secret_key = _secret_key(base.db)
    with open(package_file("ui.html"), encoding="utf-8") as f:
        ui_html = f.read()
    try:
        with open(package_file("favicon.svg"), "rb") as f:
            favicon_svg = f.read()
    except Exception:  # noqa: BLE001
        favicon_svg = b""

    @app.route("/favicon.svg")
    @app.route("/favicon.ico")
    def favicon():
        return Response(favicon_svg, mimetype="image/svg+xml")

    @app.before_request
    def _load_user():
        g.user = None
        uid = auth.session_user_id()
        if uid:
            row = base.db.get_user(uid)
            if row and row["active"]:
                g.user = dict(row)

    @app.route("/")
    def index():
        return Response(ui_html, mimetype="text/html; charset=utf-8")

    @app.route("/download/xlsx/<path:name>")
    def download_xlsx(name):
        """Отдаёт готовый .xlsx из reports/ браузеру (сохранение на сервере бесполезно вебу).
        Только залогиненным; имя — строго basename и только .xlsx (без обходов пути)."""
        if not g.user:
            return jsonify({"ok": False, "error": "not_authenticated"}), 401
        from .settings import BASE_DIR
        fn = os.path.basename(name)
        if not fn.lower().endswith(".xlsx") or fn != name:
            return jsonify({"ok": False, "error": "bad filename"}), 400
        path = os.path.join(BASE_DIR, "reports", fn)
        if not os.path.isfile(path):
            return jsonify({"ok": False, "error": "файл не найден (сгенерируй отчёт заново)"}), 404
        from flask import send_file
        return send_file(path, as_attachment=True, download_name=fn,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    def _login():
        data = request.get_json(force=True, silent=True) or {}
        if isinstance(data, list):   # контракт api() шлёт позиционные аргументы массивом
            email = data[0] if len(data) > 0 else ""
            password = data[1] if len(data) > 1 else ""
        else:
            email = data.get("email", "")
            password = data.get("password", "")
        row = base.db.get_user_by_email(email or "")
        if not row or not row["active"] or not auth.verify_password(row["pass_hash"], password):
            return jsonify({"ok": False, "error": "Неверный email или пароль"}), 401
        auth.login_session(row["id"])
        return jsonify({"ok": True, "user": _safe_user(dict(row))})

    @app.route("/api/<method>", methods=["POST"])
    def call(method):
        # публичные (до гейта)
        if method == "login":
            return _login()
        if method == "logout":
            auth.logout_session()
            return jsonify({"ok": True})
        if method == "me":
            return jsonify({"ok": True, "user": _safe_user(g.user),
                            "setup": base.db.count_users() == 0})

        if method.startswith("_"):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if not g.user:
            return jsonify({"ok": False, "error": "not_authenticated"}), 401

        api_u = _api_for(g.user)
        fn = getattr(api_u, method, None)
        if not callable(fn):
            return jsonify({"ok": False, "error": "неизвестный метод: " + method}), 404
        args = request.get_json(force=True, silent=True)
        if args is None:
            args = []
        if not isinstance(args, list):
            args = [args]
        return jsonify(fn(*args))

    @app.route("/tg/webhook", methods=["POST"])
    def tg_webhook():
        """Приём апдейтов Telegram (замена long-polling на хостинге). Секрет — в заголовке,
        который Telegram шлёт по secret_token из setWebhook. Обработка — та же handle_update."""
        secret = base.db.get_kv("tg_webhook_secret") or ""
        if not secret or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return ("forbidden", 403)
        update = request.get_json(force=True, silent=True) or {}
        try:
            from . import bot
            bot.handle_update(update, base._tg_client(), base.db, base.cfg, base._bot_name())
        except Exception as e:  # noqa: BLE001 — не роняем ответ, иначе Telegram зациклит ретраи
            print("[webhook] ошибка обработки апдейта: {}".format(e))
        return ("", 200)

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
