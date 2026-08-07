# -*- coding: utf-8 -*-
"""Шаблоны кампаний: один раз описываем стандарт агентства — дальше кампании создаются по нему.

Зачем. Каждый новый проект начинается с одних и тех же движений: выставить стратегию,
залить общий список минус-слов, прописать UTM-метку, выбрать модель атрибуции, повесить
корректировки (эксклюзивное размещение, реклама в подсказках, отсечь аудиторию до 18).
Руками это десяток экранов и место, где легко забыть галочку. Шаблон делает то же самое
одним нажатием и одинаково у всех специалистов.

Что здесь есть и чего нет. Шаблон описывает НАСТРОЙКИ кампании — то, что одинаково от
проекта к проекту. Всё, что у каждого клиента своё (счётчик Метрики, цели, регионы показа,
тексты объявлений, ключевые фразы), в шаблоне не хранится: часть подставляется при
применении из карточки клиента, остальное специалист заводит сам.

Безопасность. Создаётся кампания без групп и объявлений — Директ держит такую в статусе
черновика: показов нет, деньги не тратятся. Пока специалист не наполнит её и не отправит
на модерацию, ничего не произойдёт. Ошибочно созданную кампанию можно удалить.

Формат шаблона — обычный словарь (в базе лежит JSON). Ничего не захардкожено: набор полей
и допустимые значения описаны в SPEC, и морда строит форму по нему.
"""
import copy
import datetime as dt

# В API Директа деньги в микроединицах: 1 рубль = 1 000 000.
MICRO = 1000000

# Стратегии показа. Ключ — как это называется в API, значение — как в интерфейсе Директа,
# плюс какие параметры у стратегии есть.
SEARCH_STRATEGIES = [
    {"id": "WB_MAXIMUM_CLICKS", "name": "Максимум кликов", "params": ["weekly", "bid_ceiling"]},
    {"id": "WB_MAXIMUM_CONVERSION_RATE", "name": "Максимум конверсий",
     "params": ["weekly", "bid_ceiling", "goal"]},
    {"id": "AVERAGE_CPA", "name": "Средняя цена конверсии", "params": ["cpa", "weekly", "goal"]},
    {"id": "AVERAGE_CPC", "name": "Средняя цена клика", "params": ["cpc", "weekly"]},
    {"id": "AVERAGE_CRR", "name": "Доля рекламных расходов", "params": ["crr", "weekly", "goal"]},
    {"id": "PAY_FOR_CONVERSION", "name": "Оплата за конверсии", "params": ["cpa", "weekly", "goal"]},
    {"id": "HIGHEST_POSITION", "name": "Ручное управление (наивысшая позиция)", "params": []},
    {"id": "SERVING_OFF", "name": "Показы отключены", "params": []},
]
# Директ разрешает дневной бюджет только вместе с ручной стратегией: на автоматических
# бюджетом управляет сама стратегия, и запрос с обоими полями он отклоняет.
MANUAL_SEARCH = {"HIGHEST_POSITION"}

NETWORK_STRATEGIES = [
    {"id": "NETWORK_DEFAULT", "name": "Как на поиске (по умолчанию)", "params": []},
    {"id": "MAXIMUM_COVERAGE", "name": "Максимальный охват", "params": []},
    {"id": "SERVING_OFF", "name": "Показы в сетях отключены", "params": []},
]

# Флаги кампании. Именно эти показывает Директ у Единой перфоманс-кампании.
SETTINGS = [
    {"id": "ADD_METRICA_TAG", "name": "Размечать ссылки для Метрики", "default": "YES"},
    {"id": "ENABLE_SITE_MONITORING", "name": "Останавливать при недоступности сайта", "default": "YES"},
    {"id": "ENABLE_COMPANY_INFO", "name": "Подставлять данные из Яндекс Бизнеса", "default": "YES"},
    {"id": "ENABLE_AREA_OF_INTEREST_TARGETING", "name": "Расширенный географический таргетинг",
     "default": "YES"},
    {"id": "CAMPAIGN_EXACT_PHRASE_MATCHING_ENABLED", "name": "Только точное соответствие фраз",
     "default": "NO"},
    {"id": "ALTERNATIVE_TEXTS_ENABLED", "name": "Автоматически подбирать тексты", "default": "NO"},
    {"id": "ADD_TO_FAVORITES", "name": "Добавить в избранное", "default": "NO"},
    {"id": "REQUIRE_SERVICING", "name": "Запросить помощь менеджера Яндекса", "default": "NO"},
]

