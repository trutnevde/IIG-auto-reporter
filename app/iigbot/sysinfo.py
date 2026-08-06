# -*- coding: utf-8 -*-
"""Состояние системы и обслуживание: версия, аптайм, база, CPU, секреты, откат выката.

Всё это раньше выяснялось только по SSH: жив ли процесс и давно ли перезапускался,
сколько весит база и цела ли она, какой именно код сейчас на сервере, откуда
откатываться, если выкат оказался неудачным. Здесь то же самое собрано так, чтобы
показывалось в кабинете и делалось кнопкой.

Модуль намеренно не знает ни про Flask, ни про права: только факты о машине и файлах.
Кто имеет право это увидеть — решает api.py.
"""
import json
import os
import sqlite3
import sys
import time
import datetime as dt

from .settings import BASE_DIR, FROZEN, PKG_DIR, ERROR_LOG_PATH

# Момент импорта модуля ≈ момент старта процесса: Passenger поднимает приложение целиком.
STARTED = time.time()
_CPU_AT_START = time.process_time()

# Файл версии кладёт выкат: без него о коде на сервере можно судить только по датам файлов.
VERSION_FILE = os.path.join(PKG_DIR, "VERSION")

# Ключи стоит менять хотя бы раз в полгода — дальше подсвечиваем как просроченные.
ROTATE_DAYS = 180


# ─────────────────────────── мелкие помощники ───────────────────────────
def _kb(n):
    return round((n or 0) / 1024)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _dir_size(path, limit=20000):
    """Суммарный вес папки. limit — предохранитель от обхода чего-то огромного."""
    total = n = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            total += _size(os.path.join(root, f))
            n += 1
            if n > limit:
                return total
    return total


def _iso(ts):
    if not ts:
        return None
    return dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _find_up(name, depth=5, start=None):
    """Ищем файл/папку вверх по дереву от папки приложения."""
    p = start or BASE_DIR
    for _ in range(depth):
        cand = os.path.join(p, name)
        if os.path.exists(cand):
            return cand
        nxt = os.path.dirname(p)
        if nxt == p:
            break
        p = nxt
    return None


def fmt_dur(sec):
    """Человеческая длительность: «3 д 4 ч», «12 мин»."""
    sec = int(max(0, sec or 0))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return "{} д {} ч".format(d, h)
    if h:
        return "{} ч {} мин".format(h, m)
    if m:
        return "{} мин".format(m)
    return "{} с".format(s)


# ─────────────────────────── пути на сервере ───────────────────────────
def paths():
    """Где что лежит. Пригождается и на экране состояния, и в README для преемника."""
    from . import gsheets
    try:
        key = gsheets.key_path()
    except Exception:  # noqa: BLE001 — ключа может не быть вовсе
        key = None
    app_root = _find_up("_app")                       # .../public_html/_app на хостинге
    public = os.path.dirname(app_root) if app_root else None
    return {
        "base": BASE_DIR,
        "package": PKG_DIR,
        "public_html": public,
        "restart": os.path.join(public, "tmp", "restart.txt") if public else None,
        "deploy_backup": _find_up("deploy_backup"),
        "backups": os.path.join(BASE_DIR, "backups"),
        "error_log": ERROR_LOG_PATH,
        "sa_key": key,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "frozen": FROZEN,
    }


# ─────────────────────────── версия сборки ───────────────────────────
def version():
    """Что именно сейчас работает. Файл VERSION пишет выкат; без него — по датам файлов."""
    info = {}
    try:
        if os.path.isfile(VERSION_FILE):
            with open(VERSION_FILE, "r", encoding="utf-8") as f:
                info = json.load(f) or {}
    except Exception:  # noqa: BLE001 — версия не обязана читаться
        info = {}
    newest = 0.0
    for f in os.listdir(PKG_DIR):
        if f.endswith((".py", ".html")):
            newest = max(newest, _mtime(os.path.join(PKG_DIR, f)) or 0)
    commit = (info.get("commit") or "")[:7]
    at = info.get("at") or _iso(newest)
    short = "{}{}".format(commit + " · " if commit else "", (at or "")[:16].replace("T", " "))
    return {"commit": commit, "at": at, "note": info.get("note") or "",
            "files_at": _iso(newest), "short": short or "не определена",
            "exact": bool(info)}


