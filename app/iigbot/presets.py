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

# ─────────────────────── справочники: всё из документации и API Директа ───────────────────────
# Названия и описания — дословно из документации Яндекса. Списки допустимых значений получены
# от самого API: на заведомо неверное значение Директ перечисляет то, что принимает. Рядом с
# названием держим код настройки (id) — морда показывает и его, чтобы не было разночтений
# с интерфейсом Директа.

# Стратегии Единой перфоманс-кампании. Полный список — ответ API v501 на неверное значение.
SEARCH_STRATEGIES = [
    {"id": "WB_MAXIMUM_CLICKS", "name": "Максимум кликов", "params": ["weekly", "bid_ceiling"]},
    {"id": "WB_MAXIMUM_CONVERSION_RATE", "name": "Максимум конверсий",
     "params": ["weekly", "bid_ceiling", "goal"], "need": ["goal"]},
    {"id": "HIGHEST_POSITION", "name": "Максимум кликов с ручными ставками", "params": []},
    {"id": "AVERAGE_CPA", "name": "Средняя цена конверсии",
     "params": ["cpa", "weekly", "bid_ceiling", "goal"], "need": ["cpa"]},
    {"id": "AVERAGE_CPC", "name": "Средняя цена клика", "params": ["cpc", "weekly"], "need": ["cpc"]},
    {"id": "AVERAGE_CRR", "name": "Доля рекламных расходов",
     "params": ["crr", "weekly", "goal"], "need": ["crr"]},
    {"id": "PAY_FOR_CONVERSION", "name": "Оплата за конверсии",
     "params": ["cpa", "weekly", "goal"], "need": ["cpa"]},
    {"id": "PAY_FOR_CONVERSION_CRR", "name": "Оплата за конверсии, доля рекламных расходов",
     "params": ["crr", "weekly", "goal"], "need": ["crr"]},
    {"id": "AVERAGE_CPA_MULTIPLE_GOALS", "name": "Средняя цена конверсии по нескольким целям",
     "params": ["cpa", "weekly", "goal"]},
    {"id": "PAY_FOR_CONVERSION_MULTIPLE_GOALS",
     "name": "Оплата за конверсии по нескольким целям", "params": ["cpa", "weekly", "goal"]},
    {"id": "MAX_PROFIT", "name": "Максимум прибыли", "params": ["weekly", "goal"]},
    {"id": "SERVING_OFF", "name": "Показы отключены", "params": []},
]
# В сетях те же стратегии, только вместо ручного управления — NETWORK_DEFAULT.
NETWORK_STRATEGIES = [
    {"id": "NETWORK_DEFAULT", "name": "Как на поиске", "params": []},
    {"id": "WB_MAXIMUM_CLICKS", "name": "Максимум кликов", "params": ["weekly", "bid_ceiling"]},
    {"id": "WB_MAXIMUM_CONVERSION_RATE", "name": "Максимум конверсий",
     "params": ["weekly", "bid_ceiling", "goal"]},
    {"id": "AVERAGE_CPA", "name": "Средняя цена конверсии", "params": ["cpa", "weekly", "goal"]},
    {"id": "AVERAGE_CPC", "name": "Средняя цена клика", "params": ["cpc", "weekly"]},
    {"id": "AVERAGE_CRR", "name": "Доля рекламных расходов", "params": ["crr", "weekly", "goal"]},
    {"id": "PAY_FOR_CONVERSION", "name": "Оплата за конверсии", "params": ["cpa", "weekly", "goal"]},
    {"id": "PAY_FOR_CONVERSION_CRR", "name": "Оплата за конверсии, доля рекламных расходов",
     "params": ["crr", "weekly", "goal"]},
    {"id": "AVERAGE_CPA_MULTIPLE_GOALS", "name": "Средняя цена конверсии по нескольким целям",
     "params": ["cpa", "weekly", "goal"]},
    {"id": "PAY_FOR_CONVERSION_MULTIPLE_GOALS",
     "name": "Оплата за конверсии по нескольким целям", "params": ["cpa", "weekly", "goal"]},
    {"id": "MAX_PROFIT", "name": "Максимум прибыли", "params": ["weekly", "goal"]},
    {"id": "SERVING_OFF", "name": "Показы отключены", "params": []},
]
# Дневной бюджет Директ принимает только с ручными ставками: на автоматических стратегиях
# бюджетом управляет сама стратегия.
MANUAL_SEARCH = {"HIGHEST_POSITION"}