ATTRIBUTION = [
    {"id": "AUTO", "name": "Автоматическая (рекомендует Яндекс)"},
    {"id": "LSCCD", "name": "Последний значимый переход, кросс-девайс"},
    {"id": "LSC", "name": "Последний значимый переход"},
    {"id": "LC", "name": "Последний переход"},
    {"id": "FCCD", "name": "Первый переход, кросс-девайс"},
]

# Корректировки ставок. kind — наше короткое имя, дальше собираем из него запрос к API.
AGES = [
    {"id": "AGE_0_17", "name": "до 18 лет"},
    {"id": "AGE_18_24", "name": "18–24"},
    {"id": "AGE_25_34", "name": "25–34"},
    {"id": "AGE_35_44", "name": "35–44"},
    {"id": "AGE_45_54", "name": "45–54"},
    {"id": "AGE_55", "name": "55 и старше"},
]
GENDERS = [
    {"id": "", "name": "любой пол"},
    {"id": "GENDER_MALE", "name": "мужчины"},
    {"id": "GENDER_FEMALE", "name": "женщины"},
]
DEVICES = [
    {"id": "mobile", "name": "Смартфоны", "field": "MobileAdjustment"},
    {"id": "desktop", "name": "Компьютеры и Smart TV", "field": "DesktopAdjustment"},
    {"id": "tablet", "name": "Планшеты", "field": "TabletAdjustment"},
    {"id": "smarttv", "name": "Только Smart TV", "field": "SmartTvAdjustment"},
]
SERP_LAYOUTS = [
    {"id": "ALONE", "name": "Эксклюзивное размещение"},
    {"id": "SUGGEST", "name": "Реклама в поисковых подсказках"},
]

MODIFIER_KINDS = [
    {"kind": "serp", "name": "Поисковое размещение", "options": SERP_LAYOUTS,
     "min": 1, "max": 1300, "hint": "Процент от ставки: 130 — поднять на 30%"},
    {"kind": "demography", "name": "Пол и возраст", "ages": AGES, "genders": GENDERS,
     "min": 0, "max": 1300, "hint": "0 — не показывать этой аудитории совсем"},
    {"kind": "device", "name": "Устройства", "options": DEVICES, "min": 0, "max": 1300,
     "hint": "0 — не показывать на этих устройствах"},
    {"kind": "region", "name": "Регион показа", "min": 10, "max": 1300,
     "hint": "Номер региона из справочника Директа: 213 — Москва, 2 — Санкт-Петербург"},
]

# Что подставляется при применении, а не хранится в шаблоне.
SUBSTITUTIONS = [
    {"id": "counter", "name": "Счётчик Метрики", "from": "берётся из кампаний клиента"},
    {"id": "goals", "name": "Приоритетные цели", "from": "цели клиента, отмеченные в кабинете"},
    {"id": "name", "name": "Название кампании", "from": "маска шаблона + имя клиента"},
    {"id": "start", "name": "Дата старта", "from": "сегодня"},
]


def spec():
    """Описание всех полей шаблона — по нему морда рисует форму.

    Держим справочники на сервере, а не в разметке: иначе список стратегий пришлось бы
    править в двух местах и они бы разъехались.
    """
    return {
        "search_strategies": SEARCH_STRATEGIES,
        "network_strategies": NETWORK_STRATEGIES,
        "settings": SETTINGS,
        "attribution": ATTRIBUTION,
        "modifier_kinds": MODIFIER_KINDS,
        "ages": AGES, "genders": GENDERS, "devices": DEVICES, "serp_layouts": SERP_LAYOUTS,
        "substitutions": SUBSTITUTIONS,
    }