# ─────────────────────────── база данных ───────────────────────────
def db_stats(db):
    """Размер, WAL, свободные страницы и сколько чего лежит в основных таблицах."""
    path = getattr(db, "path", None) or ""
    out = {"path": path, "kb": _kb(_size(path)),
           "wal_kb": _kb(_size(path + "-wal")), "shm_kb": _kb(_size(path + "-shm"))}
    try:
        page = db.conn.execute("PRAGMA page_size").fetchone()[0]
        free = db.conn.execute("PRAGMA freelist_count").fetchone()[0]
        out["free_kb"] = _kb(page * free)
    except Exception:  # noqa: BLE001
        out["free_kb"] = None
    counts = []
    for t in ("clients", "chats", "bindings", "send_log", "budgets", "audit",
              "logins", "cache", "notifications"):
        try:
            n = db.conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0]
            counts.append({"table": t, "rows": n})
        except Exception:  # noqa: BLE001 — таблицы может не быть на старой копии
            pass
    out["tables"] = counts
    return out


def integrity(db):
    """Проверка целостности: быстрая, полная и связность ссылок.

    Полная проверка читает базу целиком — на нашем размере это доли секунды,
    но зовём её всё равно из фоновой задачи, чтобы не держать единственный процесс.
    """
    t0 = time.time()
    res = {"at": dt.datetime.now().isoformat(timespec="seconds")}
    try:
        res["quick"] = db.conn.execute("PRAGMA quick_check").fetchone()[0]
    except Exception as e:  # noqa: BLE001
        res["quick"] = "ошибка: {}".format(e)[:200]
    try:
        rows = db.conn.execute("PRAGMA integrity_check").fetchall()
        res["full"] = "; ".join(str(r[0]) for r in rows)[:600]
    except Exception as e:  # noqa: BLE001
        res["full"] = "ошибка: {}".format(e)[:200]
    try:
        bad = db.conn.execute("PRAGMA foreign_key_check").fetchall()
        res["fk"] = len(bad)
    except Exception:  # noqa: BLE001
        res["fk"] = None
    res["ok"] = (res.get("quick") == "ok" and res.get("full") == "ok")
    res["ms"] = int((time.time() - t0) * 1000)
    res["kb"] = _kb(_size(getattr(db, "path", "") or ""))
    return res


# ─────────────────────────── место на диске ───────────────────────────
def storage_stats():
    """Сколько занимаем мы сами. Общий размер диска на shared-хостинге ни о чём не говорит:
    там видна вся машина, а не наша квота, — поэтому считаем свои папки."""
    p = paths()
    backups_dir = p["backups"]
    files = []
    if os.path.isdir(backups_dir):
        for f in sorted(os.listdir(backups_dir), reverse=True):
            if f.startswith("iigbot_") and f.endswith(".sqlite3"):
                fp = os.path.join(backups_dir, f)
                files.append({"file": f, "kb": _kb(_size(fp)), "at": _iso(_mtime(fp))})
    return {
        "app_kb": _kb(_dir_size(p["package"])),
        "backups_kb": sum(x["kb"] for x in files),
        "backups_n": len(files),
        "backups": files[:30],
        "deploy_kb": _kb(_dir_size(p["deploy_backup"])) if p["deploy_backup"] else 0,
        "log_kb": _kb(_size(p["error_log"])),
    }


