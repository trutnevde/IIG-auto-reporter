# -*- coding: utf-8 -*-
"""Локальное хранилище (SQLite): чаты, клиенты, привязки, лог отправок.

Одна база на ПК. И бот, и веб-админка работают с ней одновременно (включён WAL).
"""
import os
import json
import sqlite3
import threading
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _SharedConn(sqlite3.Connection):
    """Соединение, переживающее работу из нескольких потоков.

    Соединение одно на всё приложение (check_same_thread=False), а пишут в него и обработчик
    запроса, и фоновые задачи: сбор бюджетов, автосинк, бэкап. Они шли вперемешку, и коммит
    одного потока закрывал транзакцию другого — второй падал с «cannot commit - no transaction
    is active». В журнале это роняло sync_clients и весь автосинк следом.

    Одного замка на коммит мало: одновременный execute на общем соединении даёт
    «bad parameter or other API misuse». Поэтому под замок берём и запросы, и коммит —
    доступ к базе становится последовательным. Нагрузка тут крошечная (десятки запросов
    в минуту), так что очередь ничего не замедляет, а гонки исчезают.

    Плюс терпимость к уже закрытой транзакции: если её успел закрыть чужой коммит,
    данные уже на диске и терять нечего.
    """

    _lock = threading.RLock()

    def execute(self, *a, **kw):
        with self._lock:
            return super().execute(*a, **kw)

    def executemany(self, *a, **kw):
        with self._lock:
            return super().executemany(*a, **kw)

    def executescript(self, *a, **kw):
        with self._lock:
            return super().executescript(*a, **kw)

    def commit(self):
        with self._lock:
            try:
                super().commit()
            except sqlite3.OperationalError as e:
                if "no transaction is active" not in str(e).lower():
                    raise