def blank():
    """Пустой шаблон со здравыми умолчаниями — с него начинается создание нового."""
    return {
        "strategy": {"search": {"type": "WB_MAXIMUM_CLICKS", "weekly": 3000, "bid_ceiling": None},
                     "network": {"type": "NETWORK_DEFAULT"}},
        "settings": {s["id"]: s["default"] for s in SETTINGS},
        "attribution": "AUTO",
        "negative_keywords": [],
        "tracking_params": ("utm_source=YandexDirect&utm_medium=cpc&utm_campaign={campaign_id}"
                            "&utm_content={ad_id}&utm_term={keyword}"),
        "excluded_sites": [],
        "blocked_ips": [],
        "time_zone": "Europe/Moscow",
        "modifiers": [],
        "goal_value": None,
        "use_client_goals": True,
        "use_client_counter": True,
        "name_mask": "{клиент} — Поиск и Сети",
    }


# ─────────────────────────── снять шаблон с готовой кампании ───────────────────────────
def from_campaign(camp, modifiers=None):
    """Слепок настроек живой кампании. Самый короткий путь к шаблону: специалист один раз
    собрал эталонную кампанию руками — забираем из неё всё, что переносимо."""
    uni = camp.get("UnifiedCampaign") or camp.get("TextCampaign") or {}
    p = blank()

    strat = uni.get("BiddingStrategy") or {}
    p["strategy"] = {"search": _strategy_in(strat.get("Search")),
                     "network": _strategy_in(strat.get("Network"), network=True)}

    got = {}
    for s in (uni.get("Settings") or []):
        if s.get("Option") in {x["id"] for x in SETTINGS}:
            got[s["Option"]] = s.get("Value")
    if got:
        p["settings"] = {s["id"]: got.get(s["id"], s["default"]) for s in SETTINGS}

    p["attribution"] = uni.get("AttributionModel") or "AUTO"
    p["tracking_params"] = uni.get("TrackingParams") or ""
    p["negative_keywords"] = list((camp.get("NegativeKeywords") or {}).get("Items") or [])
    p["excluded_sites"] = list((camp.get("ExcludedSites") or {}).get("Items") or [])
    p["blocked_ips"] = list((camp.get("BlockedIps") or {}).get("Items") or [])
    p["time_zone"] = camp.get("TimeZone") or "Europe/Moscow"

    db = camp.get("DailyBudget") or None
    if db and db.get("Amount"):
        p["daily_budget"] = {"amount": round(db["Amount"] / MICRO, 2),
                             "mode": db.get("Mode") or "STANDARD"}

    tt = camp.get("TimeTargeting") or None
    # расписание переносим только если оно не круглосуточное — иначе это лишний шум
    if tt and not _schedule_is_full(tt):
        p["time_targeting"] = tt

    goals = ((uni.get("PriorityGoals") or {}).get("Items")) or []
    if goals and goals[0].get("Value"):
        p["goal_value"] = round(goals[0]["Value"] / MICRO, 2)

    p["modifiers"] = [m for m in (_modifier_in(x) for x in (modifiers or [])) if m]
    p["name_mask"] = "{клиент} — " + _strip_client(camp.get("Name") or "Поиск и Сети")
    return p


