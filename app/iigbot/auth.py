# -*- coding: utf-8 -*-
"""Аутентификация для веб-версии: пароли + серверные сессии + декораторы доступа.

Модуль НЕ знает про хранилище — только про Flask-сессию и текущего пользователя в `flask.g.user`
(его кладёт `web.py` в `before_request`, разрешив `session["uid"]` → пользователь из БД).
Так модуль остаётся тестируемым и не завязан на конкретную БД.

Роли: "admin" (видит всё, заводит пользователей, раздаёт клиентов) и "user" (только свои клиенты).
"""
from functools import wraps

from flask import session, g, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

SESSION_KEY = "uid"


# ---- пароли ----
def hash_password(password):
    return generate_password_hash(password or "")


def verify_password(pw_hash, password):
    if not pw_hash:
        return False
    try:
        return check_password_hash(pw_hash, password or "")
    except Exception:  # noqa: BLE001 — битый хеш → просто «не совпало»
        return False


# ---- сессия ----
def login_session(user_id):
    session[SESSION_KEY] = int(user_id)
    session.permanent = True


def logout_session():
    session.pop(SESSION_KEY, None)


def session_user_id():
    return session.get(SESSION_KEY)


def current_user():
    """Текущий пользователь (dict) или None. Кладётся web.py в before_request."""
    return getattr(g, "user", None)


def is_admin():
    u = current_user()
    return bool(u) and u.get("role") == "admin"


# ---- декораторы для эндпоинтов ----
def _unauth():
    return jsonify({"ok": False, "error": "not_authenticated"}), 401


def _forbidden():
    return jsonify({"ok": False, "error": "forbidden"}), 403


def require_auth(fn):
    """Пускает только залогиненного пользователя."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return _unauth()
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    """Пускает только админа."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            return _unauth()
        if u.get("role") != "admin":
            return _forbidden()
        return fn(*args, **kwargs)
    return wrapper