# ─────────────────────────── процессор ───────────────────────────
def cpu_now():
    """Сколько процессорного времени сжёг текущий процесс с момента старта.

    На виртуальном хостинге лимит считается хостером в своих единицах (CP) и снаружи
    не читается — зато наш собственный расход виден точно, и по нему понятно,
    какие операции дорогие.
    """
    used = time.process_time() - _CPU_AT_START
    up = time.time() - STARTED
    out = {"cpu_sec": round(used, 2), "uptime_sec": int(up),
           "uptime": fmt_dur(up), "started": _iso(STARTED),
           "share": round(100.0 * used / up, 2) if up > 1 else None}
    try:
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF)
        out["rss_mb"] = round(ru.ru_maxrss / 1024.0, 1)     # в Linux ru_maxrss в КБ
    except Exception:  # noqa: BLE001 — под Windows модуля нет
        out["rss_mb"] = None
    return out


# ─────────────────────────── секреты ───────────────────────────
def _mask(val):
    """Показываем, что ключ на месте, но не сам ключ."""
    s = str(val or "")
    if not s:
        return "пусто"
    if len(s) <= 8:
        return "•" * len(s)
    return "{}…{}  ({} симв.)".format(s[:3], s[-4:], len(s))


def secrets_report(db=None):
    """Какие ключи есть, когда файл менялся в последний раз и не пора ли обновить.

    Значения не отдаём никогда — только длину и хвост, чтобы можно было сверить
    с тем, что выдал сервис, не вытаскивая ключ на экран.
    """
    from .settings import load_secrets, _secrets_candidates, _first_existing
    p = paths()
    items = []
    now = time.time()

    def add(name, title, path, keys):
        ts = _mtime(path) if path else None
        marked = None
        if db is not None:
            try:
                raw = db.get_kv("secret_rotated_" + name)
                marked = float(raw) if raw else None
            except Exception:  # noqa: BLE001
                marked = None
        base = marked or ts
        age = int((now - base) / 86400) if base else None
        items.append({"name": name, "title": title, "path": path,
                      "exists": bool(path and os.path.isfile(path)),
                      "changed": _iso(ts), "rotated": _iso(marked) if marked else None,
                      "age_days": age, "stale": bool(age is not None and age > ROTATE_DAYS),
                      "keys": keys})

    sec_path = _first_existing(*_secrets_candidates())
    keys = []
    try:
        data = load_secrets() or {}
        titles = {"yandex_oauth_token": "Токен Яндекс.Директа и Метрики",
                  "telegram_bot_token": "Токен Telegram-бота",
                  "yandex_client_id": "ID приложения Яндекса",
                  "yandex_client_secret": "Секрет приложения Яндекса"}
        for k, v in sorted(data.items()):
            if isinstance(v, (dict, list)):
                continue
            keys.append({"key": k, "title": titles.get(k, k), "value": _mask(v)})
    except Exception as e:  # noqa: BLE001
        keys.append({"key": "—", "title": "не прочитать", "value": str(e)[:120]})
    add("secrets", "secrets.json — токены Яндекса и бота", sec_path, keys)

    sa = p.get("sa_key")
    sa_keys = []
    if sa and os.path.isfile(sa):
        try:
            with open(sa, "r", encoding="utf-8") as f:
                d = json.load(f)
            sa_keys = [{"key": "client_email", "title": "Сервисный аккаунт",
                        "value": d.get("client_email") or "?"},
                       {"key": "private_key_id", "title": "Идентификатор ключа",
                        "value": _mask(d.get("private_key_id"))},
                       {"key": "project_id", "title": "Проект Google",
                        "value": d.get("project_id") or "?"}]
        except Exception as e:  # noqa: BLE001
            sa_keys = [{"key": "—", "title": "не прочитать", "value": str(e)[:120]}]
    add("sa_key", "Ключ сервисного аккаунта Google", sa, sa_keys)

    wh = None
    if db is not None:
        try:
            wh = db.get_kv("tg_webhook_secret")
        except Exception:  # noqa: BLE001
            wh = None
    items.append({"name": "webhook", "title": "Секрет вебхука Telegram", "path": "база, ключ tg_webhook_secret",
                  "exists": bool(wh), "changed": None, "rotated": None, "age_days": None,
                  "stale": False,
                  "keys": [{"key": "tg_webhook_secret", "title": "Заголовок X-Telegram-Bot-Api-Secret-Token",
                            "value": _mask(wh)}]})
    return {"items": items, "rotate_days": ROTATE_DAYS}


