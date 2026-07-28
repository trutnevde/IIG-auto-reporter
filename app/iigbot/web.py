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


# ---------- ленивый планировщик (shared-хостинг: демонов нет, тикаем на живом трафике) ----------
_periodic = {"checked": 0.0}
_login_fails = {}   # анти-брутфорс входа: {"ip|email": {"n": попыток, "until": до какого времени пауза}}


def _periodic_tick(base):
    """Раз в ~10 минут смотрит kv-метки и запускает просроченные фоновые задачи:
    суточный автосинк клиентов+целей и 12-часовой сбор бюджетов (+алерты)."""
    import time as _t
    now = _t.time()
    if now - _periodic["checked"] < 600:
        return
    _periodic["checked"] = now

    def _due(key, hours):
        try:
            last = float(base.db.get_kv(key) or 0)
        except (TypeError, ValueError):
            last = 0.0
        return (now - last) >= hours * 3600

    if _due("autosync_last", 20):        # ~раз в сутки (20ч — гистерезис от времени захода)
        base.db.set_kv("autosync_last", now)   # метку ставим сразу — от двойного запуска
        threading.Thread(target=_autosync_job, args=(base,), daemon=True).start()
    if _due("budgets_last", 12):
        base.db.set_kv("budgets_last", now)
        threading.Thread(target=_budgets_job, args=(base,), daemon=True).start()
    if _due("dialog_alert_last", 3):   # каждые ~3 часа: «клиент ждёт ответа больше суток»
        base.db.set_kv("dialog_alert_last", now)
        threading.Thread(target=_dialog_job, args=(base,), daemon=True).start()
    if _due("backup_last", 24):      # суточный бэкап БД (WAL-safe) с ротацией
        base.db.set_kv("backup_last", now)
        threading.Thread(target=_backup_job, args=(base,), daemon=True).start()
    # еженедельная сводка работодателю: понедельник с 10:00, не чаще раза в 5 дней
    import datetime as _dt
    _n = _dt.datetime.now()
    if _n.weekday() == 0 and _n.hour >= 10 and _due("digest_last", 24 * 5):
        base.db.set_kv("digest_last", now)
        threading.Thread(target=_digest_job, args=(base,), daemon=True).start()


def _autosync_job(base):
    """Суточное авто-обновление: список клиентов Директа + цели привязанных (пресет ключевых,
    ручные галочки не трогаются — та же логика, что у кнопок). Итог — в Журнал."""
    from .settings import log_error
    try:
        r1 = base.sync_clients()
        r2 = base.metrika_goals_bulk()
        d1, d2 = (r1.get("data") or {}), (r2.get("data") or {})
        if not r1.get("ok"):
            raise RuntimeError("sync_clients: {}".format(r1.get("error")))
        if not r2.get("ok"):
            raise RuntimeError("metrika_goals_bulk: {}".format(r2.get("error")))
        log_error("autosync", "ок: клиентов в Директе {}, цели обновлены у {} из {} привязанных".format(
            d1.get("synced", "?"), d2.get("with_goals", "?"), d2.get("clients", "?")))
    except Exception as e:  # noqa: BLE001
        log_error("autosync", "сбой: {}".format(e))


def _release_notify(base, ui_html):
    """Если версия кабинета (дата последней записи в CHANGELOG) изменилась — уведомить всех
    в колокольчике «вышло обновление». Работает без ручного вмешательства при каждом выкате."""
    import re
    m = re.search(r"const CHANGELOG=\[\s*\{date:'([\d-]+)',tag:'([a-z]+)',title:'([^']+)'", ui_html)
    if not m:
        return
    date, _tag, title = m.group(1), m.group(2), m.group(3)
    key = "{} {}".format(date, title)
    if base.db.get_kv("release_seen") == key:
        return
    base.db.set_kv("release_seen", key)
    try:
        base.db.add_notification(None, "release", "Вышло обновление кабинета",
                                 title, "changelog", dedup_key=True)
    except Exception:  # noqa: BLE001
        pass


def _dialog_job(base):
    """Уведомления «клиент ждёт ответа больше суток» владельцам проектов."""
    from .settings import log_error
    try:
        n = base._dialog_alerts()
        if n:
            log_error("dialogs", "уведомлений о неотвеченных: {}".format(n))
    except Exception as e:  # noqa: BLE001
        log_error("dialogs", "сбой: {}".format(e))


def _backup_job(base):
    """Суточный бэкап БД: целостная копия (sqlite backup API) + ротация 14 копий."""
    from .settings import log_error
    try:
        name = base._make_backup(keep=14)
        log_error("backup", "ок: " + name)
    except Exception as e:  # noqa: BLE001
        log_error("backup", "сбой: {}".format(e))


def _digest_job(base):
    """Понедельничная сводка работодателю (покрытие, долги, деньги, нагрузка). Итог — в Журнал."""
    from .settings import log_error
    try:
        r = base.digest_send()
        d = r.get("data") or {}
        if not r.get("ok"):
            raise RuntimeError(r.get("error"))
        log_error("digest", "ок: отправлено {}{}".format(
            d.get("sent"), (", без привязки: " + ", ".join(d.get("missing") or [])) if d.get("missing") else ""))
    except Exception as e:  # noqa: BLE001
        log_error("digest", "сбой: {}".format(e))