# Настройки кампании. src говорит, откуда взята подпись:
#   "ui"  — так настройка называется в интерфейсе Директа (проверено по справке);
#   "api" — дословное описание из приложения «Настройки кампаний (параметр Option)»:
#           подпись интерфейса в документации не приводится, а выдумывать её нельзя.
# Морда показывает пометку у вторых, чтобы никто не принял описание за название.
SETTINGS = [
    {"id": "ENABLE_SITE_MONITORING", "name": "Мониторинг сайта", "src": "ui", "default": "YES"},
    {"id": "ENABLE_AREA_OF_INTEREST_TARGETING", "name": "Расширенный географический таргетинг",
     "src": "ui", "default": "YES"},
    {"id": "ALTERNATIVE_TEXTS_ENABLED", "name": "Оптимизировать текст объявлений",
     "src": "ui", "default": "NO"},
    {"id": "ADD_METRICA_TAG", "name": "Автоматически добавлять в ссылку объявления метку yclid",
     "src": "api", "default": "YES"},
    {"id": "ENABLE_COMPANY_INFO",
     "name": "При показе на Яндекс Картах добавлять информацию об организации",
     "src": "api", "default": "YES"},
    {"id": "CAMPAIGN_EXACT_PHRASE_MATCHING_ENABLED",
     "name": "Включает отбор фразы по точности соответствия", "src": "api", "default": "NO"},
    {"id": "ADD_TO_FAVORITES", "name": "Добавить кампанию в самые важные для применения фильтра",
     "src": "api", "default": "NO"},
    {"id": "REQUIRE_SERVICING", "name": "Перевести кампанию на обслуживание персональным менеджером",
     "src": "api", "default": "NO"},
    # Этих двух нет ни в приложении к документации, ни в справке — только коды из ответа API.
    {"id": "ENABLE_CURRENT_AREA_TARGETING", "name": "ENABLE_CURRENT_AREA_TARGETING",
     "src": "code", "default": "NO"},
    {"id": "ENABLE_REGULAR_AREA_TARGETING", "name": "ENABLE_REGULAR_AREA_TARGETING",
     "src": "code", "default": "NO"},
]

# Модели атрибуции — полный список из ответа API.
ATTRIBUTION = [
    {"id": "AUTO", "name": "Автоматическая"},
    {"id": "LSCCD", "name": "Последний значимый переход, кросс-девайс"},
    {"id": "LSC", "name": "Последний значимый переход"},
    {"id": "LC", "name": "Последний переход"},
    {"id": "FC", "name": "Первый переход"},
    {"id": "FCCD", "name": "Первый переход, кросс-девайс"},
    {"id": "LYDC", "name": "Последний переход из Яндекс Директа"},
    {"id": "LYDCCD", "name": "Последний переход из Яндекс Директа, кросс-девайс"},
]

# Возраст и пол — перечисления из ответа API.
AGES = [
    {"id": "AGE_0_17", "name": "младше 18 лет"},
    {"id": "AGE_18_24", "name": "18—24"},
    {"id": "AGE_25_34", "name": "25—34"},
    {"id": "AGE_35_44", "name": "35—44"},
    {"id": "AGE_45", "name": "45 и старше"},
    {"id": "AGE_45_54", "name": "45—54"},
    {"id": "AGE_55", "name": "55 и старше"},
]
GENDERS = [
    {"id": "", "name": "любой"},
    {"id": "GENDER_MALE", "name": "мужской"},
    {"id": "GENDER_FEMALE", "name": "женский"},
]
# Устройства. Описания — из справочника объектов BidModifier.
DEVICES = [
    {"id": "mobile", "name": "На смартфонах", "field": "MobileAdjustment",
     "type": "MOBILE_ADJUSTMENT"},
    {"id": "desktop", "name": "На всех устройствах, кроме смартфонов",
     "field": "DesktopAdjustment", "type": "DESKTOP_ADJUSTMENT"},
    {"id": "desktop_only", "name": "Только на ПК", "field": "DesktopOnlyAdjustment",
     "type": "DESKTOP_ONLY_ADJUSTMENT"},
    {"id": "tablet", "name": "На планшетах", "field": "TabletAdjustment",
     "type": "TABLET_ADJUSTMENT"},
    {"id": "smarttv", "name": "На Smart TV", "field": "SmartTvAdjustment",
     "type": "SMARTTV_ADJUSTMENT"},
]
MOBILE_OS = [
    {"id": "", "name": "любая"},
    {"id": "IOS", "name": "iOS"},
    {"id": "ANDROID", "name": "Android"},
]
SERP_LAYOUTS = [
    {"id": "ALONE", "name": "Эксклюзивное размещение"},
    {"id": "SUGGEST", "name": "Продвижение в саджесте"},
]
INCOME_GRADES = [
    {"id": "VERY_HIGH", "name": "VERY_HIGH"},
    {"id": "HIGH", "name": "HIGH"},
    {"id": "ABOVE_AVERAGE", "name": "ABOVE_AVERAGE"},
]