def _strategy_in(node, network=False):
    """Стратегия из ответа API → в наш плоский вид."""
    node = node or {}
    t = node.get("BiddingStrategyType") or ("NETWORK_DEFAULT" if network else "WB_MAXIMUM_CLICKS")
    out = {"type": t}
    # у разных стратегий параметры лежат в по-разному названных вложенных объектах
    for key in ("WbMaximumClicks", "WbMaximumConversionRate", "AverageCpa", "AverageCpc",
                "AverageCrr", "PayForConversion"):
        d = node.get(key)
        if not isinstance(d, dict):
            continue
        if d.get("WeeklySpendLimit"):
            out["weekly"] = round(d["WeeklySpendLimit"] / MICRO, 2)
        if d.get("BidCeiling"):
            out["bid_ceiling"] = round(d["BidCeiling"] / MICRO, 2)
        if d.get("AverageCpa"):
            out["cpa"] = round(d["AverageCpa"] / MICRO, 2)
        if d.get("AverageCpc"):
            out["cpc"] = round(d["AverageCpc"] / MICRO, 2)
        if d.get("Crr"):
            out["crr"] = d["Crr"]
    return out


def _modifier_in(m):
    """Корректировка из ответа API → в наш вид. Ретаргетинг пропускаем: он завязан на
    условия конкретного аккаунта и на другой аккаунт не переносится."""
    t = m.get("Type") or ""
    if t == "SERP_LAYOUT_ADJUSTMENT":
        d = m.get("SerpLayoutAdjustment") or {}
        return {"kind": "serp", "layout": d.get("SerpLayout"), "value": d.get("BidModifier")}
    if t == "DEMOGRAPHICS_ADJUSTMENT":
        d = m.get("DemographicsAdjustment") or {}
        return {"kind": "demography", "age": d.get("Age") or "", "gender": d.get("Gender") or "",
                "value": d.get("BidModifier")}
    if t == "REGIONAL_ADJUSTMENT":
        d = m.get("RegionalAdjustment") or {}
        return {"kind": "region", "region_id": d.get("RegionId"), "value": d.get("BidModifier")}
    for dev in DEVICES:
        if t == _device_type(dev["id"]):
            d = m.get(dev["field"]) or {}
            return {"kind": "device", "device": dev["id"], "value": d.get("BidModifier")}
    return None


def _device_type(dev_id):
    return {"mobile": "MOBILE_ADJUSTMENT", "desktop": "DESKTOP_ADJUSTMENT",
            "tablet": "TABLET_ADJUSTMENT", "smarttv": "SMARTTV_ADJUSTMENT"}.get(dev_id)


def _schedule_is_full(tt):
    """Расписание «круглосуточно всю неделю» — значит его не задавали."""
    items = ((tt or {}).get("Schedule") or {}).get("Items") or []
    if len(items) != 7:
        return False
    for row in items:
        if set(str(row).split(",")[1:]) != {"100"}:
            return False
    return True


def _strip_client(name):
    """Из «ТГО РК — Поиск и Сети — Картонная упаковка» делаем «Поиск и Сети — Картонная упаковка»:
    имя клиента в маску подставится своё."""
    return (name or "").strip()


# ─────────────────────────── применение шаблона ───────────────────────────
def campaign_name(preset, client_name, custom=None):
    """Название по маске. {клиент} — имя проекта, {дата} — сегодня."""
    if custom:
        return str(custom)[:255]
    mask = preset.get("name_mask") or "{клиент}"
    return (mask.replace("{клиент}", client_name or "")
                .replace("{дата}", dt.date.today().strftime("%d.%m.%Y"))).strip()[:255]