class Storage:
    def __init__(self, path):
        self.path = path
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        # check_same_thread=False — чтобы Flask мог читать из разных потоков.
        self.conn = sqlite3.connect(path, check_same_thread=False, factory=_SharedConn)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS chats (
                chat_id    INTEGER PRIMARY KEY,
                type       TEXT,
                title      TEXT,
                username   TEXT,
                status     TEXT,   -- active | removed
                my_status  TEXT,   -- member | administrator | left | kicked ...
                added_at   TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS clients (
                login       TEXT PRIMARY KEY,
                name        TEXT,
                goals       TEXT,   -- json: [{"id","name"}]
                attribution TEXT,
                source      TEXT,   -- yandex | config | manual
                updated_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS bindings (
                chat_id   INTEGER PRIMARY KEY,   -- один клиент на чат
                login     TEXT NOT NULL,
                confirmed INTEGER DEFAULT 1,
                bound_by  INTEGER,
                bound_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS send_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                login       TEXT,
                chat_id     INTEGER,
                period_from TEXT,
                period_to   TEXT,
                status      TEXT,
                error       TEXT,
                sent_at     TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                email      TEXT NOT NULL UNIQUE,
                pass_hash  TEXT NOT NULL,
                name       TEXT,
                role       TEXT NOT NULL DEFAULT 'user',   -- admin | observer | user
                active     INTEGER NOT NULL DEFAULT 1,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                to_user    INTEGER,          -- NULL = всем специалистам (рассылка-объявление)
                from_user  INTEGER,
                text       TEXT NOT NULL,
                kind       TEXT DEFAULT 'info',   -- info | warn | urgent
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS note_ack (
                note_id  INTEGER,
                user_id  INTEGER,
                ack_at   TEXT,
                PRIMARY KEY (note_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS excuses (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                login      TEXT NOT NULL,
                week       TEXT,          -- ISO понедельник недели; NULL = бессрочно (проект отвалился)
                kind       TEXT,          -- churned | nospend | other
                reason     TEXT,
                by_user    INTEGER,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS budgets (
                login             TEXT PRIMARY KEY,   -- клиент из рабочего пула
                name              TEXT,
                balance           REAL,               -- остаток общего счёта; NULL = недоступен
                currency          TEXT,
                cost7             REAL,               -- расход за 7 дней
                cost21            REAL,               -- расход за 21 день (фильтр активности)
                rate              REAL,               -- темп, руб/день (cost7/7)
                days_left         REAL,               -- balance/rate; NULL = не посчитать
                camps_total       INTEGER,
                camps_on          INTEGER,
                camps_pay_stopped INTEGER,            -- остановлены по оплате
                daily_budget      REAL,               -- суммарный дневной бюджет включённых
                status            TEXT,               -- ok|warning|critical|inactive|error
                note              TEXT,
                updated_at        TEXT
            );
            """
        )
        self.conn.commit()
        self._migrate()
        self._ensure_indexes()

    # Индексов не было ни одного: каждая выборка перебирала таблицу целиком.
    # Дёшево создаются, безопасно пересоздаются, ускоряют всё разом.
    _INDEXES = [
        ("ix_clients_owner",      "clients(owner)"),
        ("ix_clients_delivery",   "clients(delivery)"),
        ("ix_bindings_login",     "bindings(login)"),
        ("ix_bindings_chat",      "bindings(chat_id)"),
        ("ix_sendlog_login_date", "send_log(login, sent_at)"),
        ("ix_sendlog_status",     "send_log(status)"),
        ("ix_activity_chat",      "chat_activity(chat_id)"),
        ("ix_notif_kind",         "notifications(kind)"),
        ("ix_notifread_user",     "notif_read(user_id)"),
        ("ix_noteack_note",       "note_ack(note_id)"),
        ("ix_notereply_note",     "note_reply(note_id)"),
        ("ix_excuses_week",       "excuses(week)"),
        ("ix_budgets_login",      "budgets(login)"),
        ("ix_audit_created",      "audit(created_at)"),
        ("ix_logins_user",        "logins(user_id, at)"),
        ("ix_cache_at",           "cache(at)"),
        ("ix_cpu_day",            "cpu_usage(day)"),
        ("ix_presets_owner",      "presets(owner)"),
        ("ix_presetruns_login",   "preset_runs(login, at)"),
    ]

    def _ensure_indexes(self):
        """Создать недостающие индексы. Ошибки по отдельному индексу не валят запуск:
        схема на разных копиях базы могла разойтись."""
        for name, target in self._INDEXES:
            try:
                self.conn.execute("CREATE INDEX IF NOT EXISTS {} ON {}".format(name, target))
            except Exception:  # noqa: BLE001
                pass
        self.conn.commit()

    def log_login(self, user_id, email, ok, ip, agent):
        """Кто и когда заходил — и чьи попытки не прошли."""
        self.conn.execute(
            "INSERT INTO logins(user_id,email,ok,ip,agent,at) VALUES(?,?,?,?,?,?)",
            (user_id, email, 1 if ok else 0, (ip or "")[:64], (agent or "")[:200], _now()))
        self.conn.commit()

    def logins_recent(self, limit=50, user_id=None):
        if user_id:
            cur = self.conn.execute(
                "SELECT * FROM logins WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
        else:
            cur = self.conn.execute("SELECT * FROM logins ORDER BY id DESC LIMIT ?", (limit,))
        return cur.fetchall()

    def last_login(self, user_id):
        r = self.conn.execute(
            "SELECT at FROM logins WHERE user_id=? AND ok=1 ORDER BY id DESC LIMIT 1",
            (user_id,)).fetchone()
        return r["at"] if r else None

    # ─────────── кэш выгрузок ───────────
    # Раньше кэш жил только в памяти процесса: Passenger перезапустил приложение —
    # и всё, что было накоплено, пропадало. Теперь переживает перезапуск.
    CACHE_MAX_ROWS = 200
    CACHE_MAX_BYTES = 8 * 1024 * 1024      # 8 МБ на весь кэш, дальше вытесняем старое

    def cache_get(self, key, max_age):
        import time as _t
        r = self.conn.execute("SELECT value, at FROM cache WHERE key=?", (key,)).fetchone()
        if not r:
            return None
        if _t.time() - (r["at"] or 0) > max_age:
            self.conn.execute("DELETE FROM cache WHERE key=?", (key,))
            self.conn.commit()
            return None
        return r["value"]

    def cache_put(self, key, value):
        import time as _t
        val = value or ""
        self.conn.execute(
            "INSERT INTO cache(key,value,at,bytes) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, at=excluded.at, bytes=excluded.bytes",
            (key, val, _t.time(), len(val)))
        self.conn.commit()
        self._cache_trim()

    def _cache_trim(self):
        """Держим кэш в берегах: и по числу записей, и по объёму."""
        row = self.conn.execute("SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b FROM cache").fetchone()
        if row["n"] <= self.CACHE_MAX_ROWS and row["b"] <= self.CACHE_MAX_BYTES:
            return
        self.conn.execute(
            "DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY at ASC LIMIT ?)",
            (max(1, row["n"] // 4),))
        self.conn.commit()

    def cache_clear(self):
        self.conn.execute("DELETE FROM cache")
        self.conn.commit()

    def cache_stats(self):
        r = self.conn.execute("SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b FROM cache").fetchone()
        return {"rows": r["n"], "kb": round((r["b"] or 0) / 1024)}

    # ─────────── фоновые задачи ───────────
    def job_set(self, key, **fields):
        import time as _t
        cur = self.job_get(key) or {}
        data = {"key": key, "title": cur.get("title"), "note": cur.get("note"),
                "state": cur.get("state"), "result": cur.get("result"),
                "error": cur.get("error"), "started": cur.get("started"),
                "finished": cur.get("finished"), "owner": cur.get("owner")}
        data.update(fields)
        if fields.get("state") == "running" and not data.get("started"):
            data["started"] = _t.time()
        if fields.get("state") in ("done", "error"):
            data["finished"] = _t.time()
        self.conn.execute(
            "INSERT INTO jobs(key,title,note,state,result,error,started,finished,owner) "
            "VALUES(:key,:title,:note,:state,:result,:error,:started,:finished,:owner) "
            "ON CONFLICT(key) DO UPDATE SET title=excluded.title, note=excluded.note, "
            "state=excluded.state, result=excluded.result, error=excluded.error, "
            "started=excluded.started, finished=excluded.finished, owner=excluded.owner", data)
        self.conn.commit()
        return data

    def job_get(self, key):
        r = self.conn.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()
        return dict(r) if r else None

    def jobs_running(self):
        rows = self.conn.execute("SELECT * FROM jobs WHERE state='running'").fetchall()
        return [dict(r) for r in rows]

    def jobs_recent(self, limit=10):
        rows = self.conn.execute(
            "SELECT * FROM jobs ORDER BY COALESCE(finished, started) DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ─────────── расход процессора ───────────
    # На виртуальном хостинге процессорное время — расходуемый ресурс, и когда его
    # не хватает, хостер просто гасит процесс. Считаем свой расход сами: по дням и
    # по методам — иначе непонятно, какая операция дорогая.
    def cpu_add(self, day, method, secs, calls=1):
        self.conn.execute(
            "INSERT INTO cpu_usage(day,method,secs,calls) VALUES(?,?,?,?) "
            "ON CONFLICT(day,method) DO UPDATE SET secs=secs+excluded.secs, calls=calls+excluded.calls",
            (day, method, float(secs), int(calls)))
        self.conn.commit()

    def cpu_days(self, limit=14):
        rows = self.conn.execute(
            "SELECT day, ROUND(SUM(secs),2) secs, SUM(calls) calls FROM cpu_usage "
            "GROUP BY day ORDER BY day DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def cpu_top(self, day=None, limit=12):
        if day:
            rows = self.conn.execute(
                "SELECT method, ROUND(SUM(secs),2) secs, SUM(calls) calls FROM cpu_usage "
                "WHERE day=? GROUP BY method ORDER BY secs DESC LIMIT ?", (day, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT method, ROUND(SUM(secs),2) secs, SUM(calls) calls FROM cpu_usage "
                "GROUP BY method ORDER BY secs DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def cpu_trim(self, keep_days=30):
        """Держим только последний месяц: это диагностика, а не архив."""
        import datetime as _d
        edge = (_d.date.today() - _d.timedelta(days=keep_days)).isoformat()
        self.conn.execute("DELETE FROM cpu_usage WHERE day < ?", (edge,))
        self.conn.commit()

    # ─────────── шаблоны кампаний ───────────
    # Шаблон — это наш стандарт настройки кампании. Общий (owner=NULL) виден всем,
    # личный — только автору и администратору: так же, как устроены клиенты.
    def preset_list(self, owner=None, all_visible=False):
        if all_visible or owner is None:
            rows = self.conn.execute("SELECT * FROM presets ORDER BY name COLLATE NOCASE").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM presets WHERE owner IS NULL OR owner=? ORDER BY name COLLATE NOCASE",
                (owner,)).fetchall()
        return [dict(r) for r in rows]

    def preset_get(self, preset_id):
        r = self.conn.execute("SELECT * FROM presets WHERE id=?", (preset_id,)).fetchone()
        return dict(r) if r else None

    def preset_save(self, preset_id, name, note, data, owner=None):
        if preset_id:
            self.conn.execute(
                "UPDATE presets SET name=?, note=?, data=?, updated_at=? WHERE id=?",
                (name, note, data, _now(), preset_id))
            self.conn.commit()
            return preset_id
        cur = self.conn.execute(
            "INSERT INTO presets(name,note,data,owner,created_at,updated_at,used_count) "
            "VALUES(?,?,?,?,?,?,0)", (name, note, data, owner, _now(), _now()))
        self.conn.commit()
        return cur.lastrowid

    def preset_delete(self, preset_id):
        self.conn.execute("DELETE FROM presets WHERE id=?", (preset_id,))
        self.conn.commit()

    def preset_used(self, preset_id):
        self.conn.execute(
            "UPDATE presets SET used_count=COALESCE(used_count,0)+1, last_used=? WHERE id=?",
            (_now(), preset_id))
        self.conn.commit()

    def preset_run_log(self, preset_id, preset_name, login, campaign_id, campaign,
                       by_user, ok, error=None):
        """Что и когда создали по шаблону: без этого нельзя ни проверить, ни откатить."""
        self.conn.execute(
            "INSERT INTO preset_runs(preset_id,preset_name,login,campaign_id,campaign,"
            "by_user,at,ok,error) VALUES(?,?,?,?,?,?,?,?,?)",
            (preset_id, preset_name, login, campaign_id, campaign, by_user, _now(),
             1 if ok else 0, error))
        self.conn.commit()

    def preset_runs(self, limit=50, logins=None):
        if logins is None:
            rows = self.conn.execute(
                "SELECT * FROM preset_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            if not logins:
                return []
            marks = ",".join("?" * len(logins))
            rows = self.conn.execute(
                "SELECT * FROM preset_runs WHERE login IN ({}) ORDER BY id DESC LIMIT ?".format(marks),
                tuple(logins) + (limit,)).fetchall()
        return [dict(r) for r in rows]

    def maintenance(self):
        """Обслуживание базы: пересчёт статистики и сжатие. Зовётся по кнопке из кабинета —
        VACUUM переписывает файл целиком, в фоне при каждом старте это лишнее."""
        import os
        before = 0
        try:
            before = os.path.getsize(self.path) if getattr(self, "path", None) else 0
        except OSError:
            pass
        self.conn.execute("ANALYZE")
        self.conn.commit()
        self.conn.execute("VACUUM")
        after = 0
        try:
            after = os.path.getsize(self.path) if getattr(self, "path", None) else 0
        except OSError:
            pass
        return {"before_kb": round(before / 1024), "after_kb": round(after / 1024),
                "saved_kb": round(max(0, before - after) / 1024)}

    def _migrate(self):
        """Безопасные миграции для уже существующих баз (только добавления)."""
        ucols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        if "pass_version" not in ucols:   # смена пароля обнуляет прежние сессии
            self.conn.execute("ALTER TABLE users ADD COLUMN pass_version INTEGER DEFAULT 0")
            self.conn.commit()
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(clients)")}
        if "owner" not in cols:   # владелец клиента (кому назначен); NULL = общий пул
            self.conn.execute("ALTER TABLE clients ADD COLUMN owner INTEGER")
            self.conn.commit()
        if "delivery" not in cols:   # способ доставки: NULL/'telegram'=бот, 'external'=копипаст (сторонний)
            self.conn.execute("ALTER TABLE clients ADD COLUMN delivery TEXT")
            self.conn.commit()
        if "added_at" not in cols:   # когда клиент впервые появился (для сортировки «по дате»)
            self.conn.execute("ALTER TABLE clients ADD COLUMN added_at TEXT")
            # бэкфилл существующих: лучшая доступная оценка — updated_at
            self.conn.execute("UPDATE clients SET added_at=updated_at WHERE added_at IS NULL")
            self.conn.commit()
        acols = {r["name"] for r in self.conn.execute("PRAGMA table_info(chat_activity)")}
        if acols and "wait_off_at" not in acols:   # снят с ожидания ответа: на каком сообщении клиента
            self.conn.execute("ALTER TABLE chat_activity ADD COLUMN wait_off_at TEXT")
            self.conn.execute("ALTER TABLE chat_activity ADD COLUMN wait_off_by INTEGER")
            self.conn.commit()
        ucols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        if "note" not in ucols:   # своя приписка к отчётам: NULL=общая (из Настроек), ''=без, текст=своя
            self.conn.execute("ALTER TABLE users ADD COLUMN note TEXT")
            self.conn.commit()
        if "alert_username" not in ucols:   # свой чат/username для бюджет-алертов (NULL=общий по умолчанию)
            self.conn.execute("ALTER TABLE users ADD COLUMN alert_username TEXT")
            self.conn.commit()
        if "alert_chat_id" not in ucols:   # привязанный по deep-link chat_id для алертов (надёжно, без @username)
            self.conn.execute("ALTER TABLE users ADD COLUMN alert_chat_id INTEGER")
            self.conn.commit()
        if "alert_scope" not in ucols:   # 'mine' (по умолчанию) | 'all' — получать алерты по всему агентству
            self.conn.execute("ALTER TABLE users ADD COLUMN alert_scope TEXT")
            self.conn.commit()
        if "sheet_id" not in cols:
            # Ссылка на Google-таблицу клиента. Раньше таблицу искали только по названию
            # «Auto-Reporter ОТЧЕТ <домен>», и клиент с иначе названной таблицей в выгрузку
            # не попадал — заставлять переименовывать чужие таблицы неправильно.
            self.conn.execute("ALTER TABLE clients ADD COLUMN sheet_id TEXT")
            self.conn.commit()
        if "note" not in cols:   # заметка по проекту («на паузе до августа») — видна всем причастным
            self.conn.execute("ALTER TABLE clients ADD COLUMN note TEXT")
            self.conn.commit()
        # активность в чатах клиентов: кто последний писал — клиент, мы или бот-отчёт.
        # Нужно, чтобы видеть «клиент спросил, а мы не ответили N часов».
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS chat_activity (
                chat_id        INTEGER PRIMARY KEY,
                last_client_at   TEXT,   -- когда последний раз писал КЛИЕНТ
                last_client_name TEXT,
                last_client_text TEXT,
                last_our_at      TEXT,   -- когда последний раз отвечал НАШ сотрудник
                last_our_name    TEXT,
                last_bot_at      TEXT,   -- когда бот кидал отчёт (это НЕ ответ на вопрос)
                msgs_client      INTEGER DEFAULT 0,
                msgs_our         INTEGER DEFAULT 0,
                updated_at       TEXT,
                wait_off_at      TEXT,   -- снят с «ждут ответа» на этом сообщении клиента
                wait_off_by      INTEGER -- кто снял
            )""")
        self.conn.commit()
        # уведомления кабинета (колокольчик): to_user=NULL — всем
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS notifications (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                to_user    INTEGER,          -- NULL = всем пользователям
                kind       TEXT,             -- release | client | chat | budget | message | debt | system
                title      TEXT NOT NULL,
                text       TEXT,
                link       TEXT,             -- раздел кабинета для перехода (view)
                created_at TEXT
            )""")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS notif_read (
                notif_id INTEGER, user_id INTEGER, read_at TEXT,
                PRIMARY KEY (notif_id, user_id)
            )""")
        self.conn.commit()
        # аудит действий: кто что сделал (привязки, назначения, долги, сотрудники)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS logins (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER,
                email    TEXT,
                ok       INTEGER,     -- 1 удачный вход, 0 неверный пароль
                ip       TEXT,
                agent    TEXT,
                at       TEXT
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key   TEXT PRIMARY KEY,
                value TEXT,
                at    REAL,
                bytes INTEGER
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                key      TEXT PRIMARY KEY,
                title    TEXT,
                note     TEXT,
                state    TEXT,      -- running | done | error
                result   TEXT,
                error    TEXT,
                started  REAL,
                finished REAL,
                owner    INTEGER    -- кто запустил
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS cpu_usage (
                day    TEXT,        -- YYYY-MM-DD
                method TEXT,        -- метод api или маршрут
                secs   REAL,        -- процессорное время, накопленное за день
                calls  INTEGER,
                PRIMARY KEY (day, method)
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS presets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                note       TEXT,
                data       TEXT NOT NULL,   -- сам шаблон, JSON
                owner      INTEGER,         -- NULL = общий шаблон агентства
                created_at TEXT,
                updated_at TEXT,
                used_count INTEGER DEFAULT 0,
                last_used  TEXT
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS preset_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                preset_id   INTEGER,
                preset_name TEXT,
                login       TEXT,            -- на каком аккаунте создали
                campaign_id INTEGER,
                campaign    TEXT,
                by_user     INTEGER,
                at          TEXT,
                ok          INTEGER,
                error       TEXT
            )""")
        # ─── Экспериментальное: живые фичи на испытании ───
        # Не список идей, а инкубатор: внутри раздела работает настоящий функционал,
        # люди им пользуются, и по следам использования он выпускается или закрывается.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS exp_runs (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                key     TEXT NOT NULL,          -- машинное имя фичи
                user_id INTEGER,
                at      TEXT,
                ms      INTEGER,                -- сколько считалось
                found   INTEGER,                -- сколько нашла (мера полезности прогона)
                ok      INTEGER DEFAULT 1,
                error   TEXT
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS exp_votes (
                key     TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                param   TEXT NOT NULL,          -- useful | clear | keep
                score   INTEGER NOT NULL,       -- 1..5
                at      TEXT,
                PRIMARY KEY (key, user_id, param)
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS exp_notes (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                key     TEXT NOT NULL,
                user_id INTEGER,
                text    TEXT NOT NULL,
                at      TEXT
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS exp_state (
                key    TEXT PRIMARY KEY,
                status TEXT DEFAULT 'testing',  -- testing | released | rejected
                at     TEXT
            )""")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sheet_cols (
                login    TEXT NOT NULL,       -- клиент
                title    TEXT NOT NULL,       -- заголовок столбца как он написан в шапке листа
                goal_ids TEXT NOT NULL,       -- id целей через запятую; пусто = не заполнять
                at       TEXT,
                PRIMARY KEY (login, title)
            )""")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS audit (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                user_name  TEXT,
                action     TEXT,      -- bind | assign | reassign | excuse | delivery | user | note …
                target     TEXT,      -- клиент/чат/пользователь
                detail     TEXT,
                created_at TEXT
            )""")
        self.conn.commit()
        # ответы специалистов на сообщения наблюдателя
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS note_reply (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id    INTEGER,
                user_id    INTEGER,
                text       TEXT NOT NULL,
                created_at TEXT
            )""")
        self.conn.commit()

    # ---------- kv ----------
    def get_kv(self, key, default=None):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key, value):
        self.conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    # ---------- chats ----------
    def upsert_chat(self, chat, my_status, status):
        cid = chat["id"]
        row = self.conn.execute("SELECT added_at FROM chats WHERE chat_id=?", (cid,)).fetchone()
        added_at = row["added_at"] if row and row["added_at"] else _now()
        title = chat.get("title") or chat.get("username") or chat.get("first_name") or str(cid)
        self.conn.execute(
            """
            INSERT INTO chats(chat_id,type,title,username,status,my_status,added_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                type=excluded.type, title=excluded.title, username=excluded.username,
                status=excluded.status, my_status=excluded.my_status, updated_at=excluded.updated_at
            """,
            (cid, chat.get("type"), title, chat.get("username"), status, my_status, added_at, _now()),
        )
        self.conn.commit()

    def get_chat(self, chat_id):
        return self.conn.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()

    def list_chats(self):
        return self.conn.execute(
            "SELECT * FROM chats ORDER BY (status='active') DESC, title COLLATE NOCASE"
        ).fetchall()

    def delete_chat(self, chat_id):
        """Полностью удаляет чат из базы вместе с его привязкой (сам чат в Telegram не трогает)."""
        self.conn.execute("DELETE FROM bindings WHERE chat_id=?", (chat_id,))
        self.conn.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
        self.conn.commit()

    # ---------- clients ----------
    def upsert_client(self, login, name=None, goals=None, attribution=None, source=None):
        # COALESCE: не затираем уже заданные вручную поля при повторной синхронизации.
        goals_json = json.dumps(goals, ensure_ascii=False) if goals is not None else None
        if self.get_client(login):
            self.conn.execute(
                """
                UPDATE clients SET
                    name=COALESCE(?,name),
                    goals=COALESCE(?,goals),
                    attribution=COALESCE(?,attribution),
                    source=COALESCE(?,source),
                    updated_at=?
                WHERE login=?
                """,
                (name, goals_json, attribution, source, _now(), login),
            )
        else:
            self.conn.execute(
                "INSERT INTO clients(login,name,goals,attribution,source,updated_at,added_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (login, name or login, goals_json or "[]", attribution, source or "manual", _now(), _now()),
            )
        self.conn.commit()

    def get_client(self, login):
        return self.conn.execute("SELECT * FROM clients WHERE login=?", (login,)).fetchone()

    def list_clients(self, owner="all"):
        """owner='all' → все (админ/легаси); owner=<id> → клиенты этого пользователя;
        owner=None → неназначенные (общий пул)."""
        if owner == "all":
            return self.conn.execute(
                "SELECT * FROM clients ORDER BY name COLLATE NOCASE").fetchall()
        if owner is None:
            return self.conn.execute(
                "SELECT * FROM clients WHERE owner IS NULL ORDER BY name COLLATE NOCASE").fetchall()
        return self.conn.execute(
            "SELECT * FROM clients WHERE owner=? ORDER BY name COLLATE NOCASE", (owner,)).fetchall()

    def set_client_owner(self, login, owner):
        """Назначить/снять владельца клиента (owner=None — вернуть в общий пул)."""
        self.conn.execute("UPDATE clients SET owner=?, updated_at=? WHERE login=?",
                          (owner, _now(), login))
        self.conn.commit()

    def owned_logins(self, owner):
        """Логины клиентов пользователя (для скоупа привязок/рассылки)."""
        return [r["login"] for r in
                self.conn.execute("SELECT login FROM clients WHERE owner=?", (owner,))]

    def set_client_sheet(self, login, sheet_id):
        """Привязать таблицу к клиенту (или отвязать, если пусто)."""
        self.conn.execute("UPDATE clients SET sheet_id=? WHERE login=?", (sheet_id or None, login))
        self.conn.commit()

    def client_sheets(self):
        """{логин: id таблицы} — все явные привязки разом."""
        rows = self.conn.execute(
            "SELECT login, sheet_id FROM clients WHERE sheet_id IS NOT NULL AND sheet_id<>''").fetchall()
        return {r["login"]: r["sheet_id"] for r in rows}

    # ─────────── Экспериментальное ───────────
    EXP_PARAMS = ("useful", "clear", "keep")

    def exp_stats(self, keys):
        """Сводка по каждой фиче: сколько раз запускали, кто, средние оценки, отзывы.
        Оценивают только те, кто реально запускал, — иначе это опять доска мнений."""
        out = {}
        for k in keys:
            runs = self.conn.execute(
                "SELECT COUNT(*) n, COUNT(DISTINCT user_id) u, MAX(at) last, "
                "SUM(COALESCE(found,0)) found, AVG(ms) ms FROM exp_runs WHERE key=? AND ok=1",
                (k,)).fetchone()
            votes = self.conn.execute(
                "SELECT param, AVG(score) a, COUNT(*) n FROM exp_votes WHERE key=? GROUP BY param",
                (k,)).fetchall()
            avg = {v["param"]: round(v["a"], 1) for v in votes}
            st = self.conn.execute("SELECT status FROM exp_state WHERE key=?", (k,)).fetchone()
            notes = self.conn.execute(
                "SELECT COUNT(*) n FROM exp_notes WHERE key=?", (k,)).fetchone()
            out[k] = {
                "runs": runs["n"] or 0, "users": runs["u"] or 0, "last": runs["last"],
                "found": int(runs["found"] or 0), "ms": int(runs["ms"] or 0),
                "avg": {p: avg.get(p) for p in self.EXP_PARAMS},
                "voters": len({v["param"] for v in votes}) and self.conn.execute(
                    "SELECT COUNT(DISTINCT user_id) c FROM exp_votes WHERE key=?", (k,)).fetchone()["c"],
                "notes": notes["n"] or 0,
                "status": (st["status"] if st else "testing"),
            }
        return out

    def exp_run_log(self, key, user_id, ms, found, ok=True, error=None):
        self.conn.execute(
            "INSERT INTO exp_runs(key,user_id,at,ms,found,ok,error) VALUES(?,?,?,?,?,?,?)",
            (key, user_id, _now(), int(ms), int(found or 0), 1 if ok else 0, error))
        self.conn.commit()

    def exp_ran(self, key, user_id):
        """Запускал ли этот человек эту фичу: без этого оценивать не даём."""
        r = self.conn.execute("SELECT 1 FROM exp_runs WHERE key=? AND user_id=? LIMIT 1",
                              (key, int(user_id))).fetchone()
        return bool(r)

    def exp_vote(self, key, user_id, param, score):
        if param not in self.EXP_PARAMS:
            raise ValueError("неизвестный параметр: %s" % param)
        self.conn.execute(
            "INSERT OR REPLACE INTO exp_votes(key,user_id,param,score,at) VALUES(?,?,?,?,?)",
            (key, int(user_id), param, max(1, min(5, int(score))), _now()))
        self.conn.commit()

    def exp_votes_of(self, user_id):
        out = {}
        for r in self.conn.execute("SELECT key, param, score FROM exp_votes WHERE user_id=?",
                                   (int(user_id),)).fetchall():
            out.setdefault(r["key"], {})[r["param"]] = r["score"]
        return out

    def exp_note_add(self, key, user_id, text):
        self.conn.execute("INSERT INTO exp_notes(key,user_id,text,at) VALUES(?,?,?,?)",
                          (key, user_id, text, _now()))
        self.conn.commit()

    def exp_notes(self, key):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM exp_notes WHERE key=? ORDER BY id DESC LIMIT 30", (key,)).fetchall()]

    def exp_set_status(self, key, status):
        self.conn.execute("INSERT OR REPLACE INTO exp_state(key,status,at) VALUES(?,?,?)",
                          (key, status, _now()))
        self.conn.commit()

    def set_sheet_col(self, login, title, goal_ids):
        """Ручная разметка столбца таблицы: какие цели в него складывать.

        goal_ids=None — снять разметку (вернуться к угадыванию по названию).
        goal_ids=[]   — «столбец наш, но не заполнять»: так помечают колонки, которые
                        ведут руками или тянут из Calltouch/Callibri.
        """
        t = (title or "").strip()
        if not t:
            return
        if goal_ids is None:
            self.conn.execute("DELETE FROM sheet_cols WHERE login=? AND title=?", (login, t))
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO sheet_cols(login,title,goal_ids,at) VALUES(?,?,?,?)",
                (login, t, ",".join(str(g) for g in goal_ids), _now()))
        self.conn.commit()

    def sheet_cols(self, login=None):
        """Разметка столбцов: для клиента — {заголовок: [id]}, без логина — {логин: {…}}."""
        if login:
            rows = self.conn.execute(
                "SELECT title, goal_ids FROM sheet_cols WHERE login=?", (login,)).fetchall()
            return {r["title"]: [x for x in (r["goal_ids"] or "").split(",") if x] for r in rows}
        out = {}
        for r in self.conn.execute("SELECT login, title, goal_ids FROM sheet_cols").fetchall():
            out.setdefault(r["login"], {})[r["title"]] = [
                x for x in (r["goal_ids"] or "").split(",") if x]
        return out

    def set_client_delivery(self, login, mode):
        """Способ доставки клиента: 'external' (копипаст, сторонний мессенджер) или
        'telegram'/None (обычная бот-рассылка)."""
        mode = "external" if mode == "external" else None
        self.conn.execute("UPDATE clients SET delivery=?, updated_at=? WHERE login=?",
                          (mode, _now(), login))
        self.conn.commit()

    def external_logins(self):
        """Множество логинов, помеченных как «Сторонний» (копипаст) — их не шлём ботом."""
        return {r["login"] for r in
                self.conn.execute("SELECT login FROM clients WHERE delivery='external'")}

    # ---------- users (веб-аккаунты) ----------
    def create_user(self, email, pass_hash, name=None, role="user"):
        cur = self.conn.execute(
            "INSERT INTO users(email,pass_hash,name,role,active,created_at) VALUES(?,?,?,?,1,?)",
            (email.strip().lower(), pass_hash, name, role, _now()))
        self.conn.commit()
        return cur.lastrowid

    def get_user(self, user_id):
        return self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def get_user_by_email(self, email):
        return self.conn.execute("SELECT * FROM users WHERE email=?",
                                 (email.strip().lower(),)).fetchone()

    def list_users(self):
        return self.conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()

    def count_users(self):
        return self.conn.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]

    def set_user_active(self, user_id, active):
        self.conn.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, user_id))
        self.conn.commit()

    def set_user_password(self, user_id, pass_hash):
        self.conn.execute("UPDATE users SET pass_hash=? WHERE id=?", (pass_hash, user_id))
        self.conn.execute("UPDATE users SET pass_version=COALESCE(pass_version,0)+1 WHERE id=?",
                          (user_id,))
        self.conn.commit()

    def set_user_note(self, user_id, note):
        """Своя приписка пользователя к отчётам (NULL=общая, ''=без приписки, текст=своя)."""
        self.conn.execute("UPDATE users SET note=? WHERE id=?", (note, user_id))
        self.conn.commit()

    def set_user_alert(self, user_id, username):
        """Свой @username/чат для бюджет-алертов (NULL/'' = общий по умолчанию)."""
        self.conn.execute("UPDATE users SET alert_username=? WHERE id=?",
                          ((username or None), user_id))
        self.conn.commit()

    def set_user_alert_chat(self, user_id, chat_id):
        """Привязать конкретный chat_id для алертов (по deep-link /start alert_<token>)."""
        self.conn.execute("UPDATE users SET alert_chat_id=? WHERE id=?", (chat_id, user_id))
        self.conn.commit()

    # ---------- активность в чатах (кто кому не ответил) ----------
    def touch_chat_activity(self, chat_id, kind, name=None, text=None):
        """Зафиксировать сообщение в чате. kind: 'client' | 'our' | 'bot'."""
        self.conn.execute("INSERT OR IGNORE INTO chat_activity(chat_id) VALUES(?)", (chat_id,))
        now = _now()
        if kind == "client":
            self.conn.execute(
                "UPDATE chat_activity SET last_client_at=?, last_client_name=?, last_client_text=?, "
                "msgs_client=COALESCE(msgs_client,0)+1, updated_at=? WHERE chat_id=?",
                (now, name, (text or "")[:300], now, chat_id))
        elif kind == "our":
            self.conn.execute(
                "UPDATE chat_activity SET last_our_at=?, last_our_name=?, "
                "msgs_our=COALESCE(msgs_our,0)+1, updated_at=? WHERE chat_id=?",
                (now, name, now, chat_id))
        else:
            self.conn.execute("UPDATE chat_activity SET last_bot_at=?, updated_at=? WHERE chat_id=?",
                              (now, now, chat_id))
        self.conn.commit()

    def list_chat_activity(self):
        return self.conn.execute("SELECT * FROM chat_activity").fetchall()

    def dismiss_chat_wait(self, chat_id, mark_at, user_id=None):
        """Снять чат с ожидания ответа: клиент написал «Отлично», отвечать нечего.

        Запоминаем ИМЕННО то сообщение, на котором сняли (mark_at = last_client_at на
        тот момент). Напишет клиент что-то новое — last_client_at станет больше, и чат
        сам вернётся в список. Никакой уборки по расписанию не нужно."""
        self.conn.execute("INSERT OR IGNORE INTO chat_activity(chat_id) VALUES(?)", (int(chat_id),))
        self.conn.execute(
            "UPDATE chat_activity SET wait_off_at=?, wait_off_by=? WHERE chat_id=?",
            (mark_at, user_id, int(chat_id)))
        self.conn.commit()

    def restore_chat_wait(self, chat_id):
        """Вернуть чат в «Ждут ответа» (отмена снятия)."""
        self.conn.execute(
            "UPDATE chat_activity SET wait_off_at=NULL, wait_off_by=NULL WHERE chat_id=?",
            (int(chat_id),))
        self.conn.commit()

    def seed_activity_from_sendlog(self):
        """Разовая инициализация активности из истории отправок: Telegram не отдаёт боту
        переписку задним числом, но когда МЫ слали отчёт — знаем точно. Даёт «последний
        контакт с клиентом» сразу, без ожидания новых сообщений. Существующие записи не трогаем."""
        rows = self.conn.execute(
            "SELECT chat_id, MAX(sent_at) last FROM send_log "
            "WHERE status='sent' AND chat_id IS NOT NULL GROUP BY chat_id").fetchall()
        n = 0
        for r in rows:
            cur = self.conn.execute("SELECT last_bot_at FROM chat_activity WHERE chat_id=?",
                                    (r["chat_id"],)).fetchone()
            if cur and cur["last_bot_at"]:
                continue
            self.conn.execute("INSERT OR IGNORE INTO chat_activity(chat_id) VALUES(?)", (r["chat_id"],))
            self.conn.execute("UPDATE chat_activity SET last_bot_at=?, updated_at=? WHERE chat_id=?",
                              (r["last"], _now(), r["chat_id"]))
            n += 1
        self.conn.commit()
        return n

    def our_telegram_ids(self, cfg_admin_ids=None):
        """Telegram-id «наших»: админы из app_config + привязанные личики сотрудников
        (в приватном чате chat_id == user_id, поэтому alert_chat_id и есть его tg-id)."""
        ids = set()
        for x in (cfg_admin_ids or []):
            try:
                ids.add(int(x))
            except (TypeError, ValueError):
                pass
        try:
            for r in self.conn.execute("SELECT alert_chat_id FROM users WHERE alert_chat_id IS NOT NULL"):
                ids.add(int(r["alert_chat_id"]))
        except Exception:  # noqa: BLE001 — старая база без колонки
            pass
        return ids

    # ---------- уведомления кабинета ----------
    def add_notification(self, to_user, kind, title, text=None, link=None, dedup_key=None):
        """Создать уведомление. dedup_key — если такое же уже есть за сутки, не дублируем."""
        if dedup_key:
            row = self.conn.execute(
                "SELECT id FROM notifications WHERE kind=? AND title=? AND COALESCE(to_user,-1)=? "
                "AND created_at > datetime('now','-1 day')",
                (kind, title, (to_user if to_user is not None else -1))).fetchone()
            if row:
                return row["id"]
        cur = self.conn.execute(
            "INSERT INTO notifications(to_user,kind,title,text,link,created_at) VALUES(?,?,?,?,?,?)",
            (to_user, kind, title, text, link, _now()))
        self.conn.commit()
        return cur.lastrowid

    def notifications_for(self, user_id, limit=40):
        """Уведомления пользователя (адресные + общие) с флагом прочитанности."""
        return self.conn.execute(
            """SELECT n.*, (a.user_id IS NOT NULL) AS is_read FROM notifications n
               LEFT JOIN notif_read a ON a.notif_id=n.id AND a.user_id=?
               WHERE n.to_user=? OR n.to_user IS NULL
               ORDER BY n.id DESC LIMIT ?""", (user_id, user_id, int(limit))).fetchall()

    def unread_count(self, user_id):
        return self.conn.execute(
            """SELECT COUNT(*) n FROM notifications n
               WHERE (n.to_user=? OR n.to_user IS NULL)
                 AND NOT EXISTS (SELECT 1 FROM notif_read a WHERE a.notif_id=n.id AND a.user_id=?)""",
            (user_id, user_id)).fetchone()["n"]

    def mark_notif_read(self, notif_id, user_id):
        self.conn.execute("INSERT OR IGNORE INTO notif_read(notif_id,user_id,read_at) VALUES(?,?,?)",
                          (int(notif_id), user_id, _now()))
        self.conn.commit()

    def mark_all_notif_read(self, user_id):
        self.conn.execute(
            """INSERT OR IGNORE INTO notif_read(notif_id,user_id,read_at)
               SELECT n.id, ?, ? FROM notifications n WHERE n.to_user=? OR n.to_user IS NULL""",
            (user_id, _now(), user_id))
        self.conn.commit()

    # ---------- бэкап базы ----------
    def backup_to(self, path):
        """Целостная копия БД (sqlite3 backup API — корректно даже при WAL, в отличие от
        обычного копирования файла, где свежие данные остаются в -wal)."""
        import sqlite3 as _s
        dst = _s.connect(path)
        try:
            with dst:
                self.conn.backup(dst)
        finally:
            dst.close()
        return path

    def wal_checkpoint(self):
        """Слить WAL в основной файл (иначе -wal разрастается)."""
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001
            pass

    # ---------- аудит действий ----------
    def log_action(self, user_id, user_name, action, target=None, detail=None):
        """Пишет действие в аудит (кто/что/когда). Сбои не должны ломать основную операцию."""
        try:
            self.conn.execute(
                "INSERT INTO audit(user_id,user_name,action,target,detail,created_at) VALUES(?,?,?,?,?,?)",
                (user_id, user_name, action, target, detail, _now()))
            self.conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def list_audit(self, limit=300, action=None, user_id=None):
        sql = "SELECT * FROM audit"
        cond, args = [], []
        if action:
            cond.append("action=?"); args.append(action)
        if user_id:
            cond.append("user_id=?"); args.append(int(user_id))
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return self.conn.execute(sql, tuple(args)).fetchall()

    # ---------- заметка по проекту ----------
    def set_client_note(self, login, note):
        self.conn.execute("UPDATE clients SET note=?, updated_at=? WHERE login=?",
                          ((note or None), _now(), login))
        self.conn.commit()

    def set_user_alert_scope(self, user_id, scope):
        """Охват алертов: 'all' — по всем клиентам агентства, иначе только свои."""
        self.conn.execute("UPDATE users SET alert_scope=? WHERE id=?",
                          ("all" if scope == "all" else None, user_id))
        self.conn.commit()

    def add_note_reply(self, note_id, user_id, text):
        self.conn.execute(
            "INSERT INTO note_reply(note_id,user_id,text,created_at) VALUES(?,?,?,?)",
            (int(note_id), user_id, text, _now()))
        self.conn.commit()

    def note_replies(self, note_id):
        return self.conn.execute(
            """SELECT r.*, u.name AS user_name FROM note_reply r
               LEFT JOIN users u ON u.id=r.user_id WHERE r.note_id=? ORDER BY r.id""",
            (int(note_id),)).fetchall()

    def all_note_replies(self):
        """Все ответы разом (для списка сообщений наблюдателя) → {note_id: [ {text,user_name,created_at} ]}."""
        rows = self.conn.execute(
            """SELECT r.note_id, r.text, r.created_at, u.name AS user_name FROM note_reply r
               LEFT JOIN users u ON u.id=r.user_id ORDER BY r.id""").fetchall()
        out = {}
        for r in rows:
            out.setdefault(r["note_id"], []).append(
                {"text": r["text"], "user_name": r["user_name"], "created_at": r["created_at"]})
        return out

    # ---------- бюджеты ----------
    def save_budget(self, row):
        self.conn.execute(
            """
            INSERT INTO budgets(login,name,balance,currency,cost7,cost21,rate,days_left,
                                camps_total,camps_on,camps_pay_stopped,daily_budget,status,note,updated_at)
            VALUES(:login,:name,:balance,:currency,:cost7,:cost21,:rate,:days_left,
                   :camps_total,:camps_on,:camps_pay_stopped,:daily_budget,:status,:note,:updated_at)
            ON CONFLICT(login) DO UPDATE SET
                name=excluded.name, balance=excluded.balance, currency=excluded.currency,
                cost7=excluded.cost7, cost21=excluded.cost21, rate=excluded.rate,
                days_left=excluded.days_left, camps_total=excluded.camps_total,
                camps_on=excluded.camps_on, camps_pay_stopped=excluded.camps_pay_stopped,
                daily_budget=excluded.daily_budget, status=excluded.status,
                note=excluded.note, updated_at=excluded.updated_at
            """,
            {**row, "updated_at": _now()},
        )
        self.conn.commit()

    def list_budgets(self):
        """Все строки бюджетов: критичные сверху, потом по «дней осталось»."""
        return self.conn.execute(
            "SELECT * FROM budgets ORDER BY "
            "CASE status WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 WHEN 'ok' THEN 2 "
            "WHEN 'error' THEN 3 ELSE 4 END, "
            "COALESCE(days_left, 1e9), login"
        ).fetchall()

    def delete_budget(self, login):
        self.conn.execute("DELETE FROM budgets WHERE login=?", (login,))
        self.conn.commit()

    def set_user_role(self, user_id, role):
        self.conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        self.conn.commit()

    # ---------- сообщения наблюдателя (notes) ----------
    def create_note(self, to_user, from_user, text, kind="info"):
        cur = self.conn.execute(
            "INSERT INTO notes(to_user,from_user,text,kind,created_at) VALUES(?,?,?,?,?)",
            (to_user, from_user, text, kind, _now()))
        self.conn.commit()
        return cur.lastrowid

    def notes_for_user(self, user_id):
        """Неподтверждённые сообщения пользователю: адресные ему + всем (to_user IS NULL),
        по которым он ещё не нажал «прочитано»."""
        return self.conn.execute(
            """SELECT n.*, u.name AS from_name FROM notes n
               LEFT JOIN users u ON u.id=n.from_user
               WHERE (n.to_user=? OR n.to_user IS NULL)
                 AND NOT EXISTS (SELECT 1 FROM note_ack a WHERE a.note_id=n.id AND a.user_id=?)
               ORDER BY n.created_at""",
            (user_id, user_id)).fetchall()

    def ack_note(self, note_id, user_id):
        self.conn.execute(
            "INSERT OR IGNORE INTO note_ack(note_id,user_id,ack_at) VALUES(?,?,?)",
            (note_id, user_id, _now()))
        self.conn.commit()

    def list_notes(self, limit=100):
        """Все отправленные сообщения (для наблюдателя/админа) с числом подтверждений."""
        return self.conn.execute(
            """SELECT n.*, u.name AS to_name, f.name AS from_name,
                      (SELECT COUNT(*) FROM note_ack a WHERE a.note_id=n.id) AS acks
               FROM notes n
               LEFT JOIN users u ON u.id=n.to_user
               LEFT JOIN users f ON f.id=n.from_user
               ORDER BY n.id DESC LIMIT ?""", (limit,)).fetchall()

    def note_acks(self, note_id):
        return self.conn.execute(
            """SELECT a.user_id, a.ack_at, u.name FROM note_ack a
               LEFT JOIN users u ON u.id=a.user_id WHERE a.note_id=? ORDER BY a.ack_at""",
            (note_id,)).fetchall()

    def delete_note(self, note_id):
        self.conn.execute("DELETE FROM note_ack WHERE note_id=?", (note_id,))
        self.conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
        self.conn.commit()

    def sent_logins_between(self, iso_from, iso_to):
        """Множество логинов, по которым была УСПЕШНАЯ отправка в окне [from,to)."""
        rows = self.conn.execute(
            "SELECT DISTINCT login FROM send_log WHERE status='sent' AND sent_at>=? AND sent_at<?",
            (iso_from, iso_to)).fetchall()
        return {r["login"] for r in rows}

    def status_logins_between(self, status, iso_from, iso_to):
        """Логины с данным статусом в окне (напр. 'skipped' — рассылка запускалась, но нет открута)."""
        rows = self.conn.execute(
            "SELECT DISTINCT login FROM send_log WHERE status=? AND sent_at>=? AND sent_at<?",
            (status, iso_from, iso_to)).fetchall()
        return {r["login"] for r in rows}

    # ---------- уважительные (закрытые долги) ----------
    def add_excuse(self, login, week, kind, reason, by_user):
        cur = self.conn.execute(
            "INSERT INTO excuses(login,week,kind,reason,by_user,created_at) VALUES(?,?,?,?,?,?)",
            (login, week, kind, reason, by_user, _now()))
        self.conn.commit()
        return cur.lastrowid

    def excused_logins(self, week):
        """{login: {'kind','reason','id'}} — уважительные на эту неделю ИЛИ бессрочные (week IS NULL)."""
        rows = self.conn.execute(
            "SELECT * FROM excuses WHERE week=? OR week IS NULL", (week,)).fetchall()
        out = {}
        for r in rows:
            out[r["login"]] = {"id": r["id"], "kind": r["kind"], "reason": r["reason"],
                               "ongoing": r["week"] is None}
        return out

    def list_excuses(self):
        return self.conn.execute(
            """SELECT e.*, c.name AS client_name, u.name AS by_name FROM excuses e
               LEFT JOIN clients c ON c.login=e.login
               LEFT JOIN users u ON u.id=e.by_user ORDER BY e.id DESC""").fetchall()

    def remove_excuse(self, excuse_id):
        self.conn.execute("DELETE FROM excuses WHERE id=?", (excuse_id,))
        self.conn.commit()

    def excuse_owner_login(self, excuse_id):
        r = self.conn.execute("SELECT login FROM excuses WHERE id=?", (excuse_id,)).fetchone()
        return r["login"] if r else None

    def last_send_map(self, logins=None):
        """Когда по каждому клиенту последний раз уходил отчёт — одним запросом.

        По одному last_send_at на четыреста клиентов — четыреста обращений к базе;
        разделу «Сторонние» нужна вся картина сразу."""
        rows = self.conn.execute(
            "SELECT login, MAX(sent_at) m FROM send_log WHERE status='sent' GROUP BY login").fetchall()
        out = {r["login"]: r["m"] for r in rows}
        if logins is None:
            return out
        keep = set(logins)
        return {k: v for k, v in out.items() if k in keep}

    def last_send_at(self, login):
        r = self.conn.execute(
            "SELECT MAX(sent_at) m FROM send_log WHERE login=? AND status='sent'", (login,)).fetchone()
        return r["m"] if r else None

    # ---------- bindings ----------
    def set_binding(self, chat_id, login, bound_by=None):
        self.conn.execute(
            """
            INSERT INTO bindings(chat_id,login,confirmed,bound_by,bound_at)
            VALUES(?,?,1,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET
                login=excluded.login, bound_by=excluded.bound_by, bound_at=excluded.bound_at
            """,
            (chat_id, login, bound_by, _now()),
        )
        self.conn.commit()

    def get_binding(self, chat_id):
        return self.conn.execute("SELECT * FROM bindings WHERE chat_id=?", (chat_id,)).fetchone()

    def remove_binding(self, chat_id):
        self.conn.execute("DELETE FROM bindings WHERE chat_id=?", (chat_id,))
        self.conn.commit()

    def list_bindings(self, owner="all"):
        """owner='all' → все; owner=<id> → привязки клиентов этого пользователя (через clients.owner)."""
        if owner == "all":
            return self.conn.execute("SELECT * FROM bindings").fetchall()
        return self.conn.execute(
            "SELECT b.* FROM bindings b JOIN clients c ON c.login=b.login WHERE c.owner IS ?",
            (owner,)).fetchall()

    def bindings_for_login(self, login):
        """Все чаты, привязанные к данному клиенту (клиент может вещать в несколько чатов)."""
        return self.conn.execute("SELECT * FROM bindings WHERE login=?", (login,)).fetchall()

    # ---------- send log ----------
    def log_send(self, login, chat_id, period_from, period_to, status, error=None):
        self.conn.execute(
            "INSERT INTO send_log(login,chat_id,period_from,period_to,status,error,sent_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (login, chat_id, period_from, period_to, status, error, _now()),
        )
        self.conn.commit()
