# -*- coding: utf-8 -*-
"""Проверка самой обвязки: без неё все остальные тесты ничего не доказывают."""


def test_база_пустая_и_своя(db, cfg):
    assert cfg["db_path"].endswith("test.sqlite3")
    assert db.list_users() == []


def test_роли_заведены(users):
    assert {u["role"] for u in users.values()} == {"admin", "observer", "user"}
    assert users["user"]["id"] != users["other"]["id"]


def test_клиенты_разведены_по_владельцам(db, clients, users):
    assert db.get_client("my-login")["owner"] == users["user"]["id"]
    assert db.get_client("alien-login")["owner"] == users["other"]["id"]


def test_сеть_закрыта():
    import socket
    import pytest
    with pytest.raises(Exception):
        socket.create_connection(("example.com", 80), timeout=1)