def to_payload(preset, client_name, counters=None, goal_ids=None, custom_name=None, today=None):
    """Собрать тело запроса campaigns.add. Всё клиент-специфичное приходит аргументами."""
    p = preset or {}
    day = (today or dt.date.today()).isoformat()
    uni = {}

    strat = p.get("strategy") or {}
    uni["BiddingStrategy"] = {
        "Search": _strategy_out(strat.get("search") or {}, goal_ids),
        "Network": _strategy_out(strat.get("network") or {"type": "NETWORK_DEFAULT"}, goal_ids,
                                 network=True),
    }

    st = p.get("settings") or {}
    items = [{"Option": s["id"], "Value": st.get(s["id"], s["default"])} for s in SETTINGS]
    if items:
        uni["Settings"] = items

    if p.get("attribution"):
        uni["AttributionModel"] = p["attribution"]
    if p.get("tracking_params"):
        uni["TrackingParams"] = p["tracking_params"]

    if p.get("use_client_counter", True) and counters:
        uni["CounterIds"] = {"Items": [int(c) for c in counters]}

    if p.get("use_client_goals", True) and goal_ids and p.get("goal_value"):
        uni["PriorityGoals"] = {"Items": [
            {"GoalId": int(g), "Value": int(round(float(p["goal_value"]) * MICRO)),
             "IsMetrikaSourceOfValue": "NO"} for g in goal_ids]}

    camp = {
        "Name": campaign_name(p, client_name, custom_name),
        "StartDate": day,
        "UnifiedCampaign": uni,
    }
    if p.get("time_zone"):
        camp["TimeZone"] = p["time_zone"]
    if p.get("negative_keywords"):
        camp["NegativeKeywords"] = {"Items": list(p["negative_keywords"])}
    if p.get("excluded_sites"):
        camp["ExcludedSites"] = {"Items": list(p["excluded_sites"])}
    if p.get("blocked_ips"):
        camp["BlockedIps"] = {"Items": list(p["blocked_ips"])}
    if p.get("time_targeting"):
        # Директ отдаёт незаполненные поля как null, но на запись их не принимает
        # («HolidaysSchedule не может иметь значение null») — вычищаем пустоту.
        camp["TimeTargeting"] = _drop_nulls(copy.deepcopy(p["time_targeting"]))
    db = p.get("daily_budget") or None
    if db and db.get("amount"):
        camp["DailyBudget"] = {"Amount": int(round(float(db["amount"]) * MICRO)),
                               "Mode": db.get("mode") or "STANDARD"}
    return camp


def _drop_nulls(node):
    """Убрать пустые значения из вложенной структуры перед отправкой в Директ."""
    if isinstance(node, dict):
        return {k: _drop_nulls(v) for k, v in node.items() if v is not None}
    if isinstance(node, list):
        return [_drop_nulls(v) for v in node if v is not None]
    return node


def _strategy_out(s, goal_ids=None, network=False):
    """Наш плоский вид стратегии → структура API."""
    t = s.get("type") or ("NETWORK_DEFAULT" if network else "WB_MAXIMUM_CLICKS")
    out = {"BiddingStrategyType": t}
    box = {}
    if s.get("weekly"):
        box["WeeklySpendLimit"] = int(round(float(s["weekly"]) * MICRO))
    if s.get("bid_ceiling"):
        box["BidCeiling"] = int(round(float(s["bid_ceiling"]) * MICRO))
    if s.get("cpa"):
        box["AverageCpa"] = int(round(float(s["cpa"]) * MICRO))
    if s.get("cpc"):
        box["AverageCpc"] = int(round(float(s["cpc"]) * MICRO))
    if s.get("crr"):
        box["Crr"] = int(s["crr"])
    if goal_ids and t in ("WB_MAXIMUM_CONVERSION_RATE", "AVERAGE_CPA", "AVERAGE_CRR",
                          "PAY_FOR_CONVERSION"):
        box["GoalId"] = int(goal_ids[0])
    key = {"WB_MAXIMUM_CLICKS": "WbMaximumClicks",
           "WB_MAXIMUM_CONVERSION_RATE": "WbMaximumConversionRate",
           "AVERAGE_CPA": "AverageCpa", "AVERAGE_CPC": "AverageCpc",
           "AVERAGE_CRR": "AverageCrr", "PAY_FOR_CONVERSION": "PayForConversion"}.get(t)
    if key and box:
        out[key] = box
    return out


