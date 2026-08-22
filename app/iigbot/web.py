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

from flask import Flask, request, jsonify, Response, g, redirect

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
# Расход процессора по методам. На виртуальном хостинге это тот ресурс, из-за нехватки
# которого хостер гасит процесс, — а понять, какая операция дорогая, было неоткуда.
# Копим в памяти и пишем в базу пачкой: запись на каждый запрос стоила бы дороже замера.
_cpu_buf = {}
_cpu_flushed = {"at": __import__("time").time()}


def _cpu_note(base, method, secs):
    import time as _t
    import datetime as _d
    key = (_d.date.today().isoformat(), method)
    cur = _cpu_buf.get(key) or [0.0, 0]
    cur[0] += secs
    cur[1] += 1
    _cpu_buf[key] = cur
    now = _t.time()
    if len(_cpu_buf) < 25 and (now - _cpu_flushed["at"]) < 120:
        return
    _cpu_flushed["at"] = now
    items = list(_cpu_buf.items())
    _cpu_buf.clear()
    for (day, m), (secs_, calls) in items:
        try:
            base.db.cpu_add(day, m, secs_, calls)
        except Exception:  # noqa: BLE001 — учёт не должен мешать работе
            pass


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
        try:
            # раз в сутки заодно проверяем базу и подчищаем накопленное:
            # учёт расхода процессора и старые замеры доски «Итоги»
            res = base._integrity_run()
            base.db.cpu_trim()
            base.db.exp_metric_trim()
            base.db.chat_messages_trim()
            # копия наружу: суточная копия лежит на том же диске и от потери
            # сервера не спасает, поэтому раз в сутки увозим базу на Google-диск
            try:
                from . import backup_cloud
                r3 = backup_cloud.run()
                log_error("backup_cloud", r3.get("note") or "выполнено")
                if not r3.get("ok") and not r3.get("skipped"):
                    base.db.add_notification(None, "system", "Копия наружу не ушла",
                                             r3.get("note") or "причина неизвестна",
                                             "errlog", dedup_key=True)
            except Exception as e3:  # noqa: BLE001
                log_error("backup_cloud", "сбой: {}".format(e3))
                base.db.add_notification(None, "system", "Копия наружу не ушла",
                                         str(e3)[:200], "errlog", dedup_key=True)
            if not res.get("ok"):
                base.db.add_notification(None, "system", "База повреждена",
                                         "Проверка целостности не прошла — нужен откат из копии",
                                         "errlog", dedup_key=True)
        except Exception as e:  # noqa: BLE001 — обслуживание не должно ронять автосинк
            log_error("autosync", "проверка базы не выполнилась: {}".format(e))
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
    # кэш выгрузок Директа держим в базе: переживает перезапуск процесса
    try:
        from . import report as _report
        _report.set_cache_store(base.db)
    except Exception:  # noqa: BLE001
        pass

    app = Flask(__name__)
    app.secret_key = _secret_key(base.db)
    # Кука сессии: недоступна из JS, не уходит по HTTP, не отправляется
    # с чужих сайтов. Раньше не было ни одного из трёх ограничений.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=not os.environ.get("IIG_INSECURE_COOKIE"),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,     # тело запроса больше 4 МБ нам не нужно
    )

    @app.after_request
    def _cpu_account(resp):
        """Сколько процессорного времени стоил этот запрос — в разрезе метода."""
        try:
            import time as _t
            t0 = getattr(g, "cpu0", None)
            spent = (_t.process_time() - t0) if t0 is not None else 0.0
            if spent > 0:
                name = request.path
                if name == "/api/" or name.startswith("/api/"):
                    name = name[5:] or "api"
                _cpu_note(base, name[:60], spent)
        except Exception:  # noqa: BLE001 — учёт не должен ломать ответ
            pass
        return resp

    @app.after_request
    def _security_headers(resp):
        """Базовые заголовки: не встраивать в чужой фрейм, не угадывать тип,
        не утекать полным адресом на внешние сайты."""
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        return resp
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
    def _force_https():
        """За обратным прокси Beget оригинальная схема приходит заголовком.
        Кука сессии помечена Secure — по http вход просто не сохранится."""
        if os.environ.get("IIG_INSECURE_COOKIE"):
            return None
        proto = request.headers.get("X-Forwarded-Proto", "")
        if proto and proto != "https":
            return redirect(request.url.replace("http://", "https://", 1), code=301)
        return None

    @app.before_request
    def _load_user():
        import time as _t
        g.cpu0 = _t.process_time()
        if not base.db.get_kv("public_url"):
            # адрес, по которому кабинет виден снаружи: нужен внешней проверке живости
            base.db.set_kv("public_url", request.url_root.rstrip("/"))
        g.user = None
        uid = auth.session_user_id()
        if uid:
            row = base.db.get_user(uid)
            # версия пароля в сессии отстала — значит пароль сменили, вход больше не годен
            pv = (row["pass_version"] if row and "pass_version" in row.keys() else 0) or 0
            if row and row["active"] and int(auth.session_pass_version() or 0) >= int(pv):
                g.user = dict(row)
            elif row:
                auth.logout_session()
        try:
            _periodic_tick(base)   # ленивые фоновые задачи (автосинк/бюджеты)
        except Exception:  # noqa: BLE001 — планировщик не должен ронять запросы
            pass

    @app.route("/")
    def index():
        return Response(ui_html, mimetype="text/html; charset=utf-8")

    @app.route("/healthz")
    def healthz():
        """Публичная проверка живости: по ней внешний монитор понимает, что кабинет отвечает.
        Ничего чувствительного не отдаём — только версия и аптайм."""
        from . import sysinfo
        try:
            base.db.conn.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception:  # noqa: BLE001
            db_ok = False
        code = 200 if db_ok else 503
        return jsonify({"ok": db_ok, "version": sysinfo.version()["short"],
                        "uptime": sysinfo.fmt_dur(__import__("time").time() - sysinfo.STARTED)}), code

    @app.route("/download/runbook.md")
    def download_runbook():
        """Справка для преемника файлом: пути, команды и расписание этой машины."""
        if not g.user or g.user.get("role") != "admin":
            return jsonify({"ok": False, "error": "только администратор"}), 403
        from . import sysinfo
        text = sysinfo.runbook(base.db)
        return Response(text, mimetype="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="iig-reporter-runbook.md"'})

    @app.route("/download/docs.md")
    def download_docs():
        """Вся документация одним файлом — офлайн-копия.

        Смысл: инструкция по подъёму кабинета не должна лежать только внутри кабинета.
        Доступна любому вошедшему: в аварии под рукой может оказаться не админ.
        """
        if not g.user:
            return jsonify({"ok": False, "error": "not_authenticated"}), 401
        import datetime as _dt
        text = Api(user=g.user)._docs_bundle(admin=(g.user.get("role") == "admin"))
        fn = "iig-reporter-docs-%s.md" % _dt.date.today().isoformat()
        return Response(text, mimetype="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="%s"' % fn})

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
            try:
                base.db.log_login(row["id"] if row else None, email, False, ip,
                                  request.headers.get("User-Agent", ""))
            except Exception:  # noqa: BLE001 — журнал входов не должен мешать логину
                pass
            s = _login_fails.setdefault(key, {"n": 0, "until": 0})
            s["n"] += 1
            if s["n"] >= 5:   # 5-я и далее: пауза 15с, 30с, 60с … максимум 5 минут
                s["until"] = _t.time() + min(300, 15 * (2 ** (s["n"] - 5)))
            if len(_login_fails) > 500:   # не растим словарь бесконечно
                for k in [k for k, v in list(_login_fails.items()) if v["until"] < _t.time() - 3600][:200]:
                    _login_fails.pop(k, None)
            return jsonify({"ok": False, "error": "Неверный email или пароль"}), 401
        _login_fails.pop(key, None)   # успешный вход — счётчик сбрасываем
        auth.login_session(row["id"], (row["pass_version"] if "pass_version" in row.keys() else 0))
        try:
            base.db.log_login(row["id"], email, True, ip, request.headers.get("User-Agent", ""))
        except Exception:  # noqa: BLE001
            pass
        from . import sysinfo
        return jsonify({"ok": True, "user": _safe_user(dict(row)),
                        "version": sysinfo.version()["short"]})

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
        # простая защита от запросов с чужих страниц: браузер не даст
        # выставить этот заголовок кросс-доменно без разрешающего CORS
        if request.headers.get("X-IIG") != "1":
            return jsonify({"ok": False, "error": "bad_request"}), 400
        # публичные (до гейта)
        if method == "login":
            return _login()
        if method == "logout":
            auth.logout_session()
            return jsonify({"ok": True})
        if method == "me":
            from . import sysinfo
            return jsonify({"ok": True, "user": _safe_user(g.user),
                            "setup": base.db.count_users() == 0,
                            "version": sysinfo.version()["short"]})

        if method.startswith("_"):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        if not g.user:
            return jsonify({"ok": False, "error": "not_authenticated"}), 401

        api_u = _api_for(g.user)
        fn = getattr(api_u, method, None)
        # Запрещено по умолчанию: наружу выставлены только методы с декоратором @safe.
        # Раньше вызвать можно было ЛЮБОЙ публичный метод объекта, включая служебные.
        if not callable(fn) or not getattr(fn, "_api_exposed", False):
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
