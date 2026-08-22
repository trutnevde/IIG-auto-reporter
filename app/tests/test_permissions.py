# -*- coding: utf-8 -*-
"""Матрица прав: кто из ролей какой метод может вызвать.

Зачем именно так. Разбор прав вручную дал 17 дефектов за один заход, и все они были
одного сорта: метод просто забыли закрыть, или проверка стояла не та. Такое нельзя
поймать чтением — методов 150, а глаз замыливается. Зато можно зафиксировать
снимком: прогоняем каждый метод каждой ролью, записываем, кого пустили, и держим
результат в файле рядом. Любая правка, которая меняет доступ, роняет тест и требует
осознанно обновить снимок.

Что тест НЕ проверяет: работает ли метод. Аргументы подставляются фиктивные, сеть
закрыта, половина вызовов падает по делу. Важно одно: отличается ли отказ по правам
от любой другой ошибки. Проверка прав в этом коде всегда стоит первой строкой,
поэтому фиктивных аргументов достаточно.
"""
import inspect
import io
import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "permissions.json")

ROLES = ["agency", "admin", "observer", "user"]

# Слова, по которым узнаём именно отказ по правам, а не любую другую ошибку.
DENIED_MARKS = (
    "Доступно только", "только администратору", "только наблюдателю",
    "режим просмотра", "Не хватает прав", "может менять только",
    "не в вашем доступе", "Чужой", "чужое", "чужой",
)


def _dummy(name, ctx):
    """Правдоподобный аргумент по имени параметра.

    Имена в этом коде говорящие, поэтому подстановка по имени надёжнее, чем по типу:
    login это всегда логин клиента, а user_id всегда идентификатор сотрудника.
    """
    table = {
        "login": ctx["login"], "logins": [ctx["login"]],
        "chat_id": 1, "user_id": ctx["other_id"], "to_user": ctx["other_id"],
        "from_user_id": ctx["other_id"], "to_user_id": ctx["other_id"],
        "note_id": 1, "preset_id": 1, "excuse_id": 1, "run_id": 1, "campaign_id": 1,
        "key": "idle", "param": "useful", "score": 3, "status": "testing",
        "doc_id": "01-паспорт-продукта.md",
        "email": "новый@test.local", "password": "тест-пароль-12345",
        "role": "user", "scope": "own", "mode": "telegram",
        "text": "текст", "note": "заметка", "name": "имя", "title": "заголовок",
        "url": "https://docs.google.com/spreadsheets/d/ТЕСТ/edit",
        "code": "тест", "cut": "week", "username": "test_user",
        "file": "нет-такого-файла.sqlite3", "stamp": "20260101-000000",
        "payload": {"chat_id": 1, "messages": []}, "data": {},
        "active": True, "server": None,
    }
    return table.get(name, "тест")


def _call(api, fn, ctx):
    """Вызвать метод фиктивными аргументами и сказать, чем кончилось."""
    sig = inspect.signature(fn)
    args = [_dummy(p.name, ctx) for p in list(sig.parameters.values())[1:]
            if p.default is inspect._empty]
    try:
        res = fn(api, *args)
    except Exception as e:                      # noqa: BLE001 — @safe ловит сам, но подстрахуемся
        return "исключение", str(e)[:120]
    if not isinstance(res, dict):
        return "разрешено", ""
    if res.get("ok"):
        return "разрешено", ""
    err = str(res.get("error") or "")
    if any(m in err for m in DENIED_MARKS):
        return "отказано", err[:120]
    return "разрешено", err[:120]               # упало, но не по правам — доступ был


_MATRIX_CACHE = {}


def build_matrix(api_for, ctx):
    # Оба теста строят одну и ту же матрицу по одной и той же пустой базе, а это
    # 600 вызовов и минута времени. Считаем один раз за прогон.
    if "готово" in _MATRIX_CACHE:
        return _MATRIX_CACHE["готово"]
    from iigbot.api import Api
    methods = sorted(n for n, f in vars(Api).items()
                     if getattr(f, "_api_exposed", False))
    out = {}
    for name in methods:
        fn = vars(Api)[name]
        row = {}
        for role in ROLES:
            api = api_for(None if role == "agency" else role)
            verdict, _ = _call(api, fn, ctx)
            row[role] = verdict
        out[name] = row
    _MATRIX_CACHE["готово"] = out
    return out


@pytest.fixture
def ctx(users, clients):
    return {"login": clients["mine"], "alien": clients["alien"],
            "other_id": users["other"]["id"]}