# Виды корректировок, которые переносятся с аккаунта на аккаунт. Ретаргетинг, фильтры,
# производители и LTV сюда не входят намеренно: они ссылаются на объекты конкретного
# аккаунта (условие ретаргетинга, фильтр, вендор) и на другом аккаунте не существуют.
MODIFIER_KINDS = [
    {"kind": "serp", "name": "При эксклюзивном размещении объявлений или размещении "
                             "продвижения в саджесте", "short": "Поисковое размещение",
     "options": SERP_LAYOUTS, "min": 1, "max": 1300},
    {"kind": "demography", "name": "По полу и возрасту", "short": "Пол и возраст",
     "ages": AGES, "genders": GENDERS, "min": 0, "max": 1300},
    {"kind": "device", "name": "По типу устройства", "short": "Устройства",
     "options": DEVICES, "os": MOBILE_OS, "min": 0, "max": 1300},
    {"kind": "region", "name": "Набор регионов и коэффициентов к ставке", "short": "Регион показа",
     "min": 10, "max": 1300},
    {"kind": "income", "name": "При показе пользователям с определённым уровнем "
                               "платежеспособности", "short": "Платежеспособность",
     "options": INCOME_GRADES, "min": 1, "max": 1300},
    {"kind": "video", "name": "При показе объявлений с видеодополнением",
     "short": "Видеодополнения", "min": 50, "max": 1300},
]
# Чего в списке нет и почему. Первые две Директ отклоняет для Единой перфоманс-кампании
# («Корректировка данного типа не поддерживается» — проверено), остальные ссылаются на
# объекты конкретного аккаунта и на другом аккаунте не существуют.
NOT_SUPPORTED = [
    {"name": "Отключение показов по IP-адресам",
     "why": "Директ отвечает «Поле задано неверно: BlockedIps» на любой адрес — в ЕПК не работает"},
    {"name": "Предупреждать при остатке (WarningBalance)",
     "why": "Директ: «Поле не поддерживается в кампании заданного типа»"},
    {"name": "Получать предупреждения (SendWarnings)",
     "why": "Директ: «Поле не поддерживается в кампании заданного типа»"},
]

MODIFIERS_NOT_PORTABLE = [
    {"name": "Для товарных предложений (смарт-объявления)",
     "why": "Директ отвечает «Корректировка данного типа не поддерживается» для ЕПК"},
    {"name": "По погоде",
     "why": "Директ отвечает «Корректировка данного типа не поддерживается» для ЕПК"},
    {"name": "Набор условий ретаргетинга и подбора аудитории",
     "why": "условия ретаргетинга принадлежат конкретному аккаунту"},
    {"name": "По фильтру товарных предложений", "why": "фильтр принадлежит конкретному аккаунту"},
    {"name": "По производителю", "why": "список производителей свой у каждого аккаунта"},
    {"name": "По ценности покупателя (LTV)", "why": "считается по данным конкретного аккаунта"},
]

# Что подставляется при применении, а не хранится в шаблоне.
SUBSTITUTIONS = [
    {"id": "counter", "name": "Счётчики Метрики", "from": "берутся из кампаний клиента"},
    {"id": "goals", "name": "Ключевые цели", "from": "цели клиента, отмеченные в кабинете"},
    {"id": "name", "name": "Название кампании", "from": "маска шаблона + имя клиента"},
    {"id": "start", "name": "Дата начала кампании", "from": "сегодня"},
]


