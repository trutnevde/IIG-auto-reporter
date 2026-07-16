# -*- coding: utf-8 -*-
"""Локальное хранилище (SQLite): чаты, клиенты, привязки, лог отправок.

Одна база на ПК. И бот, и веб-админка работают с ней одновременно (включён WAL).
"""
import os
import json
import sqlite3
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path):
        self.path = path
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        # check_same_thread=False — чтобы Flask мог читать из разных потоков.
        self.conn = sqlite3.connect(path, check_same_thread=False)
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

    def _migrate(self):
        """Безопасные миграции для уже существующих баз (только добавления)."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(clients)")}
        if "owner" not in cols:   # владелец клиента (кому назначен); NULL = общий пул
            self.conn.execute("ALTER TABLE clients ADD COLUMN owner INTEGER")
            self.conn.commit()
        if "delivery" not in cols:   # способ доставки: NULL/'telegram'=бот, 'external'=копипаст (сторонний)
            self.conn.execute("ALTER TABLE clients ADD COLUMN delivery TEXT")
            self.conn.commit()
        ucols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        if "note" not in ucols:   # своя приписка к отчётам: NULL=общая (из Настроек), ''=без, текст=своя
            self.conn.execute("ALTER TABLE users ADD COLUMN note TEXT")
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
                "INSERT INTO clients(login,name,goals,attribution,source,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (login, name or login, goals_json or "[]", attribution, source or "manual", _now()),
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
        self.conn.commit()

    def set_user_note(self, user_id, note):
        """Своя приписка пользователя к отчётам (NULL=общая, ''=без приписки, текст=своя)."""
        self.conn.execute("UPDATE users SET note=? WHERE id=?", (note, user_id))
        self.conn.commit()

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
