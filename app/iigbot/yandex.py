# -*- coding: utf-8 -*-
"""Чтение списка клиентов агентства из API Яндекс.Директа (метод agencyclients.get).

Только чтение, изменяющих вызовов нет. Используется тот же OAuth-токен, что и в
weekly_report.ps1 (secrets.json -> yandex_oauth_token).
"""
import requests

from . import net

API = "https://api.direct.yandex.com/json/v5/"

# коды Директа, у которых есть человеческое объяснение: иначе в кабинете
# показывался сырой технический текст, по которому непонятно, что делать
_ERR_HINTS = {
    53:   "Нет доступа к этому клиенту под текущим токеном.",
    54:   "Токен не подходит: не тот аккаунт или истёк срок.",
    152:  "Закончились баллы Директа на сегодня — попробуй позже.",
    1000: "Директ временно недоступен, повтори чуть позже.",
    9000: "Директ отклонил параметры запроса.",
}


def _explain(err, status=None):
    """Собрать понятное сообщение из ответа Директа."""
    code = err.get("error_code") if isinstance(err, dict) else None
    text = "{} — {}".format((err or {}).get("error_string", ""),
                            (err or {}).get("error_detail", "")).strip(" —")
    hint = _ERR_HINTS.get(int(code)) if str(code).isdigit() else None
    if status == 429:
        hint = hint or "Слишком часто обращаемся к Директу — сбавляем темп."
    parts = ["Директ API: " + text if text else "Директ API вернул ошибку"]
    if code:
        parts.append("(код {})".format(code))
    if hint:
        parts.append("— " + hint)
    return " ".join(parts)



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
    r = net.post("Директ", API + "agencyclients", json=body, headers=headers)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("Директ вернул не-JSON (HTTP {})".format(r.status_code))
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise RuntimeError(_explain(err, getattr(r, "status_code", None)))
    return (data.get("result") or {}).get("Clients", [])


def get_ads_text(token, login, ad_ids, _post=None):
    """{ad_id: {'title','text'}} — заголовки и текст объявлений по их ID (для уровня «Объявления»
    конструктора: Reports API отдаёт только ID). Поддержаны текстовые объявления (TextAd) и
    товарные/динамические — берём что есть. Батчами по 1000, только чтение. Ошибки не роняют отчёт."""
    post = _post or (lambda url, **kw: net.post("Директ", url, **kw))
    ids = []
    for x in ad_ids:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            pass
    ids = sorted(set(ids))
    if not ids:
        return {}
    headers = {
        "Authorization": "Bearer {}".format(token),
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    out = {}
    for i in range(0, len(ids), 1000):
        batch = ids[i:i + 1000]
        body = {"method": "get", "params": {
            "SelectionCriteria": {"Ids": batch},
            "FieldNames": ["Id"],
            "TextAdFieldNames": ["Title", "Title2", "Text"],
            "DynamicTextAdFieldNames": ["Text"],
        }}
        try:
            r = post(API + "ads", json=body, headers=headers, timeout=60)
            data = r.json()
        except Exception:  # noqa: BLE001 — сеть/JSON: пропускаем батч, отчёт не роняем
            continue
        if isinstance(data, dict) and data.get("error"):
            continue
        for a in (data.get("result") or {}).get("Ads", []):
            ta = a.get("TextAd") or a.get("DynamicTextAd") or {}
            title = (ta.get("Title") or "").strip()
            t2 = (ta.get("Title2") or "").strip()
            text = (ta.get("Text") or "").strip()
            hdr = title + (" // " + t2 if t2 else "")
            out[str(a.get("Id"))] = {"title": hdr, "text": text}
    return out


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
    r = net.post("Директ", API + "campaigns", json=body, headers=headers)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("Директ вернул не-JSON (HTTP {})".format(r.status_code))
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise RuntimeError(_explain(err, getattr(r, "status_code", None)))
    camps = (data.get("result") or {}).get("Campaigns", [])
    camps.sort(key=lambda c: (c.get("Name") or "").lower())
    return camps


def get_campaign_counters(token, login, _post=None):
    """ID счётчиков Метрики из настроек кампаний клиента (TextCampaign.CounterIds).

    SelectionCriteria пустой — берём кампании во ВСЕХ статусах (в т.ч. ARCHIVED),
    иначе у приостановленных аккаунтов вернётся пусто.
    """
    post = _post or (lambda url, **kw: net.post("Директ", url, **kw))
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
        raise RuntimeError(_explain(err, getattr(r, "status_code", None)))
    ids = set()
    for c in (data.get("result") or {}).get("Campaigns", []):
        items = (((c.get("TextCampaign") or {}).get("CounterIds") or {}).get("Items")) or []
        for x in items:
            ids.add(str(x))
    return sorted(ids)
