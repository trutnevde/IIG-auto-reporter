# -*- coding: utf-8 -*-
"""Чтение списка клиентов агентства из API Яндекс.Директа (метод agencyclients.get).

Только чтение, изменяющих вызовов нет. Используется тот же OAuth-токен, что и в
weekly_report.ps1 (secrets.json -> yandex_oauth_token).
"""
import requests

API = "https://api.direct.yandex.com/json/v5/"


def get_agency_clients(token):
    """Возвращает список словарей вида {'Login','ClientId','ClientInfo'} по всем клиентам агентства."""
    headers = {
        "Authorization": "Bearer {}".format(token),
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {
        "method": "get",
        "params": {
            "SelectionCriteria": {},
            "FieldNames": ["Login", "ClientId", "ClientInfo"],
        },
    }
    r = requests.post(API + "agencyclients", json=body, headers=headers, timeout=60)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("Директ вернул не-JSON (HTTP {})".format(r.status_code))
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise RuntimeError("Директ API: {} — {}".format(err.get("error_string"), err.get("error_detail")))
    return (data.get("result") or {}).get("Clients", [])


def get_campaigns(token, login):
    """Список кампаний клиента (синхронно, быстро): [{'Id','Name'}]. Только чтение."""
    headers = {
        "Authorization": "Bearer {}".format(token),
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {
        "method": "get",
        "params": {"SelectionCriteria": {}, "FieldNames": ["Id", "Name"]},
    }
    r = requests.post(API + "campaigns", json=body, headers=headers, timeout=60)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("Директ вернул не-JSON (HTTP {})".format(r.status_code))
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise RuntimeError("Директ API: {} — {}".format(err.get("error_string"), err.get("error_detail")))
    camps = (data.get("result") or {}).get("Campaigns", [])
    camps.sort(key=lambda c: (c.get("Name") or "").lower())
    return camps


def get_campaign_counters(token, login, _post=None):
    """ID счётчиков Метрики из настроек кампаний клиента (TextCampaign.CounterIds).

    SelectionCriteria пустой — берём кампании во ВСЕХ статусах (в т.ч. ARCHIVED),
    иначе у приостановленных аккаунтов вернётся пусто.
    """
    post = _post or requests.post
    headers = {
        "Authorization": "Bearer {}".format(token),
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {"method": "get", "params": {
        "SelectionCriteria": {},
        "FieldNames": ["Id"],
        "TextCampaignFieldNames": ["CounterIds"],
    }}
    r = post(API + "campaigns", json=body, headers=headers, timeout=60)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("Директ вернул не-JSON (HTTP {})".format(getattr(r, "status_code", "?")))
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise RuntimeError("Директ API: {} — {}".format(err.get("error_string"), err.get("error_detail")))
    ids = set()
    for c in (data.get("result") or {}).get("Campaigns", []):
        items = (((c.get("TextCampaign") or {}).get("CounterIds") or {}).get("Items")) or []
        for x in items:
            ids.add(str(x))
    return sorted(ids)