# ─────────────────────────── откат выката ───────────────────────────
def deploy_backups():
    """Копии файлов приложения, которые кладёт выкат перед заливкой новых."""
    d = paths()["deploy_backup"]
    if not d or not os.path.isdir(d):
        return {"dir": d, "items": []}
    out = []
    for name in sorted(os.listdir(d), reverse=True)[:20]:
        sub = os.path.join(d, name)
        if not os.path.isdir(sub):
            continue
        files = []
        for f in sorted(os.listdir(sub)):
            fp = os.path.join(sub, f)
            if os.path.isfile(fp):
                files.append({"file": f, "kb": _kb(_size(fp))})
        if files:
            out.append({"stamp": name, "at": _iso(_mtime(sub)), "files": files,
                        "kb": sum(f["kb"] for f in files)})
    return {"dir": d, "items": out}


def rollback(stamp, safety_dir_name=None):
    """Вернуть файлы приложения из выбранной копии. Текущие сначала складываем рядом,
    чтобы откат можно было откатить."""
    d = paths()["deploy_backup"]
    if not d:
        raise RuntimeError("папка deploy_backup не найдена — откатывать нечего")
    src = os.path.join(d, os.path.basename(stamp))
    if not os.path.isdir(src):
        raise RuntimeError("копия {} не найдена".format(stamp))
    import shutil
    safety = os.path.join(d, safety_dir_name or
                          ("before_rollback_" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")))
    os.makedirs(safety, exist_ok=True)
    moved = []
    for f in sorted(os.listdir(src)):
        sp = os.path.join(src, f)
        if not os.path.isfile(sp):
            continue
        dp = os.path.join(PKG_DIR, f)
        if os.path.isfile(dp):
            shutil.copy2(dp, os.path.join(safety, f))
        shutil.copy2(sp, dp)
        moved.append(f)
    return {"stamp": os.path.basename(src), "files": moved, "safety": os.path.basename(safety)}


def touch_restart():
    """Попросить Passenger перезапустить приложение (он следит за tmp/restart.txt)."""
    p = paths()["restart"]
    if not p:
        return {"ok": False, "error": "файл перезапуска не найден (не хостинг?)"}
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("restart {}\n".format(int(time.time())))
        return {"ok": True, "path": p}
    except OSError as e:
        return {"ok": False, "error": str(e)[:200]}


# ─────────────────────────── восстановление из бэкапа ───────────────────────────
def restore(db, path):
    """Заменить содержимое живой базы содержимым файла бэкапа.

    Через backup API, а не копированием файла: соединение приложения остаётся тем же,
    поэтому не нужно ни перезапускать процесс, ни ловить момент, когда база свободна.
    """
    if not os.path.isfile(path):
        raise RuntimeError("файл бэкапа не найден")
    src = sqlite3.connect(path)
    try:
        src.backup(db.conn)
    finally:
        src.close()
    db.conn.commit()
    return {"restored": os.path.basename(path), "kb": _kb(_size(path))}


# ─────────────────────────── проверка живости снаружи ───────────────────────────
def watch_once(db, tg=None, url=None, fails_to_alert=2):
    """Дёрнуть /healthz снаружи и написать в Telegram, если кабинет молчит.

    Изнутри приложения этого не сделать: если процесс не поднимается, проверять некому.
    Поэтому зовётся из cron хостинга (`python -m iigbot watch`) — отдельным процессом,
    который живёт независимо от веб-приложения.
    """
    from . import net
    base = (url or db.get_kv("public_url") or "https://reports.iig.ru").rstrip("/")
    target = base + "/healthz"
    prev = {}
    try:
        raw = db.get_kv("watch_last")
        prev = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        prev = {}

    t0 = time.time()
    ok, status, err = False, None, None
    try:
        r = net.get("Кабинет", target, retries=1, timeout=25)
        status = r.status_code
        ok = status == 200
        if not ok:
            err = "HTTP {}".format(status)
    except Exception as e:  # noqa: BLE001 — недоступность и есть результат проверки
        err = str(e)[:200]
    ms = int((time.time() - t0) * 1000)

    fails = 0 if ok else int(prev.get("fails") or 0) + 1
    state = {"at": dt.datetime.now().isoformat(timespec="seconds"), "ok": ok, "ms": ms,
             "status": status, "error": err, "fails": fails, "url": target,
             "last_ok": (dt.datetime.now().isoformat(timespec="seconds") if ok
                         else prev.get("last_ok"))}

    sent = None
    was_down = int(prev.get("fails") or 0) >= fails_to_alert
    if tg is not None:
        if not ok and fails == fails_to_alert:
            # в тревоге нужна суть, а не питоновский стектрейс на пол-экрана
            why = ("нет связи с сервером" if (err or "").startswith("Кабинет: нет связи")
                   else (err or "нет ответа")[:120])
            sent = _watch_notify(db, tg, "Кабинет не отвечает\n{}\n{}".format(target, why))
        elif ok and was_down:
            sent = _watch_notify(db, tg, "Кабинет снова отвечает: {}\nОтклик {} мс".format(target, ms))
    state["notified"] = sent
    db.set_kv("watch_last", json.dumps(state, ensure_ascii=False))
    return state


def _watch_notify(db, tg, text):
    """Тревога уходит администраторам, у кого привязан личный чат с ботом."""
    from . import budgets as B
    by_id, by_un, by_title = B._priv_index(db)
    n = 0
    for u in db.list_users():
        if (u["role"] or "") != "admin" or not u["active"]:
            continue
        chat = B._resolve_chat(u, by_id, by_un, by_title)
        if not chat:
            continue
        try:
            tg.send_message(chat["chat_id"], text)
            n += 1
        except Exception:  # noqa: BLE001 — один недоступный чат не мешает остальным
            pass
    return n


# ─────────────────────────── README для преемника ───────────────────────────
def runbook(db=None, extra=None):
    """Живая инструкция: пути, команды, расписание — с подставленными значениями
    этой машины, а не с примерами из головы."""
    p = paths()
    v = version()
    lines = []
    a = lines.append
    a("# IIG Reporter — как это устроено и как этим управлять")
    a("")
    a("Собрано автоматически {}. Версия кода: {}."
      .format(dt.datetime.now().strftime("%d.%m.%Y %H:%M"), v["short"]))
    a("")
    a("## Что это")
    a("")
    a("Кабинет агентства: тянет статистику из Яндекс.Директа и Метрики, собирает недельные")
    a("отчёты и отправляет их клиентам в Telegram, ведёт бюджеты, Google-таблицы и контроль сдачи.")
    a("Пользователи заходят по логину и паролю, каждый видит своих клиентов, администратор — всех.")
    a("")
    a("## Где что лежит")
    a("")
    a("| Что | Путь |")
    a("| --- | --- |")
    for title, key in (("Папка приложения", "package"), ("Рабочая папка (база, логи)", "base"),
                       ("Корень сайта", "public_html"), ("Файл перезапуска", "restart"),
                       ("Бэкапы базы", "backups"), ("Копии файлов выката", "deploy_backup"),
                       ("Журнал ошибок", "error_log"), ("Ключ Google", "sa_key"),
                       ("Python", "python")):
        a("| {} | `{}` |".format(title, p.get(key) or "—"))
    a("")
    a("Секреты (`secrets.json`, ключ сервисного аккаунта) лежат вне публичной папки и **не хранятся")
    a("в репозитории**. При переносе на другую машину их нужно перенести отдельно, по SFTP.")
    a("")
    a("## Как запускать и перезапускать")
    a("")
    a("Приложение поднимает Passenger через `passenger_wsgi.py`, отдельного демона нет.")
    a("Перезапуск — записать что угодно в файл перезапуска:")
    a("")
    a("```")
    a("echo restart > {}".format(p.get("restart") or "<корень сайта>/tmp/restart.txt"))
    a("```")
    a("")
    a("В кабинете то же самое делает кнопка «Перезапустить приложение» в разделе «Система».")
    a("")
    a("## Расписание (cron хостинга)")
    a("")
    a("```")
    a("# суточное обслуживание: клиенты, цели, бюджеты, проверка целостности базы")
    a("0 5 * * *  {} -m iigbot autosync".format(p.get("python") or "python3"))
    a("# недельная рассылка отчётов клиентам")
    a("0 10 * * 1 {} -m iigbot weekly".format(p.get("python") or "python3"))
    a("# проверка живости снаружи каждые 5 минут (пишет в Telegram, если сайт не отвечает)")
    a("*/5 * * * * {} -m iigbot watch".format(p.get("python") or "python3"))
    a("```")
    a("")
    a("День и время недельной рассылки берутся из «Настроек» кабинета; cron только запускает проверку.")
    a("")
    a("## Бот")
    a("")
    a("Бот работает **через вебхук** (`/tg/webhook`), а не long-polling. Токен один на агентство,")
    a("поэтому одновременно с вебхуком нельзя запускать десктопный слушатель — Telegram отдаст 409.")
    a("Установка вебхука: `python -m iigbot webhook set https://reports.iig.ru/tg/webhook`.")
    a("")
    a("## Выкат новой версии")
    a("")
    a("1. Скопировать изменённые `.py`/`ui.html` в `{}`.".format(p.get("package") or "папку iigbot"))
    a("2. Проверить синтаксис прямо на сервере: `python3 -c \"import ast;ast.parse(open('iigbot/api.py',encoding='utf-8').read())\"`.")
    a("3. Записать файл перезапуска.")
    a("4. Открыть сайт и убедиться, что он отвечает.")
    a("")
    a("Перед заливкой прежние файлы складываются в `{}` —".format(p.get("deploy_backup") or "deploy_backup"))
    a("оттуда же работает кнопка «Откатить выкат» в разделе «Система».")
    a("")
    a("## Бэкапы")
    a("")
    a("База копируется через SQLite backup API (корректно при включённом WAL), хранится 14 последних")
    a("копий в `{}`. Кнопки в разделе «Система»: сделать копию, скачать,".format(p.get("backups") or ""))
    a("отправить в Telegram (внешнее хранилище) и восстановиться из копии.")
    a("")
    a("## Если что-то сломалось")
    a("")
    a("1. Раздел «Система» → «Состояние»: жив ли процесс, когда перезапускался, цела ли база.")
    a("2. Раздел «Журнал ошибок»: последние сбои с временем и местом.")
    a("3. Кнопка «Проверить целостность» — если база повреждена, восстановиться из копии.")
    a("4. Если сломал выкат — кнопка «Откатить выкат» вернёт прежние файлы.")
    a("5. Совсем ничего не отвечает — по SSH записать файл перезапуска (см. выше).")
    if db is not None:
        try:
            a("")
            a("## Сейчас в базе")
            a("")
            st = db_stats(db)
            a("Файл базы: {} КБ.".format(st["kb"]))
            for t in st["tables"]:
                a("* {} — {}".format(t["table"], t["rows"]))
        except Exception:  # noqa: BLE001 — справка не обязана падать из-за статистики
            pass
    if extra:
        a("")
        a(extra)
    a("")
    return "\n".join(lines)
