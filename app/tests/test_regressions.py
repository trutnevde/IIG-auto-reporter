# -*- coding: utf-8 -*-
"""Ошибки, которые уже случались. Каждая должна один раз стать тестом.

Здесь нет ничего гипотетического: все проверки ниже соответствуют поломкам,
которые доехали до боевой или до боевых данных. Дата в комментарии — когда нашли.
"""
import json

import pytest


# ─────────── 19.08.2026: токен бота уезжал в текст ошибки ───────────
# Метод настроек доступен любому вошедшему и вызывается при каждом входе.
# При обрыве связи с Telegram подробность ошибки уходила на экран вместе с
# адресом запроса, а токен — часть этого адреса.
def test_токен_не_попадает_в_текст_ошибки():
    from iigbot.telegram_api import Telegram
    tg = Telegram("1234567890:ОЧЕНЬ_СЕКРЕТНЫЙ_ТОКЕН_БОТА")
    грязь = ("HTTPSConnectionPool(host='api.telegram.org'): "
             "/bot1234567890:ОЧЕНЬ_СЕКРЕТНЫЙ_ТОКЕН_БОТА/getMe не ответил")
    чисто = tg._redact(грязь)
    assert "ОЧЕНЬ_СЕКРЕТНЫЙ_ТОКЕН_БОТА" not in чисто
    assert "1234567890:" not in чисто
    assert "токен скрыт" in чисто


def test_настройки_не_отдают_подробность_ошибки_телеграма(api_for, monkeypatch):
    """В ответе метода настроек не должно быть ничего похожего на токен."""
    import iigbot.api as api_mod

    class ЛомаетсяКлиент:
        def get_me(self):
            raise RuntimeError("сбой связи /bot999:СЕКРЕТ/getMe")

    api = api_for("user")
    monkeypatch.setattr(type(api), "_tg_client", lambda self: ЛомаетсяКлиент())
    res = api.settings()
    текст = json.dumps(res, ensure_ascii=False)
    assert "СЕКРЕТ" not in текст, "подробность ошибки Telegram уехала пользователю"
    assert "999:" not in текст
    assert api_mod is not None


# ─────────── 19.08.2026: двойная сериализация целей ───────────
# upsert_client сериализует список сам. Передали ему готовую строку JSON —
# в базу уехал JSON внутри JSON, и 115 целей превратились в 10 968 элементов
# по одному символу, из них активных ноль.
def test_цели_не_сериализуются_дважды(db):
    цели = [{"id": "1", "name": "Отправка формы", "active": True, "counter": "123"},
            {"id": "2", "name": "Просмотр страниц", "active": False, "counter": "123"}]
    db.upsert_client(login="проект", name="Проект", goals=цели)
    сохранено = json.loads(db.get_client("проект")["goals"])
    assert len(сохранено) == 2, "список целей рассыпался на символы"
    assert all(isinstance(g, dict) for g in сохранено), "цели перестали быть словарями"
    assert сохранено[0]["name"] == "Отправка формы"


def test_строку_json_в_цели_передавать_нельзя(db):
    """Если кто-то снова передаст готовую строку, тест поймает это здесь."""
    db.upsert_client(login="проект2", name="Проект 2",
                     goals=json.dumps([{"id": "1", "name": "Цель"}], ensure_ascii=False))
    сохранено = json.loads(db.get_client("проект2")["goals"])
    рассыпалось = len(сохранено) > 5 and all(
        isinstance(g, str) and len(g) <= 2 for g in сохранено[:5])
    assert not рассыпалось, (
        "строка JSON снова сериализовалась повторно и рассыпалась на символы — "
        "передавайте в upsert_client список, а не строку")


def test_счётчик_у_цели_переживает_сохранение(db):
    """У проектов с несколькими лендингами цели-клоны различимы только по счётчику."""
    from iigbot.import_config import normalize_goals
    цели = normalize_goals([{"id": "7", "name": "Клик по кнопке Позвонить", "counter": "47748562"}])
    db.upsert_client(login="проект3", name="Проект 3", goals=цели)
    сохранено = json.loads(db.get_client("проект3")["goals"])
    assert сохранено[0].get("counter") == "47748562"


# ─────────── 19.08.2026: пустой скоуп означал «всё агентство» ───────────
# У пяти фич стояло `if scope and b["login"] not in scope`. Пустой список своих
# проектов означал ложное условие, проверка отключалась, и специалист без клиентов
# видел всё агентство.
def test_специалист_без_клиентов_видит_пусто(api_for, db, users):
    db.upsert_client(login="чужой-1", name="Чужой 1")
    db.set_client_owner("чужой-1", users["other"]["id"])
    db.upsert_client(login="чужой-2", name="Чужой 2")
    db.set_client_owner("чужой-2", users["other"]["id"])

    api = api_for("user")
    assert api._exp_scope() == [], "пустой скоуп снова означает «всё»"
    res = api.exp_idle()
    assert res.get("ok"), res.get("error")
    assert res["data"]["rows"] == [], "фича показала чужие проекты"


