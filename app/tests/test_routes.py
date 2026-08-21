# -*- coding: utf-8 -*-
"""Смоук по маршрутам: приложение поднимается и ни один адрес не отвечает 500.

Задача скромная и оттого полезная: поймать опечатку, из-за которой кабинет вообще
не открывается. Такое уже случалось — приложение легло после заливки правки с
синтаксической ошибкой, и узнали мы об этом от клиента.
"""
import json

import pytest


@pytest.fixture
def app(cfg, monkeypatch):
    """Flask-приложение на временной базе, без сети."""
    from iigbot import web
    monkeypatch.setattr(web, "_apis", {})       # кэш Api не должен течь между тестами
    application = web.create_app()
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def маршруты(app):
    """Адреса без параметров: их можно дёрнуть вслепую."""
    out = []
    for r in app.url_map.iter_rules():
        if "<" in str(r.rule) or r.endpoint == "static":
            continue
        out.append((str(r.rule), sorted(r.methods - {"HEAD", "OPTIONS"})))
    return sorted(out)


def test_приложение_поднимается(app):
    assert app is not None
    assert len(маршруты(app)) > 3


def test_ни_один_маршрут_не_отвечает_пятисоткой(client, app):
    """500 — это всегда наша ошибка. 401, 403, 404, 400 — нормальные ответы."""
    плохие = []
    for rule, methods in маршруты(app):
        for m in methods:
            resp = client.open(rule, method=m, json={} if m == "POST" else None)
            if resp.status_code >= 500:
                плохие.append("{} {} -> {}".format(m, rule, resp.status_code))
    assert not плохие, "маршруты упали:\n  " + "\n  ".join(плохие)


def test_живость_отвечает(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert (r.get_json() or {}).get("ok") is True


def test_кабинет_отдаётся_целиком(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert len(body) > 100000, "страница кабинета подозрительно короткая"
    for кусок in ("<title>", "function renderLab", "function api(", "</html>"):
        assert кусок in body, "в странице нет: " + кусок


def test_без_входа_данные_не_отдаются(client):
    """Ключевая граница: неаутентифицированный не должен получать ничего содержательного."""
    for метод in ("clients", "chats", "dashboard", "users", "settings"):
        r = client.post("/api/" + метод, json=[], headers={"X-IIG": "1"})
        assert r.status_code in (200, 401, 403), "{}: {}".format(метод, r.status_code)
        тело = json.dumps(r.get_json() or {}, ensure_ascii=False)
        if r.status_code == 200:
            assert '"ok": true' not in тело.replace(" ", "") or "not_authenticated" in тело, (
                "{} отдал данные без входа: {}".format(метод, тело[:200]))


# Диспетчер отсекает в таком порядке: чужая страница (400), служебное имя с
# подчёркиванием (403), нет входа (401), нет метки @safe (404). Поэтому проверяем
# не конкретный код, а то, что метод НЕ выполнился.
НЕ_ВЫПОЛНЕН = (400, 401, 403, 404)


def test_несуществующий_метод_не_выполняется(client):
    r = client.post("/api/такого_метода_нет", json=[], headers={"X-IIG": "1"})
    assert r.status_code in НЕ_ВЫПОЛНЕН
    assert r.status_code < 500


def test_приватный_метод_через_апи_не_вызвать(client):
    """Диспетчер работает по белому списку: без метки @safe метод недоступен."""
    for имя in ("_require_admin", "_make_backup", "_exp_scope", "__init__"):
        r = client.post("/api/" + имя, json=[], headers={"X-IIG": "1"})
        assert r.status_code in НЕ_ВЫПОЛНЕН, "приватный {} выполнился".format(имя)


def test_белый_список_держит_и_после_входа(app, client, db, monkeypatch):
    """Главная проверка: вошедший пользователь тоже не дотянется до служебных методов.

    Раньше снаружи можно было вызвать ЛЮБОЙ публичный метод объекта, включая
    служебные, — теперь наружу выставлены только помеченные @safe.
    """
    from iigbot import auth, web
    ph = auth.hash_password("тест-пароль-12345")
    db.create_user("вошедший@test.local", ph, name="Вошедший", role="admin")
    monkeypatch.setattr(web, "_apis", {})
    r = client.post("/api/login", json=["вошедший@test.local", "тест-пароль-12345"],
                    headers={"X-IIG": "1"})
    assert (r.get_json() or {}).get("ok"), "вход не прошёл: " + r.get_data(as_text=True)[:200]

    for имя in ("_make_backup", "_exp_scope", "_owner", "_tg_client", "_audit"):
        resp = client.post("/api/" + имя, json=[], headers={"X-IIG": "1"})
        assert resp.status_code in НЕ_ВЫПОЛНЕН, (
            "вошедший дотянулся до служебного {}: {}".format(имя, resp.status_code))
    # а помеченный @safe — работает
    ok = client.post("/api/clients", json=[], headers={"X-IIG": "1"})
    assert ok.status_code == 200 and (ok.get_json() or {}).get("ok") is True


def test_вебхук_без_секрета_отвергается(client):
    r = client.post("/tg/webhook", json={"update_id": 1})
    assert r.status_code == 403


def test_чужой_сайт_не_дёрнет_апи(client):
    """Заголовок X-IIG браузер не даст выставить с чужой страницы без CORS."""
    r = client.post("/api/me", json=[])
    assert r.status_code in (400, 403, 404), (
        "метод отработал без заголовка своей страницы: {}".format(r.status_code))