def modifiers_payload(preset, campaign_id):
    """Корректировки к запросу bidmodifiers.add.

    Каждая уходит отдельным элементом: Директ отвечает ошибкой 5009, если в одном
    элементе указано больше одного вида корректировки.
    """
    out = []
    serp = []
    for m in (preset.get("modifiers") or []):
        kind, val = m.get("kind"), m.get("value")
        if val is None:
            continue
        val = int(val)
        if kind == "serp" and m.get("layout"):
            serp.append({"SerpLayout": m["layout"], "BidModifier": val})
        elif kind == "demography" and m.get("age"):
            item = {"Age": m["age"], "BidModifier": val}
            if m.get("gender"):
                item["Gender"] = m["gender"]
            out.append({"CampaignId": int(campaign_id), "DemographicsAdjustments": [item]})
        elif kind == "device" and m.get("device"):
            field = {d["id"]: d["field"] for d in DEVICES}.get(m["device"])
            if field:
                out.append({"CampaignId": int(campaign_id), field: {"BidModifier": val}})
        elif kind == "region" and m.get("region_id"):
            out.append({"CampaignId": int(campaign_id), "RegionalAdjustments": [
                {"RegionId": int(m["region_id"]), "BidModifier": val}]})
    if serp:
        # один вид — можно списком, это по-прежнему одно поле в элементе
        out.append({"CampaignId": int(campaign_id), "SerpLayoutAdjustments": serp})
    return out


# ─────────────────────────── проверки и предпросмотр ───────────────────────────
def validate(preset):
    """Что не так с шаблоном. Пустой список — можно применять."""
    p = preset or {}
    bad = []
    s = (p.get("strategy") or {}).get("search") or {}
    known = {x["id"] for x in SEARCH_STRATEGIES}
    if s.get("type") not in known:
        bad.append("Не выбрана стратегия на поиске")
    if s.get("type") in ("WB_MAXIMUM_CLICKS", "WB_MAXIMUM_CONVERSION_RATE") and not s.get("weekly"):
        bad.append("У этой стратегии нужен недельный бюджет")
    if s.get("weekly") and float(s["weekly"]) < 300:
        bad.append("Недельный бюджет меньше 300 ₽ — Директ такой не примет")
    n = (p.get("strategy") or {}).get("network") or {}
    if n.get("type") and n["type"] not in {x["id"] for x in NETWORK_STRATEGIES}:
        bad.append("Неизвестная стратегия в сетях")
    if p.get("attribution") and p["attribution"] not in {x["id"] for x in ATTRIBUTION}:
        bad.append("Неизвестная модель атрибуции")
    for kw in (p.get("negative_keywords") or []):
        if len(str(kw).split()) > 7:
            bad.append("Минус-фраза длиннее семи слов: «{}»".format(kw))
            break
    if len(p.get("negative_keywords") or []) > 1000:
        bad.append("Минус-слов больше 1000")
    for m in (p.get("modifiers") or []):
        v = m.get("value")
        if v is None or not (0 <= int(v) <= 1300):
            bad.append("Корректировка вне диапазона 0–1300%")
            break
        if m.get("kind") == "serp" and int(v) < 1:
            bad.append("Для поискового размещения корректировка не может быть нулевой")
            break
        if m.get("kind") == "region" and int(v) < 10:
            bad.append("Для региона минимальная корректировка — 10%")
            break
    db = p.get("daily_budget") or None
    if db and db.get("amount") and s.get("type") not in MANUAL_SEARCH:
        bad.append("Дневной бюджет работает только с ручной стратегией — убери бюджет "
                   "или поставь ручное управление")
    if db and db.get("amount") and float(db["amount"]) < 300:
        bad.append("Дневной бюджет меньше 300 ₽ — Директ такой не примет")
    if p.get("goal_value") is not None and float(p.get("goal_value") or 0) < 0:
        bad.append("Ценность конверсии не может быть отрицательной")
    if not (p.get("name_mask") or "").strip():
        bad.append("Пустая маска названия кампании")
    return bad