def spec(labels=None):
    """Описание всех полей шаблона — по нему морда рисует форму.

    Держим справочники на сервере, а не в разметке: иначе список стратегий пришлось бы
    править в двух местах и они бы разъехались.

    labels — свои подписи настроек, заданные в кабинете. Часть настроек справка Директа
    дословно не называет, и вместо выдумки мы показываем описание из справочника API,
    помечая это. Кто видит интерфейс, может подписать точно — и подпись станет общей.
    """
    labels = labels or {}
    settings = []
    for x in SETTINGS:
        item = dict(x)
        if labels.get(x["id"]):
            item["name"] = labels[x["id"]]
            item["src"] = "own"
        settings.append(item)
    return {
        "search_strategies": SEARCH_STRATEGIES,
        "network_strategies": NETWORK_STRATEGIES,
        "settings": settings,
        "attribution": ATTRIBUTION,
        "modifier_kinds": MODIFIER_KINDS,
        "not_portable": MODIFIERS_NOT_PORTABLE, "not_supported": NOT_SUPPORTED,
        "ages": AGES, "genders": GENDERS, "devices": DEVICES, "serp_layouts": SERP_LAYOUTS,
        "income_grades": INCOME_GRADES, "mobile_os": MOBILE_OS,
        "substitutions": SUBSTITUTIONS,
    }


