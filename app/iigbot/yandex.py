# -*- coding: utf-8 -*-
"""Работа с API Яндекс.Директа: клиенты агентства, кампании, объявления, корректировки.

Основная часть — только чтение (список клиентов, кампании, счётчики, тексты объявлений).
Отдельным блоком внизу — вызовы, которые СОЗДАЮТ сущности на аккаунте клиента: они нужны
для шаблонов кампаний. Все изменяющие вызовы собраны в одном месте и идут через call(),
чтобы их было видно и легко проверить.

Версии API. v5 — привычный эндпоинт, на нём работает вся отчётность. v501 — эндпоинт
Единой перфоманс-кампании (ЕПК): с переходом Директа на ЕПК новые кампании создаются
только так, и все кампании агентства уже этого типа. Поэтому шаблоны работают на v501.

Токен — тот же, из secrets.json -> yandex_oauth_token.
"""
import requests

from . import net

API = "https://api.direct.yandex.com/json/v5/"
# эндпоинт Единой перфоманс-кампании: создание кампаний живёт только здесь
API501 = "https://api.direct.yandex.com/json/v501/"
# песочница — изолированная копия API: там можно создавать что угодно, боевых данных нет
SANDBOX501 = "https://api-sandbox.direct.yandex.com/json/v501/"

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


# ═══════════════════════ общий вызов API (v5 и v501) ═══════════════════════
def _headers(token, login=None):
    h = {
        "Authorization": "Bearer {}".format(token),
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    if login:
        h["Client-Login"] = login
    return h


def call(token, service, method, params, login=None, base=None, timeout=90):
    """Один вызов API Директа. Возвращает result; на ошибке бросает понятное исключение.

    base позволяет увести вызов в песочницу — тем же кодом, что работает на бою.
    """
    url = (base or API501) + service
    r = net.post("Директ", url, json={"method": method, "params": params},
                 headers=_headers(token, login), timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        # без куска тела такую ошибку невозможно разбирать: HTTP 500 у Директа
        # может быть и сбоем сервиса, и жалобой на конкретное поле
        body = (r.text or "").strip().replace("\n", " ")[:200]
        raise RuntimeError("Директ вернул не-JSON (HTTP {}){}".format(
            r.status_code, ": " + body if body else ""))
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(_explain(data["error"], getattr(r, "status_code", None)))
    return data.get("result") or {}


def campaigns_brief(token, login, base=None):
    """Кампании клиента с типом и состоянием — чтобы выбрать эталон для шаблона."""
    res = call(token, "campaigns", "get", {
        "SelectionCriteria": {},
        "FieldNames": ["Id", "Name", "Type", "Status", "State"],
    }, login=login, base=base)
    camps = res.get("Campaigns", [])
    camps.sort(key=lambda c: (c.get("Name") or "").lower())
    return camps


# Поля, которые вообще имеет смысл снимать в шаблон. Всё остальное (деньги на счёте,
# статистика, идентификаторы) к настройкам не относится.
SNAPSHOT_COMMON = ["Id", "Name", "Type", "StartDate", "EndDate", "TimeZone", "DailyBudget",
                   "NegativeKeywords", "BlockedIps", "ExcludedSites", "TimeTargeting"]
SNAPSHOT_UNIFIED = ["BiddingStrategy", "Settings", "CounterIds", "PriorityGoals",
                    "AttributionModel", "TrackingParams", "NegativeKeywordSharedSetIds"]


def campaign_full(token, login, campaign_id, base=None):
    """Все настройки одной кампании — исходник для «снять шаблон с этой кампании»."""
    res = call(token, "campaigns", "get", {
        "SelectionCriteria": {"Ids": [int(campaign_id)]},
        "FieldNames": SNAPSHOT_COMMON,
        "UnifiedCampaignFieldNames": SNAPSHOT_UNIFIED,
    }, login=login, base=base)
    camps = res.get("Campaigns") or []
    if not camps:
        raise RuntimeError("Кампания не найдена или недоступна под этим доступом")
    return camps[0]


def bidmodifiers_for(token, login, campaign_ids, base=None):
    """Корректировки ставок кампаний — вторая половина слепка."""
    ids = [int(x) for x in campaign_ids]
    if not ids:
        return []
    res = call(token, "bidmodifiers", "get", {
        "SelectionCriteria": {"Levels": ["CAMPAIGN"], "CampaignIds": ids},
        "FieldNames": ["Id", "CampaignId", "Level", "Type"],
        "DemographicsAdjustmentFieldNames": ["Age", "Gender", "BidModifier"],
        "SerpLayoutAdjustmentFieldNames": ["SerpLayout", "BidModifier"],
        "MobileAdjustmentFieldNames": ["BidModifier", "OperatingSystemType"],
        "DesktopAdjustmentFieldNames": ["BidModifier"],
        "TabletAdjustmentFieldNames": ["BidModifier"],
        "SmartTvAdjustmentFieldNames": ["BidModifier"],
        "RegionalAdjustmentFieldNames": ["RegionId", "BidModifier"],
    }, login=login, base=base)
    return res.get("BidModifiers", [])


# ═══════════════════════ ИЗМЕНЯЮЩИЕ ВЫЗОВЫ ═══════════════════════
# Ниже — всё, что создаёт и удаляет сущности на аккаунте клиента. Держим в одном
# месте намеренно: любой такой вызов виден при чтении файла, а не спрятан по коду.

def campaigns_add(token, login, campaigns, base=None):
    """Создать кампании. Возвращает [{'Id'} | {'Errors': [...], 'Warnings': [...]}].

    Кампания без групп и объявлений создаётся черновиком: показов нет, деньги не тратятся,
    пока специалист не наполнит её и не отправит на модерацию.
    """
    res = call(token, "campaigns", "add", {"Campaigns": campaigns},
               login=login, base=base, timeout=120)
    return res.get("AddResults", [])


def campaigns_delete(token, login, ids, base=None):
    """Удалить кампании — нужно, чтобы можно было убрать промах сразу после создания.
    Директ разрешает удалять только кампании без открутки."""
    res = call(token, "campaigns", "delete",
               {"SelectionCriteria": {"Ids": [int(x) for x in ids]}},
               login=login, base=base, timeout=120)
    return res.get("DeleteResults", [])


def bidmodifiers_add(token, login, modifiers, base=None):
    """Повесить корректировки ставок. В одном элементе допустима ровно одна корректировка —
    Директ отвечает ошибкой 5009, если положить две, поэтому каждая уходит отдельно."""
    res = call(token, "bidmodifiers", "add", {"BidModifiers": modifiers},
               login=login, base=base, timeout=120)
    return res.get("AddResults", [])