def describe(preset, client_name=None, counters=None, goals=None):
    """Человеческое описание того, что произойдёт, — для экрана подтверждения.
    Пишем ровно то, что уйдёт в Директ, а не общие слова."""
    p = preset or {}
    lines = []
    s = (p.get("strategy") or {}).get("search") or {}
    nm = {x["id"]: x["name"] for x in SEARCH_STRATEGIES}.get(s.get("type"), s.get("type") or "—")
    tail = []
    if s.get("weekly"):
        tail.append("недельный бюджет {:,.0f} ₽".format(float(s["weekly"])).replace(",", " "))
    if s.get("bid_ceiling"):
        tail.append("потолок ставки {:,.0f} ₽".format(float(s["bid_ceiling"])).replace(",", " "))
    if s.get("cpa"):
        tail.append("цена конверсии {:,.0f} ₽".format(float(s["cpa"])).replace(",", " "))
    lines.append(("Стратегия на поиске", nm + (", " + ", ".join(tail) if tail else "")))
    nn = {x["id"]: x["name"] for x in NETWORK_STRATEGIES}.get(
        ((p.get("strategy") or {}).get("network") or {}).get("type"), "—")
    lines.append(("В сетях", nn))
    lines.append(("Модель атрибуции",
                  {x["id"]: x["name"] for x in ATTRIBUTION}.get(p.get("attribution"), "—")))
    db = p.get("daily_budget") or None
    if db and db.get("amount"):
        lines.append(("Дневной бюджет", "{:,.0f} ₽, {}".format(
            float(db["amount"]),
            "распределённый" if db.get("mode") == "DISTRIBUTED" else "стандартный").replace(",", " ")))
    kws = p.get("negative_keywords") or []
    lines.append(("Минус-слова", "{} шт.".format(len(kws)) if kws else "нет"))
    lines.append(("UTM-метка", p.get("tracking_params") or "нет"))
    on = [s_["name"] for s_ in SETTINGS if (p.get("settings") or {}).get(s_["id"]) == "YES"]
    lines.append(("Включённые настройки", ", ".join(on) if on else "нет"))
    for m in (p.get("modifiers") or []):
        lines.append(("Корректировка", modifier_text(m)))
    if p.get("time_targeting"):
        lines.append(("Расписание показов", "своё, из шаблона"))
    if p.get("excluded_sites"):
        lines.append(("Запрещённые площадки", "{} шт.".format(len(p["excluded_sites"]))))
    if client_name:
        lines.append(("Название кампании", campaign_name(p, client_name)))
    if p.get("use_client_counter", True):
        lines.append(("Счётчик Метрики",
                      ", ".join(str(c) for c in counters) if counters
                      else "не нашли у клиента — кампания создастся без счётчика"))
    if p.get("use_client_goals", True):
        if goals:
            val = " по {:,.0f} ₽".format(float(p["goal_value"])).replace(",", " ") \
                if p.get("goal_value") else ""
            lines.append(("Приоритетные цели", ", ".join(goals) + val))
        else:
            lines.append(("Приоритетные цели", "у клиента не отмечены — кампания создастся без них"))
    return lines


def modifier_text(m):
    """«Эксклюзивное размещение +30%» вместо ALONE/130."""
    kind, val = m.get("kind"), m.get("value")
    pct = "" if val is None else (
        "выключить показы" if int(val) == 0 else
        "{:+d}%".format(int(val) - 100) if int(val) != 100 else "без изменений")
    if kind == "serp":
        name = {x["id"]: x["name"] for x in SERP_LAYOUTS}.get(m.get("layout"), m.get("layout"))
    elif kind == "demography":
        age = {x["id"]: x["name"] for x in AGES}.get(m.get("age"), m.get("age"))
        gen = {x["id"]: x["name"] for x in GENDERS}.get(m.get("gender") or "", "")
        name = "Возраст {}{}".format(age, ", " + gen if gen and gen != "любой пол" else "")
    elif kind == "device":
        name = {x["id"]: x["name"] for x in DEVICES}.get(m.get("device"), m.get("device"))
    elif kind == "region":
        name = "Регион {}".format(m.get("region_id"))
    else:
        name = str(kind)
    return "{} — {}".format(name, pct)
