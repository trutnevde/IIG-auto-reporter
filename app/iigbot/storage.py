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
            """
        )
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

    def list_clients(self):
        return self.conn.execute("SELECT * FROM clients ORDER BY name COLLATE NOCASE").fetchall()

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

    def list_bindings(self):
        return self.conn.execute("SELECT * FROM bindings").fetchall()

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