def _budgets_job(base):
    """12-часовой сбор бюджетов + алерты в личку (см. budgets.py). Итог — в Журнал."""
    from .settings import log_error, load_secrets
    from . import budgets as B
    try:
        token = load_secrets()["yandex_oauth_token"]
        tg = None
        try:
            tg = base._tg_client()
        except Exception:  # noqa: BLE001
            tg = None
        res = B.collect_and_alert(base.db, token, tg=tg)
        log_error("budgets", "ок: пул {}, активных {}, критичных {}, предупреждений {}, ошибок {}".format(
            res.get("clients"), res.get("active"), res.get("critical"),
            res.get("warning"), res.get("errors")))
    except Exception as e:  # noqa: BLE001
        log_error("budgets", "сбой: {}".format(e))


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
    try:
        _release_notify(base, ui_html)   # при старте после выката — уведомить всех об обновлении
    except Exception:  # noqa: BLE001
        pass

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
        try:
            _periodic_tick(base)   # ленивые фоновые задачи (автосинк/бюджеты)
        except Exception:  # noqa: BLE001 — планировщик не должен ронять запросы
            pass

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
        # анти-брутфорс: после 5 неудач с одного IP+email — пауза, растущая до 5 минут
        import time as _t
        ip = (request.headers.get("X-Forwarded-For", "") or request.remote_addr or "?").split(",")[0].strip()
        data0 = request.get_json(force=True, silent=True) or {}
        who = (data0[0] if isinstance(data0, list) and data0 else (data0.get("email") if isinstance(data0, dict) else "")) or ""
        key = "{}|{}".format(ip, str(who).strip().lower())
        st = _login_fails.get(key)
        if st and st["until"] > _t.time():
            wait = int(st["until"] - _t.time()) + 1
            return jsonify({"ok": False,
                            "error": "Слишком много попыток входа. Попробуй через {} сек.".format(wait)}), 429
        data = data0
        if isinstance(data, list):   # контракт api() шлёт позиционные аргументы массивом
            email = data[0] if len(data) > 0 else ""
            password = data[1] if len(data) > 1 else ""
        else:
            email = data.get("email", "")
            password = data.get("password", "")
        row = base.db.get_user_by_email(email or "")
        if not row or not row["active"] or not auth.verify_password(row["pass_hash"], password):
            s = _login_fails.setdefault(key, {"n": 0, "until": 0})
            s["n"] += 1
            if s["n"] >= 5:   # 5-я и далее: пауза 15с, 30с, 60с … максимум 5 минут
                s["until"] = _t.time() + min(300, 15 * (2 ** (s["n"] - 5)))
            if len(_login_fails) > 500:   # не растим словарь бесконечно
                for k in [k for k, v in list(_login_fails.items()) if v["until"] < _t.time() - 3600][:200]:
                    _login_fails.pop(k, None)
            return jsonify({"ok": False, "error": "Неверный email или пароль"}), 401
        _login_fails.pop(key, None)   # успешный вход — счётчик сбрасываем
        auth.login_session(row["id"])
        return jsonify({"ok": True, "user": _safe_user(dict(row))})

    @app.route("/download/backup/<path:name>")
    def download_backup(name):
        """Скачать бэкап БД (только админ)."""
        if not g.user or g.user.get("role") != "admin":
            return jsonify({"ok": False, "error": "только администратор"}), 403
        from .settings import BASE_DIR
        fn = os.path.basename(name)
        if fn != name or not (fn.startswith("iigbot_") and fn.endswith(".sqlite3")):
            return jsonify({"ok": False, "error": "bad filename"}), 400
        path = os.path.join(BASE_DIR, "backups", fn)
        if not os.path.isfile(path):
            return jsonify({"ok": False, "error": "файл не найден"}), 404
        from flask import send_file
        return send_file(path, as_attachment=True, download_name=fn, mimetype="application/octet-stream")

    @app.route("/download/export/<what>.csv")
    def download_export(what):
        """Экспорт в CSV: clients | history | budgets (в рамках видимости пользователя)."""
        if not g.user:
            return jsonify({"ok": False, "error": "not_authenticated"}), 401
        import csv
        import io as _io
        api_u = _api_for(g.user)
        buf = _io.StringIO()
        w = csv.writer(buf, delimiter=";")
        if what == "clients":
            r = api_u.clients()
            if not r.get("ok"):
                return jsonify(r), 400
            w.writerow(["Логин", "Название", "Чат", "Доставка", "Целей активных", "Атрибуция", "Добавлен"])
            for c in r["data"]:
                goals = [g_ for g_ in (c.get("goals") or []) if g_.get("active") is not False]
                w.writerow([c["login"], c["name"], c.get("chat_title") or "", c.get("delivery"),
                            len(goals), c.get("attribution") or "", (c.get("added_at") or "")[:10]])
        elif what == "history":
            r = api_u.history()
            if not r.get("ok"):
                return jsonify(r), 400
            w.writerow(["Когда", "Клиент", "Чат", "Период", "Статус", "Ошибка"])
            for h in r["data"]:
                w.writerow([h.get("sent_at", "")[:19], h.get("login"), h.get("chat_title") or "",
                            "{} — {}".format(h.get("period_from"), h.get("period_to")),
                            h.get("status"), (h.get("error") or "")[:200]])
        elif what == "budgets":
            r = api_u.budgets("all" if g.user.get("role") in ("admin", "observer") else None)
            if not r.get("ok"):
                return jsonify(r), 400
            w.writerow(["Логин", "Клиент", "Остаток", "Темп/день", "Дней осталось", "Кампаний вкл",
                        "Остановлено по оплате", "Статус", "Обновлено"])
            for b in r["data"]["rows"]:
                w.writerow([b["login"], b["name"], b["balance"], b["rate"], b["days_left"],
                            b["camps_on"], b["camps_pay_stopped"], b["status"], (b.get("updated_at") or "")[:19]])
        else:
            return jsonify({"ok": False, "error": "неизвестный экспорт"}), 404
        data = "﻿" + buf.getvalue()   # BOM — чтобы Excel открыл кириллицу правильно
        return Response(data, mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="{}.csv"'.format(what)})

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
