# -*- coding: utf-8 -*-
"""Чтение Яндекс.Метрики (Management API): счётчики и их цели. Только чтение.

Нужен OAuth-токен с правом metrika:read (тот же общий токен, что и для Директа).
"""
import requests

API = "https://api-metrika.yandex.net/management/v1/"


def _get(url, token, _get_fn=None, timeout=30):
    get = _get_fn or requests.get
    r = get(url, headers={"Authorization": "OAuth " + token, "Accept-Language": "ru"}, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("Метрика вернула не-JSON (HTTP {})".format(getattr(r, "status_code", "?")))
    if isinstance(data, dict) and data.get("errors"):
        msgs = "; ".join(e.get("message", "") for e in data["errors"])
        raise RuntimeError("Метрика: {} (код {})".format(msgs, data.get("code", "?")))
    return data


def get_counters(token, _get_fn=None):
    """Все доступные токену счётчики: [{'id','name','site'}]. С пагинацией."""
    out, offset = [], 1
    while True:
        url = API + "counters?per_page=1000&offset={}".format(offset)
        data = _get(url, token, _get_fn)
        chunk = data.get("counters") or []
        for c in chunk:
            out.append({"id": str(c.get("id")), "name": c.get("name") or "",
                        "site": (c.get("site") or "").lower()})
        rows = data.get("rows")
        if not chunk or (rows is not None and len(out) >= rows):
            break
        offset += len(chunk)
    return out


def get_counter_goals(token, counter_id, _get_fn=None):
    """Цели счётчика: [{'id','name','type'}]. Может бросить RuntimeError (403 — нет доступа)."""
    url = API + "counter/{}/goals?useDeleted=false".format(counter_id)
    data = _get(url, token, _get_fn)
    out = []
    for g in (data.get("goals") or []):
        out.append({"id": str(g.get("id")),
                    "name": g.get("name") or ("Цель " + str(g.get("id"))),
                    "type": g.get("type") or ""})
    return out