def blank():
    """Пустой шаблон со здравыми умолчаниями — с него начинается создание нового."""
    return {
        "strategy": {"search": {"type": "WB_MAXIMUM_CLICKS", "weekly": 3000, "bid_ceiling": None},
                     "network": {"type": "NETWORK_DEFAULT"}},
        "settings": {x["id"]: x["default"] for x in SETTINGS},
        "attribution": "AUTO",
        "negative_keywords": [],
        "negative_keyword_set_ids": [],
        "tracking_params": ("utm_source=YandexDirect&utm_medium=cpc&utm_campaign={campaign_id}"
                            "&utm_content={ad_id}&utm_term={keyword}"),
        "excluded_sites": [],
        "time_zone": "Europe/Moscow",
        "end_after_days": None,
        "daily_budget": None,
        "notification": None,
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

    got = {x.get("Option"): x.get("Value") for x in (uni.get("Settings") or [])}
    p["settings"] = {x["id"]: got.get(x["id"], x["default"]) for x in SETTINGS}

    p["attribution"] = uni.get("AttributionModel") or "AUTO"
    p["tracking_params"] = uni.get("TrackingParams") or ""
    p["negative_keywords"] = list((camp.get("NegativeKeywords") or {}).get("Items") or [])
    p["negative_keyword_set_ids"] = list(
        (uni.get("NegativeKeywordSharedSetIds") or {}).get("Items") or [])
    p["excluded_sites"] = list((camp.get("ExcludedSites") or {}).get("Items") or [])
    p["time_zone"] = camp.get("TimeZone") or "Europe/Moscow"

    db = camp.get("DailyBudget") or None
    if db and db.get("Amount"):
        p["daily_budget"] = {"amount": round(db["Amount"] / MICRO, 2),
                             "mode": db.get("Mode") or "STANDARD"}

    # Дату окончания переносим как «через сколько дней после старта»: дата эталона к новой
    # кампании отношения не имеет, а срок «на месяц» — имеет.
    if camp.get("EndDate") and camp.get("StartDate"):
        try:
            d1 = dt.date.fromisoformat(camp["StartDate"])
            d2 = dt.date.fromisoformat(camp["EndDate"])
            if d2 > d1:
                p["end_after_days"] = (d2 - d1).days
        except (TypeError, ValueError):
            pass

    n = camp.get("Notification") or {}
    em, sms = n.get("EmailSettings") or {}, n.get("SmsSettings") or {}
    if em.get("Email") or sms.get("SmsEnabled"):
        p["notification"] = {
            "email": em.get("Email") or "",
            "send_account_news": em.get("SendAccountNews") or "NO",
        }

    tt = camp.get("TimeTargeting") or None
    if tt and not _schedule_is_full(tt):
        p["time_targeting"] = tt

    goals = ((uni.get("PriorityGoals") or {}).get("Items")) or []
    if goals and goals[0].get("Value"):
        p["goal_value"] = round(goals[0]["Value"] / MICRO, 2)

    p["modifiers"] = [m for m in (_modifier_in(x) for x in (modifiers or [])) if m]
    p["name_mask"] = "{клиент} — " + (camp.get("Name") or "").strip()
    return p


def _strategy_in(node, network=False):
    """Стратегия из ответа API → в наш плоский вид."""
    node = node or {}
    t = node.get("BiddingStrategyType") or ("NETWORK_DEFAULT" if network else "WB_MAXIMUM_CLICKS")
    out = {"type": t}
    for key in ("WbMaximumClicks", "WbMaximumConversionRate", "AverageCpa", "AverageCpc",
                "AverageCrr", "PayForConversion", "PayForConversionCrr", "MaxProfit",
                "AverageCpaMultipleGoals", "PayForConversionMultipleGoals"):
        d = node.get(key)
        if not isinstance(d, dict):
            continue
        if d.get("WeeklySpendLimit"):
            out["weekly"] = round(d["WeeklySpendLimit"] / MICRO, 2)
        if d.get("BidCeiling"):
            out["bid_ceiling"] = round(d["BidCeiling"] / MICRO, 2)
        if d.get("AverageCpa") or d.get("Cpa"):
            out["cpa"] = round((d.get("AverageCpa") or d.get("Cpa")) / MICRO, 2)
        if d.get("AverageCpc"):
            out["cpc"] = round(d["AverageCpc"] / MICRO, 2)
        if d.get("Crr"):
            out["crr"] = d["Crr"]
    return out


def _modifier_in(m):
    """Корректировка из ответа API → в наш вид. Непереносимые (ретаргетинг, фильтры,
    производители, LTV) пропускаем: они ссылаются на объекты конкретного аккаунта."""
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
    if t == "INCOME_GRADE_ADJUSTMENT":
        d = m.get("IncomeGradeAdjustment") or {}
        return {"kind": "income", "grade": d.get("Grade"), "value": d.get("BidModifier")}
    if t == "VIDEO_ADJUSTMENT":
        return {"kind": "video", "value": (m.get("VideoAdjustment") or {}).get("BidModifier")}
    for dev in DEVICES:
        if t == dev["type"]:
            d = m.get(dev["field"]) or {}
            out = {"kind": "device", "device": dev["id"], "value": d.get("BidModifier")}
            if d.get("OperatingSystemType"):
                out["os"] = d["OperatingSystemType"]
            return out
    return None


def _schedule_is_full(tt):
    """Расписание «круглосуточно всю неделю» — значит его не задавали."""
    items = ((tt or {}).get("Schedule") or {}).get("Items") or []
    if len(items) != 7:
        return False
    for row in items:
        if set(str(row).split(",")[1:]) != {"100"}:
            return False
    return True


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
    day = today or dt.date.today()
    uni = {}

    strat = p.get("strategy") or {}
    uni["BiddingStrategy"] = {
        "Search": _strategy_out(strat.get("search") or {}, goal_ids),
        "Network": _strategy_out(strat.get("network") or {"type": "NETWORK_DEFAULT"}, goal_ids,
                                 network=True),
    }

    st = p.get("settings") or {}
    uni["Settings"] = [{"Option": x["id"], "Value": st.get(x["id"], x["default"])}
                       for x in SETTINGS]

    if p.get("attribution"):
        uni["AttributionModel"] = p["attribution"]
    if p.get("tracking_params"):
        uni["TrackingParams"] = p["tracking_params"]
    if p.get("negative_keyword_set_ids"):
        uni["NegativeKeywordSharedSetIds"] = {
            "Items": [int(x) for x in p["negative_keyword_set_ids"][:3]]}

    if p.get("use_client_counter", True) and counters:
        uni["CounterIds"] = {"Items": [int(c) for c in counters]}

    if p.get("use_client_goals", True) and goal_ids and p.get("goal_value"):
        uni["PriorityGoals"] = {"Items": [
            {"GoalId": int(g), "Value": int(round(float(p["goal_value"]) * MICRO)),
             "IsMetrikaSourceOfValue": "NO"} for g in goal_ids]}

    camp = {
        "Name": campaign_name(p, client_name, custom_name),
        "StartDate": day.isoformat(),
        "UnifiedCampaign": uni,
    }
    if p.get("end_after_days"):
        camp["EndDate"] = (day + dt.timedelta(days=int(p["end_after_days"]))).isoformat()
    if p.get("time_zone"):
        camp["TimeZone"] = p["time_zone"]
    if p.get("negative_keywords"):
        camp["NegativeKeywords"] = {"Items": list(p["negative_keywords"])}
    if p.get("excluded_sites"):
        camp["ExcludedSites"] = {"Items": list(p["excluded_sites"])}
    if p.get("time_targeting"):
        # Директ отдаёт незаполненные поля как null, но на запись их не принимает
        # («HolidaysSchedule не может иметь значение null») — вычищаем пустоту.
        camp["TimeTargeting"] = _drop_nulls(copy.deepcopy(p["time_targeting"]))
    db = p.get("daily_budget") or None
    if db and db.get("amount"):
        camp["DailyBudget"] = {"Amount": int(round(float(db["amount"]) * MICRO)),
                               "Mode": db.get("mode") or "STANDARD"}
    # Уведомления: у ЕПК Директ принимает только адрес и новости аккаунта — на остальные
    # поля отвечает «не поддерживается в кампании заданного типа».
    n = p.get("notification") or None
    if n and n.get("email"):
        camp["Notification"] = {"EmailSettings": {
            "Email": n["email"], "SendAccountNews": n.get("send_account_news") or "NO"}}
    return camp


def _drop_nulls(node):
    """Убрать пустые значения из вложенной структуры перед отправкой в Директ."""
    if isinstance(node, dict):
        return {k: _drop_nulls(v) for k, v in node.items() if v is not None}
    if isinstance(node, list):
        return [_drop_nulls(v) for v in node if v is not None]
    return node


# Как называется вложенный объект параметров у каждой стратегии.
_STRATEGY_BOX = {
    "WB_MAXIMUM_CLICKS": "WbMaximumClicks",
    "WB_MAXIMUM_CONVERSION_RATE": "WbMaximumConversionRate",
    "AVERAGE_CPA": "AverageCpa", "AVERAGE_CPC": "AverageCpc", "AVERAGE_CRR": "AverageCrr",
    "PAY_FOR_CONVERSION": "PayForConversion", "PAY_FOR_CONVERSION_CRR": "PayForConversionCrr",
    "MAX_PROFIT": "MaxProfit", "AVERAGE_CPA_MULTIPLE_GOALS": "AverageCpaMultipleGoals",
    "PAY_FOR_CONVERSION_MULTIPLE_GOALS": "PayForConversionMultipleGoals",
}
# Оплата за конверсии называет цену Cpa, остальные — AverageCpa.
_CPA_FIELD = {"PAY_FOR_CONVERSION": "Cpa", "PAY_FOR_CONVERSION_MULTIPLE_GOALS": "Cpa"}


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
        box[_CPA_FIELD.get(t, "AverageCpa")] = int(round(float(s["cpa"]) * MICRO))
    if s.get("cpc"):
        box["AverageCpc"] = int(round(float(s["cpc"]) * MICRO))
    if s.get("crr"):
        box["Crr"] = int(s["crr"])
    if goal_ids and t in ("WB_MAXIMUM_CONVERSION_RATE", "AVERAGE_CPA", "AVERAGE_CRR",
                          "PAY_FOR_CONVERSION", "PAY_FOR_CONVERSION_CRR", "MAX_PROFIT"):
        box["GoalId"] = int(goal_ids[0])
    key = _STRATEGY_BOX.get(t)
    if key and box:
        out[key] = box
    return out


def modifiers_payload(preset, campaign_id):
    """Корректировки к запросу bidmodifiers.add.

    В одном элементе допустима ровно одна корректировка: Директ отвечает ошибкой 5009,
    если указать две. Поэтому каждый вид уходит отдельным элементом.
    """
    cid = int(campaign_id)
    out, serp, demo, region, income = [], [], [], [], []
    dev_field = {d["id"]: d["field"] for d in DEVICES}
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
            demo.append(item)
        elif kind == "region" and m.get("region_id"):
            region.append({"RegionId": int(m["region_id"]), "BidModifier": val})
        elif kind == "income" and m.get("grade"):
            income.append({"Grade": m["grade"], "BidModifier": val})
        elif kind == "device" and dev_field.get(m.get("device")):
            body = {"BidModifier": val}
            if m.get("device") == "mobile" and m.get("os"):
                body["OperatingSystemType"] = m["os"]
            out.append({"CampaignId": cid, dev_field[m["device"]]: body})
        elif kind == "video":
            out.append({"CampaignId": cid, "VideoAdjustment": {"BidModifier": val}})
    if serp:
        out.append({"CampaignId": cid, "SerpLayoutAdjustments": serp})
    if demo:
        out.append({"CampaignId": cid, "DemographicsAdjustments": demo})
    if region:
        out.append({"CampaignId": cid, "RegionalAdjustments": region})
    if income:
        out.append({"CampaignId": cid, "IncomeGradeAdjustments": income})
    return out


# ─────────────────────────── проверки и предпросмотр ───────────────────────────
def validate(preset):
    """Что не так с шаблоном. Пустой список — можно применять."""
    p = preset or {}
    bad = []
    s = (p.get("strategy") or {}).get("search") or {}
    known = {x["id"]: x for x in SEARCH_STRATEGIES}
    if s.get("type") not in known:
        bad.append("Не выбрана стратегия")
    else:
        need = known[s["type"]].get("need") or []
        titles = {"cpa": "цена конверсии", "cpc": "цена клика", "crr": "доля рекламных расходов",
                  "goal": "ключевая цель"}
        for f in need:
            if f == "goal":
                continue      # цель подставляется от клиента при применении
            if not s.get(f):
                bad.append("Для стратегии «{}» нужна {}".format(known[s["type"]]["name"],
                                                                titles.get(f, f)))
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
        bad.append("Минус-фраз больше 1000")
    if len(p.get("negative_keyword_set_ids") or []) > 3:
        bad.append("Наборов минус-фраз можно указать не больше трёх")
    if len(p.get("excluded_sites") or []) > 1000:
        bad.append("Площадок можно указать не больше 1000")
    limits = {x["kind"]: x for x in MODIFIER_KINDS}
    for m in (p.get("modifiers") or []):
        lim = limits.get(m.get("kind"))
        v = m.get("value")
        if not lim or v is None:
            bad.append("Корректировка задана не полностью")
            break
        if not (lim["min"] <= int(v) <= lim["max"]):
            bad.append("«{}»: коэффициент вне диапазона {}–{}%".format(
                lim["short"], lim["min"], lim["max"]))
            break
    db = p.get("daily_budget") or None
    if db and db.get("amount") and s.get("type") not in MANUAL_SEARCH:
        bad.append("Дневной бюджет Директ принимает только со стратегией "
                   "«Максимум кликов с ручными ставками»")
    if db and db.get("amount") and float(db["amount"]) < 300:
        bad.append("Дневной бюджет меньше 300 ₽ — Директ такой не примет")
    if p.get("goal_value") is not None and float(p.get("goal_value") or 0) < 0:
        bad.append("Ценность конверсии не может быть отрицательной")
    if not (p.get("name_mask") or "").strip():
        bad.append("Пустая маска названия кампании")
    return bad


def _money(v):
    return "{:,.0f} ₽".format(float(v)).replace(",", " ")


def describe(preset, client_name=None, counters=None, goals=None):
    """Что уйдёт в Директ — названиями Директа. Для экрана подтверждения."""
    p = preset or {}
    lines = []
    s = (p.get("strategy") or {}).get("search") or {}
    nm = {x["id"]: x["name"] for x in SEARCH_STRATEGIES}.get(s.get("type"), s.get("type") or "—")
    tail = []
    if s.get("weekly"):
        tail.append("недельный бюджет " + _money(s["weekly"]))
    if s.get("bid_ceiling"):
        tail.append("ограничение ставки " + _money(s["bid_ceiling"]))
    if s.get("cpa"):
        tail.append("цена конверсии " + _money(s["cpa"]))
    if s.get("cpc"):
        tail.append("цена клика " + _money(s["cpc"]))
    if s.get("crr"):
        tail.append("доля рекламных расходов {}%".format(s["crr"]))
    lines.append(("Стратегия", nm + (", " + ", ".join(tail) if tail else "")))
    lines.append(("Стратегия в сетях",
                  {x["id"]: x["name"] for x in NETWORK_STRATEGIES}.get(
                      ((p.get("strategy") or {}).get("network") or {}).get("type"), "—")))
    db = p.get("daily_budget") or None
    if db and db.get("amount"):
        lines.append(("Дневной бюджет", _money(db["amount"]) + (
            ", распределённый режим" if db.get("mode") == "DISTRIBUTED" else ", стандартный режим")))
    lines.append(("Модель атрибуции",
                  {x["id"]: x["name"] for x in ATTRIBUTION}.get(p.get("attribution"), "—")))
    kws = p.get("negative_keywords") or []
    lines.append(("Минус-фразы", "{} шт.".format(len(kws)) if kws else "нет"))
    if p.get("negative_keyword_set_ids"):
        lines.append(("Наборы минус-фраз",
                      ", ".join(str(x) for x in p["negative_keyword_set_ids"])))
    lines.append(("Параметры URL", p.get("tracking_params") or "нет"))
    on = [x["name"] for x in SETTINGS if (p.get("settings") or {}).get(x["id"]) == "YES"]
    lines.append(("Включённые настройки", "; ".join(on) if on else "нет"))
    for m in (p.get("modifiers") or []):
        lines.append(("Корректировка", modifier_text(m)))
    if p.get("time_targeting"):
        lines.append(("Расписание показов", "своё, из шаблона"))
    if p.get("excluded_sites"):
        lines.append(("Площадки, на которых запрещены показы",
                      "{} шт.".format(len(p["excluded_sites"]))))
    if p.get("end_after_days"):
        lines.append(("Дата окончания кампании",
                      "через {} дн. после старта".format(p["end_after_days"])))
    n = p.get("notification") or None
    if n and n.get("email"):
        lines.append(("Почтовые уведомления", n["email"] + (
            ", новости аккаунта" if n.get("send_account_news") == "YES" else "")))
    if p.get("time_zone"):
        lines.append(("Часовой пояс", p["time_zone"]))
    if client_name:
        lines.append(("Название кампании", campaign_name(p, client_name)))
    if p.get("use_client_counter", True):
        lines.append(("Счётчики Метрики",
                      ", ".join(str(c) for c in counters) if counters
                      else "не нашли у клиента — кампания создастся без счётчика"))
    if p.get("use_client_goals", True):
        if goals:
            val = ", ценность " + _money(p["goal_value"]) if p.get("goal_value") else ""
            lines.append(("Ключевые цели", ", ".join(goals) + val))
        else:
            lines.append(("Ключевые цели", "у клиента не отмечены — кампания создастся без них"))
    return lines


def modifier_text(m):
    """«Эксклюзивное размещение +30%» вместо ALONE/130."""
    kind, val = m.get("kind"), m.get("value")
    pct = "" if val is None else (
        "показы выключены" if int(val) == 0 else
        "{:+d}%".format(int(val) - 100) if int(val) != 100 else "без изменений")
    if kind == "serp":
        name = {x["id"]: x["name"] for x in SERP_LAYOUTS}.get(m.get("layout"), m.get("layout"))
    elif kind == "demography":
        age = {x["id"]: x["name"] for x in AGES}.get(m.get("age"), m.get("age"))
        gen = {x["id"]: x["name"] for x in GENDERS}.get(m.get("gender") or "", "")
        name = "Возраст {}{}".format(age, ", пол " + gen if gen and gen != "любой" else "")
    elif kind == "device":
        name = {x["id"]: x["name"] for x in DEVICES}.get(m.get("device"), m.get("device"))
        if m.get("os"):
            name += ", " + {x["id"]: x["name"] for x in MOBILE_OS}.get(m["os"], m["os"])
    elif kind == "region":
        name = "Регион {}".format(m.get("region_id"))
    elif kind == "income":
        name = "Платежеспособность {}".format(m.get("grade"))
    elif kind == "video":
        name = "Показ с видеодополнением"
    else:
        name = str(kind)
    return "{} — {}".format(name, pct)