def test_матрица_прав_не_изменилась(api_for, ctx):
    """Снимок доступа. Расхождение — не обязательно ошибка, но всегда решение."""
    now = build_matrix(api_for, ctx)
    if not os.path.isfile(GOLDEN):
        io.open(GOLDEN, "w", encoding="utf-8").write(
            json.dumps(now, ensure_ascii=False, indent=1, sort_keys=True))
        pytest.skip("снимок прав создан заново — просмотри его глазами и закоммить")
    was = json.load(io.open(GOLDEN, encoding="utf-8"))

    added = sorted(set(now) - set(was))
    gone = sorted(set(was) - set(now))
    changed = {n: {"было": was[n], "стало": now[n]}
               for n in sorted(set(now) & set(was)) if was[n] != now[n]}

    problems = []
    if added:
        problems.append("новые методы, права не зафиксированы: " + ", ".join(added))
    if gone:
        problems.append("методы исчезли: " + ", ".join(gone))
    for n, d in changed.items():
        problems.append("{}: {} -> {}".format(n, d["было"], d["стало"]))
    assert not problems, (
        "Доступ к методам изменился. Если это задумано — просмотри изменения "
        "и обнови tests/permissions.json.\n" + "\n".join(problems))


def test_каждый_метод_кому_то_запрещён_или_осознанно_открыт(api_for, ctx):
    """Метод, открытый всем без разбора, — кандидат в дыру.

    Список открытых веду явно: если он растёт, это видно в обзоре изменений.
    """
    OPEN_TO_ALL = {
        # Читают только собственные данные вызывающего или общий справочный текст
        "me", "settings", "dashboard", "clients", "client", "chats", "dialogs",
        "budgets", "budgets_state", "reports", "report_history", "my_note", "my_notes",
        "my_alert", "set_my_alert", "set_my_alert_scope", "set_my_note", "notifications",
        "notification_read", "notifications_read_all", "docs_list", "docs_read",
        "experiments", "exp_notes", "exp_note", "exp_vote", "exp_summary", "exp_idle",
        "exp_overspend", "exp_autotarget", "exp_standard", "exp_spike", "exp_bench",
        "exp_trash", "exp_conv_trust", "guide", "changelog", "healthz", "jobs_state",
        "job_progress", "note_ack", "note_reply", "presets", "preset_get",
        "preset_preview", "preset_labels", "client_goals", "client_campaigns",
        "metrika_goals", "counter_check", "dossier", "dossier_cut", "preview",
        "report_query", "report_campaigns", "report_export_xlsx", "gsheets_status",
        "client_sheet_get", "sheet_columns", "gsheets_breakdowns", "excuses",
        "specialist_card", "connect_link", "chat_check", "logins_recent",
        # Ниже — проверены руками 21.08.2026. Роль у них не проверяется намеренно:
        # каждый метод сам сверяет ПРАВА НА ОБЪЕКТ (владелец клиента, видимость чата),
        # а это сильнее ролевой проверки. Список закрыт: новое сюда только после разбора.
        "excuse_add", "excuse_add_bulk", "excuse_remove",   # владелец клиента или супервайзер
        "set_client_note",                                   # он же
        "dialog_dismiss", "dialog_restore",                  # _require_chat_visible
        "notif_read",                                        # только свои уведомления
        "gsheets_clients", "copy_reports", "budgets_refresh",  # ограничены своим скоупом
        "history", "preset_runs", "preset_spec", "presets_list",
        "dossier_options", "report_options", "run_weekly_progress", "suggestions",
        # Проверен руками 22.08.2026: роль не проверяется намеренно, зато поиск
        # сужается до чатов вызывающего — специалист видит только свою переписку,
        # наблюдатель и админ всю. Это сильнее ролевой проверки.
        "chat_search",
    }
    now = build_matrix(api_for, ctx)
    everyone = {n for n, row in now.items()
                if all(v == "разрешено" for v in row.values())}
    unexpected = sorted(everyone - OPEN_TO_ALL)
    assert not unexpected, (
        "Эти методы доступны всем ролям, но в списке осознанно открытых их нет:\n  "
        + "\n  ".join(unexpected)
        + "\nЛибо закрой их, либо добавь в OPEN_TO_ALL с объяснением.")


def test_специалист_не_лезет_в_чужого_клиента(api_for, users, clients):
    """Свой клиент можно, чужой нельзя. Ровно та ошибка, что дала пять дефектов."""
    from iigbot.api import Api
    api = api_for("user")
    for method in ("client", "dossier", "preview", "client_goals", "client_campaigns",
                   "report_query", "save_client", "resend_report"):
        fn = vars(Api).get(method)
        if fn is None:
            continue
        свой = fn(api, clients["mine"])
        чужой = fn(api, clients["alien"])
        assert isinstance(чужой, dict) and not чужой.get("ok"), (
            "{}: специалист получил чужого клиента".format(method))
        assert any(m in str(чужой.get("error") or "") for m in DENIED_MARKS), (
            "{}: отказ по чужому клиенту не похож на отказ по правам: {}"
            .format(method, чужой.get("error")))
        assert isinstance(свой, dict), "{}: свой клиент вернул не словарь".format(method)


def test_пустой_скоуп_означает_пусто_а_не_всё(api_for, db, users):
    """Регресс: у пяти фич пустой список своих проектов означал «показать всё агентство»."""
    from iigbot.api import Api
    db.upsert_client(login="чужой-1", name="Чужой 1")
    db.set_client_owner("чужой-1", users["other"]["id"])
    api = api_for("user")               # у него нет ни одного клиента
    assert api._exp_scope() == [], "скоуп специалиста без клиентов должен быть пустым"
