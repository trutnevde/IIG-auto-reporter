# -*- coding: utf-8 -*-
"""Общая обвязка тестов.

Два принципа, из которых всё остальное следует.

Сеть запрещена. Любая попытка открыть сокет валит тест сразу, а не ждёт таймаута.
Это и ускоряет прогон, и доказывает, что «чистые» проверки действительно чистые:
если тест вдруг полез в Директ или Telegram, мы узнаем об этом здесь, а не на боевой.

База своя на каждый тест. Storage принимает путь параметром, поэтому подменяем только
настройки: сам код остаётся нетронутым. Файл живёт во временной папке и уходит вместе с ней.
"""
import os
import socket
import sys

import pytest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Ни один тест не имеет права выйти наружу."""
    def deny(*a, **kw):
        raise NetworkBlocked("тест попытался выйти в сеть")
    monkeypatch.setattr(socket, "socket", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Настройки, указывающие на пустую временную базу."""
    from iigbot import settings
    real = settings.load_app_config

    def fake():
        c = dict(real())
        c["db_path"] = str(tmp_path / "test.sqlite3")
        c["admin_user_ids"] = []
        return c

    monkeypatch.setattr(settings, "load_app_config", fake)
    # api.py импортировал функцию по имени — подменяем и там
    import iigbot.api as api_mod
    monkeypatch.setattr(api_mod, "load_app_config", fake)
    monkeypatch.setattr(api_mod, "load_secrets", lambda: {
        "telegram_bot_token": "000:ТЕСТОВЫЙ_ТОКЕН_НЕ_НАСТОЯЩИЙ",
        "yandex_oauth_token": "тестовый-яндекс-токен",
    })
    return fake()


@pytest.fixture(autouse=True)
def sandbox_fs(tmp_path, monkeypatch):
    """Ни один тест не имеет права тронуть настоящую установку.

    Часть методов работает с файлами по BASE_DIR: копии базы, журнал ошибок, откат
    выката. Проверка прав у них стоит первой строкой, поэтому саму работу можно
    заглушить, а права всё равно проверятся. Пути уводим во временную папку —
    на случай, если какая-то работа всё-таки выполнится.
    """
    from iigbot import settings
    root = tmp_path / "installation"
    root.mkdir()
    monkeypatch.setattr(settings, "BASE_DIR", str(root))
    monkeypatch.setattr(settings, "ERROR_LOG_PATH", str(root / "iig_errors.log"))
    try:
        from iigbot import sysinfo
        for name in ("rollback", "touch_restart", "restore_db"):
            if hasattr(sysinfo, name):
                monkeypatch.setattr(sysinfo, name, lambda *a, **kw: {
                    "ok": True, "stamp": "тест", "files": [], "заглушка": True})
    except Exception:  # noqa: BLE001 — модуль может не импортироваться без хостинга
        pass
    return root


@pytest.fixture
def db(cfg):
    from iigbot.storage import Storage
    return Storage(cfg["db_path"])


@pytest.fixture
def users(db):
    """По одному пользователю каждой роли плюс «второй специалист» для проверки чужого."""
    from iigbot import auth
    ph = auth.hash_password("тест-пароль-12345")
    out = {}
    for key, email, name, role in (
        ("admin", "admin@test.local", "Админ", "admin"),
        ("observer", "obs@test.local", "Наблюдатель", "observer"),
        ("user", "user@test.local", "Специалист", "user"),
        ("other", "other@test.local", "Чужой специалист", "user"),
    ):
        uid = db.create_user(email, ph, name=name, role=role)
        out[key] = dict(db.get_user(uid if isinstance(uid, int) else
                                    db.get_user_by_email(email)["id"]))
    return out


@pytest.fixture
def clients(db, users):
    """Два клиента: свой для «user» и чужой, принадлежащий «other»."""
    db.upsert_client(login="my-login", name="Мой проект")
    db.set_client_owner("my-login", users["user"]["id"])
    db.upsert_client(login="alien-login", name="Чужой проект")
    db.set_client_owner("alien-login", users["other"]["id"])
    return {"mine": "my-login", "alien": "alien-login"}


@pytest.fixture
def api_for(cfg, users):
    """Фабрика: api_for('user') -> Api от лица специалиста, api_for(None) -> агентский режим."""
    from iigbot.api import Api

    def make(role):
        return Api(user=(users[role] if role else None))
    return make