def test_наблюдатель_видит_всё_агентство(api_for, db, users):
    """Обратная сторона: у наблюдателя скоуп «все» — и это правильно."""
    db.upsert_client(login="чужой-3", name="Чужой 3")
    db.set_client_owner("чужой-3", users["other"]["id"])
    assert "чужой-3" in api_for("observer")._exp_scope()


# ─────────── 19.08.2026: заведение сотрудника было сломано ───────────
# Проверка пароля вызывалась раньше, чем подключался модуль, который её выполняет.
# Метод падал всегда: завести человека было нельзя.
def test_сотрудник_заводится(api_for):
    res = api_for("admin").user_create("новичок@test.local", "нормальный-пароль-1")
    assert res.get("ok"), "заведение сотрудника снова сломано: {}".format(res.get("error"))


def test_короткий_пароль_отвергается_а_не_роняет(api_for):
    res = api_for("admin").user_create("второй@test.local", "123")
    assert not res.get("ok")
    assert "парол" in str(res.get("error")).lower()


def test_смена_пароля_работает(api_for, users):
    res = api_for("admin").user_set_password(users["user"]["id"], "другой-пароль-99")
    assert res.get("ok"), "смена пароля снова сломана: {}".format(res.get("error"))


# ─────────── 20.08.2026: адресат копии наружу ───────────
def test_копия_наружу_без_настройки_не_падает(cfg):
    """Ненастроенная выгрузка должна вежливо отказаться, а не уронить ночную задачу."""
    from iigbot import backup_cloud
    res = backup_cloud.run(cfg=dict(cfg, backup_target=""))
    assert res.get("skipped") is True
    assert not res.get("ok")


def test_снимок_базы_целостный_и_сжатый(cfg, db, tmp_path):
    """Копирование файла на WAL-базе даёт битый снимок, поэтому берём штатное."""
    import gzip
    import sqlite3
    from iigbot import backup_cloud
    db.upsert_client(login="для-копии", name="Для копии")
    dest = str(tmp_path / "снимок.sqlite3.gz")
    size = backup_cloud.snapshot(cfg["db_path"], dest)
    assert size > 0
    сырой = str(tmp_path / "распакован.sqlite3")
    with gzip.open(dest, "rb") as fi, open(сырой, "wb") as fo:
        fo.write(fi.read())
    conn = sqlite3.connect(сырой)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 1
    conn.close()


# ─────────── 19.08.2026: чужие сообщения и чужие чаты ───────────
def test_нельзя_ответить_на_чужое_сообщение(api_for, users):
    """Номер сообщения ничем не проверялся: перебором можно было вклиниться в чужую переписку."""
    admin = api_for("admin")
    создано = admin.note_create(users["other"]["id"], "сообщение не тебе")
    assert создано.get("ok"), создано.get("error")
    note_id = создано["data"].get("id") or created_id(created=создано)
    чужой = api_for("user").note_reply(note_id, "подслушал")
    assert not чужой.get("ok"), "ответ на чужое сообщение снова проходит"


def created_id(created):
    d = created.get("data") or {}
    for k in ("id", "note_id"):
        if d.get(k):
            return d[k]
    return 1


def test_импорт_чата_проверяет_доступ(api_for, db, clients):
    """Идентификатор чата брался из загружаемого файла и не сверялся ни с чем:
    выгрузкой можно было переписать активность любого чата агентства."""
    чужой_чат = 999999
    db.upsert_chat({"id": чужой_чат, "type": "private", "title": "Чат чужого клиента"},
                   "member", "active")
    db.set_binding(чужой_чат, clients["alien"])       # чат принадлежит другому специалисту

    payload = {"id": чужой_чат, "name": "Чат чужого клиента",
               "messages": [{"id": 1, "type": "message", "date": "2026-08-01T10:00:00",
                             "from": "Клиент", "from_id": "user999", "text": "привет"}]}
    res = api_for("user").import_chat_history(payload)
    текст = json.dumps(res, ensure_ascii=False)
    assert res["data"]["messages"] == 0, "импорт записал сообщения в чужой чат: " + текст[:200]
    assert "не в вашем доступе" in текст, (
        "отказ по чужому чату не похож на отказ по правам: " + текст[:200])


def test_импорт_своего_чата_проходит(api_for, db, clients):
    """Обратная сторона: свой чат импортируется, иначе сторож бесполезен."""
    свой = 111111
    db.upsert_chat({"id": свой, "type": "private", "title": "Мой чат"}, "member", "active")
    db.set_binding(свой, clients["mine"])
    payload = {"id": свой, "name": "Мой чат",
               "messages": [{"id": 1, "type": "message", "date": "2026-08-01T10:00:00",
                             "from": "Клиент", "from_id": "user1", "text": "привет"}]}
    res = api_for("user").import_chat_history(payload)
    assert res.get("ok"), res.get("error")
    assert "не в вашем доступе" not in json.dumps(res, ensure_ascii=False)
