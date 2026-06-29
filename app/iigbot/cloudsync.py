# -*- coding: utf-8 -*-
"""Общая онлайн-база привязок/целей через Google-таблицу — одна на все устройства.

Источник правды — таблица «Auto-Reporter КОНФИГ», расшаренная на тот же сервисный аккаунт,
что и клиентские таблицы. Локальная SQLite — кэш: при старте/открытии тянем из облака
(pull), при изменениях (привязал/отвязал/правил цели) — заливаем (push). Для одного человека
на нескольких устройствах этого достаточно (последняя запись побеждает).

Синхронизируем то, что расходится между устройствами: привязки чат↔клиент (+ инфо о чате,
чтобы привязка была читаемой) и цели клиентов. Список клиентов из Директа на каждом
устройстве свой — его не гоняем.
"""
import json
import re

from . import gsheets as G

CONFIG_RE = re.compile(r"Auto-?Reporter\s+КОНФИГ", re.IGNORECASE)
TAB_BIND = "Привязки"
TAB_GOALS = "Цели"


def available():
    return G.available()


def find_config(gc=None):
    """(gc, spreadsheet_id, title) для таблицы «Auto-Reporter КОНФИГ» или (gc, None, None)."""
    gc = gc or G.client(readonly=False)
    for f in gc.list_spreadsheet_files():
        if CONFIG_RE.search((f.get("name") or "").strip()):
            return gc, f.get("id"), f.get("name")
    return gc, None, None


def _ws(sh, title, cols):
    try:
        return sh.worksheet(title)
    except Exception:  # noqa: BLE001
        return sh.add_worksheet(title=title, rows=2000, cols=cols)


def _write(ws, rows):
    from gspread.utils import rowcol_to_a1
    ws.clear()
    if rows:
        rng = "A1:" + rowcol_to_a1(len(rows), max(len(r) for r in rows))
        ws.update(values=rows, range_name=rng, value_input_option="USER_ENTERED")


def _require(gc):
    gc, sid, name = find_config(gc)
    if not sid:
        raise RuntimeError("Не найдена таблица «Auto-Reporter КОНФИГ». Создай её и расшарь на "
                           "сервисный аккаунт (Редактор).")
    return gc.open_by_key(sid), name


def push(db, gc=None):
    """Локальное состояние → облако (перезапись листов «Привязки» и «Цели»)."""
    sh, name = _require(gc)
    chats = {c["chat_id"]: c for c in db.list_chats()}
    rows = [["chat_id", "chat_title", "chat_type", "login"]]
    for b in db.list_bindings():
        c = chats.get(b["chat_id"])
        rows.append([str(b["chat_id"]), (c["title"] if c else ""),
                     (c["type"] if c else ""), b["login"]])
    _write(_ws(sh, TAB_BIND, 6), rows)

    grows = [["login", "name", "goals_json"]]
    for cl in db.list_clients():
        g = cl["goals"] or "[]"
        if g and g.strip() not in ("", "[]"):
            grows.append([cl["login"], cl["name"] or "", g])
    _write(_ws(sh, TAB_GOALS, 4), grows)
    return {"sheet": name, "bindings": len(rows) - 1, "goals": len(grows) - 1}


def pull(db, gc=None):
    """Облако → локальная база (additive upsert: привязки/цели добавляются/обновляются)."""
    sh, name = _require(gc)
    nb = ng = 0
    try:
        rows = sh.worksheet(TAB_BIND).get_all_values()
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows[1:]:
        if len(r) < 4 or not str(r[0]).strip():
            continue
        try:
            cid = int(str(r[0]).strip())
        except ValueError:
            continue
        login = str(r[3]).strip()
        if not login:
            continue
        db.upsert_chat({"id": cid, "type": (r[2].strip() or "group"), "title": r[1]},
                       "member", "active")
        db.set_binding(cid, login)
        nb += 1
    try:
        grows = sh.worksheet(TAB_GOALS).get_all_values()
    except Exception:  # noqa: BLE001
        grows = []
    for r in grows[1:]:
        if len(r) < 3 or not str(r[0]).strip():
            continue
        try:
            goals = json.loads(r[2] or "[]")
        except (ValueError, TypeError):
            goals = None
        db.upsert_client(str(r[0]).strip(), name=(r[1] or None), goals=goals)
        ng += 1
    return {"sheet": name, "bindings": nb, "goals": ng}
