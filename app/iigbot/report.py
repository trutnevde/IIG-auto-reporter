# -*- coding: utf-8 -*-
"""Движок отчётов — порт логики weekly_report.ps1 на Python.

Тянет статистику ПО КАМПАНИЯМ за прошлую неделю (расход с НДС), сегментирует конверсии
по целям Метрики, исключает кампании без активности за 4 недели, помечает кампании без
расхода за прошлую неделю и формирует сообщение (итог по аккаунту + разбивка по кампаниям).

Данные клиента (цели, атрибуция) и привязки чатов берутся из локальной базы (storage).
"""
import json
import time
from datetime import date, timedelta

import requests

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"


# ---------- форматирование чисел в русском стиле (пробел — тысячи, запятая — дробь) ----------
def fmt_money(x):
    s = "{:,.2f}".format(float(x))
    intpart, dec = s.split(".")
    return "{},{} ₽".format(intpart.replace(",", " "), dec)


def fmt_int(x):
    return "{:,}".format(int(round(float(x)))).replace(",", " ")


def fmt_pct(x):
    s = "{:,.2f}".format(float(x))
    intpart, dec = s.split(".")
    return "{},{}%".format(intpart.replace(",", " "), dec)


def parse_num(s):
    """Число из ячейки отчёта. Что не разобралось — ноль.

    Пробелы внутри числа вычищаем, включая неразрывный: сегодня Директ отдаёт
    «1234.56» без разделителей, но если формат когда-нибудь сменится, мы получим
    не ошибку, а тихий ноль в расходе — а это худший вид ошибки в отчёте клиенту.
    """
    if s is None:
        return 0.0
    t = str(s).replace(",", ".").strip()
    if t in ("", "--"):
        return 0.0
    t = t.replace(" ", "").replace(" ", "").replace(" ", "")
    try:
        return float(t)
    except ValueError:
        return 0.0


# ---------- период: прошлая неделя (Пн..Вс) и окно простоя 4 недели ----------
def period(today=None):
    today = today or date.today()
    delta = today.weekday()  # Пн=0 .. Вс=6 — совпадает с расчётом в PowerShell
    last_monday = today - timedelta(days=delta + 7)
    last_sunday = last_monday + timedelta(days=6)
    month_from = last_sunday - timedelta(days=27)
    return {
        "date_from": last_monday.isoformat(),
        "date_to": last_sunday.isoformat(),
        "month_from": month_from.isoformat(),
        "month_to": last_sunday.isoformat(),
        "label_from": last_monday.strftime("%d.%m.%Y"),
        "label_to": last_sunday.strftime("%d.%m.%Y"),
    }


# ---------- запрос отчёта (CAMPAIGN_PERFORMANCE_REPORT, TSV) ----------
# ───────────────────────── кэш выгрузок Reports API ─────────────────────────
# Директ ставит каждый отчёт в очередь: один и тот же запрос — это снова десятки
# секунд ожидания. Держим ответы в памяти процесса: пересборка досье за тот же
# период или повторный конструктор отвечают мгновенно и не жгут лимиты.
_CACHE = {}                 # ключ -> (время, строки)
_CACHE_TTL = 900            # период включает сегодня: данные ещё набегают
_CACHE_TTL_CLOSED = 6 * 3600  # период закрыт (кончился вчера или раньше) — меняться нечему
_CACHE_MAX = 40             # чтобы память процесса не росла бесконечно

# Внешнее хранилище кэша (обычно база): пережить перезапуск процесса.
# Ставится один раз при старте приложения; None — работаем только в памяти.
CACHE_STORE = None          # объект с cache_get(key, max_age) / cache_put(key, value)


def set_cache_store(store):
    global CACHE_STORE
    CACHE_STORE = store


def _cache_key(token, login, date_from, date_to, fields, goal_ids, attribution,
               report_type, filters):
    return (login, date_from, date_to, tuple(fields), tuple(goal_ids or ()),
            attribution, report_type, json.dumps(filters, sort_keys=True) if filters else "")


def _store_key(ckey):
    """Ключ для внешнего хранилища: кортеж → короткая строка."""
    import hashlib
    return "rep:" + hashlib.sha1(repr(ckey).encode("utf-8")).hexdigest()


def cache_clear():
    """Сбросить кэш выгрузок (после смены целей клиента, например)."""
    _CACHE.clear()
    if CACHE_STORE is not None:
        try:
            CACHE_STORE.cache_clear()
        except Exception:  # noqa: BLE001
            pass


def _ttl_for(date_to):
    """Сколько держать выгрузку: закрытый период уже не изменится."""
    try:
        from datetime import date as _dt
        y, m, d = (int(x) for x in str(date_to)[:10].split("-"))
        return _CACHE_TTL if _dt(y, m, d) >= _dt.today() else _CACHE_TTL_CLOSED
    except Exception:  # noqa: BLE001
        return _CACHE_TTL


def _cache_get(key, now, ttl=None):
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, rows = hit
    if now - ts > (ttl or _CACHE_TTL):
        _CACHE.pop(key, None)
        return None
    return rows


def _cache_put(key, rows, now):
    if len(_CACHE) >= _CACHE_MAX:                 # выкидываем самую старую запись
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (now, rows)


def fetch_report(token, login, date_from, date_to, fields, goal_ids=None, attribution="LSCCD",
                 report_type="CAMPAIGN_PERFORMANCE_REPORT", filters=None, _post=None, _sleep=None):
    """Возвращает список строк-словарей (имя_столбца -> значение). _post/_sleep — для тестов.

    filters — список фильтров SelectionCriteria, напр. [{"Field":"CampaignId","Operator":"IN","Values":["123"]}].
    """
    post = _post or requests.post
    sleep = _sleep or time.sleep
    headers = {
        "Authorization": "Bearer " + token,
        "Client-Login": login,
        "Accept-Language": "ru",
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
        "skipReportHeader": "true",
        "skipReportSummary": "true",
        "Content-Type": "application/json; charset=utf-8",
    }
    selection = {"DateFrom": date_from, "DateTo": date_to}
    if filters:
        selection["Filter"] = list(filters)
    params = {
        "SelectionCriteria": selection,
        "FieldNames": list(fields),
        "ReportName": "wk_{}_{}".format(login, int(time.time() * 1000)),
        "ReportType": report_type,
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        "IncludeVAT": "YES",       # расход — с НДС
        "IncludeDiscount": "NO",
    }
    if goal_ids:
        params["Goals"] = list(goal_ids)
        params["AttributionModels"] = [attribution]
    body = json.dumps({"params": params}).encode("utf-8")

    ckey = _cache_key(token, login, date_from, date_to, fields, goal_ids, attribution,
                      report_type, filters)
    now = time.time()
    ttl = _ttl_for(date_to)
    cached = _cache_get(ckey, now, ttl)
    if cached is not None:
        return cached
    if CACHE_STORE is not None:                 # переживает перезапуск процесса
        try:
            raw = CACHE_STORE.cache_get(_store_key(ckey), ttl)
            if raw:
                rows = json.loads(raw)
                _cache_put(ckey, rows, now)     # поднимаем и в память — там быстрее
                return rows
        except Exception:  # noqa: BLE001 — кэш не обязан работать
            pass

    for attempt in range(12):
        r = post(REPORTS_URL, data=body, headers=headers, timeout=120)
        # Директ не всегда шлёт charset, и тогда requests читает ответ как latin-1 —
        # русские тексты ошибок превращались в кракозябры прямо в журнале
        if not getattr(r, "encoding", None) or str(r.encoding).lower() in ("iso-8859-1", "latin-1"):
            try:
                r.encoding = "utf-8"
            except Exception:  # noqa: BLE001
                pass
        if r.status_code == 200:
            rows = _parse_tsv(r.text)
            _cache_put(ckey, rows, now)
            if CACHE_STORE is not None:
                try:
                    CACHE_STORE.cache_put(_store_key(ckey), json.dumps(rows, ensure_ascii=False))
                except Exception:  # noqa: BLE001
                    pass
            return rows
        if r.status_code in (201, 202):
            # Директ ставит отчёт в очередь. Раньше ждали ровно столько, сколько
            # он сказал, и долбились с той же частотой; теперь пауза подрастает.
            wait = r.headers.get("retryIn", "5")
            try:
                wait = int(wait)
            except (TypeError, ValueError):
                wait = 5
            sleep(min(30, max(wait, 1) + attempt))
            continue
        detail = (r.text or "")[:500]
        try:
            j = r.json()
            err = j.get("error") if isinstance(j, dict) else None
            if err:
                detail = (str(err.get("error_string") or "") + ". " + str(err.get("error_detail") or "")).strip(" .")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError("Reports API {}: {}".format(r.status_code, detail))
    raise RuntimeError("Отчёт не готов за отведённое время (login {})".format(login))


def _parse_tsv(text):
    lines = [ln for ln in (text or "").split("\n") if ln.strip() != ""]
    if len(lines) < 2:
        return []  # только заголовок -> данных за период нет
    header = [h.strip() for h in lines[0].split("\t")]
    rows = []
    for ln in lines[1:]:
        vals = ln.split("\t")
        rows.append({header[i]: (vals[i] if i < len(vals) else "") for i in range(len(header))})
    return rows


def _find_goal_col(row, gid):
    pref = "Conversions_{}_".format(gid)  # столбцы целей: Conversions_<id>_<модель>
    for k in row:
        if k.startswith(pref):
            return k
    return None


# ---------- сбор кампаний (без «спящих») ----------
def build_campaign_data(token, login, goal_defs, attribution, per, _post=None, _sleep=None):
    goal_ids = [g["id"] for g in goal_defs]

    week_fields = ["CampaignId", "CampaignName", "Impressions", "Clicks", "Cost", "Conversions"]
    # Reports API: массив Goals не может содержать более 10 элементов — поэтому цели
    # запрашиваем БАТЧАМИ по 10 и склеиваем byGoal по кампаниям (базовые метрики — из первого
    # батча, они от целей не зависят). Без этого клиенты с >10 активными целями роняли рассылку.
    batches = [goal_ids[i:i + 10] for i in range(0, len(goal_ids), 10)] or [None]

    month_fields = ["CampaignId", "CampaignName", "Impressions", "Cost"]

    # Месячный отчёт и батчи целей друг от друга не зависят, поэтому спрашиваем их
    # ОДНОВРЕМЕННО. Последовательно это было 1 + ceil(целей/10) запросов подряд: когда Директ
    # отвечает по 20 секунд, клиент с 22 целями складывался в 80 секунд, и предпросмотр
    # отваливался по таймауту раньше, чем успевал собраться.
    def _month():
        return fetch_report(token, login, per["month_from"], per["month_to"], month_fields,
                            None, attribution, _post=_post, _sleep=_sleep)

    def _batch(b):
        return fetch_report(token, login, per["date_from"], per["date_to"], week_fields,
                            b, attribution, _post=_post, _sleep=_sleep)

    if _post is not None or len(batches) <= 1:
        # с подменённым транспортом (тесты) и на одном батче параллелить нечего
        month_rows = _month()
        pairs = [(b, _batch(b)) for b in batches]
    else:
        from concurrent import futures as _f
        with _f.ThreadPoolExecutor(max_workers=min(5, 1 + len(batches))) as ex:
            fm = ex.submit(_month)
            fb = [(b, ex.submit(_batch, b)) for b in batches]
            month_rows = fm.result()
            pairs = [(b, f.result()) for b, f in fb]

    week = {}
    for batch, week_rows in pairs:
        for r in week_rows:
            cid = str(r.get("CampaignId"))
            obj = week.get(cid)
            if obj is None:
                obj = {
                    "id": cid, "name": str(r.get("CampaignName") or ""),
                    "imp": parse_num(r.get("Impressions")), "clicks": parse_num(r.get("Clicks")),
                    "cost": parse_num(r.get("Cost")), "conv": 0.0, "byGoal": {},
                }
                week[cid] = obj
            if batch:
                for gid in batch:
                    key = _find_goal_col(r, gid)
                    if key:
                        obj["byGoal"][gid] = parse_num(r.get(key))
            elif not goal_ids:
                obj["conv"] = parse_num(r.get("Conversions"))
    if goal_ids:
        for obj in week.values():
            obj["conv"] = sum(obj["byGoal"].values())

    # вселенная активных кампаний = крутившиеся за 4 недели
    universe = {}
    for r in month_rows:
        if parse_num(r.get("Impressions")) <= 0 and parse_num(r.get("Cost")) <= 0:
            continue
        universe[str(r.get("CampaignId"))] = str(r.get("CampaignName") or "")
    # подстраховка: активная на неделе кампания, не попавшая в месячный отчёт
    for cid, obj in week.items():
        if cid not in universe and (obj["imp"] > 0 or obj["cost"] > 0):
            universe[cid] = obj["name"]

    camps = []
    for cid, name in universe.items():
        if cid in week:
            w = week[cid]
            camps.append({
                "id": cid, "name": name or w["name"], "imp": w["imp"], "clicks": w["clicks"],
                "cost": w["cost"], "conv": w["conv"], "byGoal": w["byGoal"], "spent": w["cost"] > 0,
            })
        else:
            camps.append({
                "id": cid, "name": name, "imp": 0.0, "clicks": 0.0,
                "cost": 0.0, "conv": 0.0, "byGoal": {}, "spent": False,
            })
    camps.sort(key=lambda c: (-c["cost"], (c["name"] or "").lower()))
    return camps


# ---------- форматирование сообщения ----------
def split_goal_defs(goal_defs):
    """(ключевые, дополнительные).

    Ключевые — те, что формируют «Конверсии», CR и CPA. Дополнительные показываем строками,
    но в итог НЕ суммируем: иначе прокрутки и просмотры корзины раздувают цифру, которую
    клиент читает как «заявки» (у kst21 так выходил CR 45%).
    Если ключевых не нашлось — считаем по всем, как было раньше.
    """
    key = [g for g in goal_defs if g.get("key", True)]
    other = [g for g in goal_defs if not g.get("key", True)]
    if not key:
        return list(goal_defs), []
    return key, other


def format_metrics(cost, imp, clicks, conv, by_goal, goal_defs, indent="", show_extra=True):
    key_defs, other_defs = split_goal_defs(goal_defs or [])
    if goal_defs:
        # итог считаем ТОЛЬКО по ключевым целям, а не по всему, что отмечено галочкой
        conv = sum(float(by_goal.get(g["id"], 0)) for g in key_defs) if by_goal else 0.0
    ctr = clicks / imp * 100 if imp else 0
    cpc = cost / clicks if clicks else 0
    cr = conv / clicks * 100 if clicks else 0
    cpa = cost / conv if conv else 0
    p = indent
    out = [
        p + "— Расход (с НДС): " + fmt_money(cost),
        p + "— Показы: " + fmt_int(imp),
        p + "— Клики: " + fmt_int(clicks),
        p + "— CTR: " + fmt_pct(ctr),
        p + "— CPC: " + fmt_money(cpc),
    ]

    def goal_lines(defs):
        # цели с нулём НЕ печатаем — иначе отчёт распухает строками «• Цель: 0»
        for g in defs:
            cv = float(by_goal.get(g["id"], 0)) if by_goal else 0.0
            if cv > 0:
                out.append(p + "   • {}: {} (CPA {})".format(g["name"], fmt_int(cv), fmt_money(cost / cv)))

    if goal_defs:
        out.append(p + "— Конверсии (по целям, суммарно): " + fmt_int(conv))
        goal_lines(key_defs)
    else:
        out.append(p + "— Конверсии: " + fmt_int(conv))
    out.append(p + "— CR: " + fmt_pct(cr))
    out.append(p + "— CPA: " + fmt_money(cpa))
    # справочный блок печатаем один раз (в «Итого по аккаунту»), иначе он дублируется в каждой кампании
    if show_extra and other_defs and by_goal and any(float(by_goal.get(g["id"], 0)) > 0 for g in other_defs):
        out.append(p + "— Дополнительные цели (в конверсии не входят):")
        goal_lines(other_defs)
    return "\n".join(out)


def build_message(client_name, goal_defs, camps, per, intro, note):
    """Собирает текст отчёта. Возвращает None, если активных кампаний нет (клиент пропускается)."""
    if not camps:
        return None

    t_cost = t_imp = t_clicks = t_conv = 0.0
    t_by_goal = {g["id"]: 0.0 for g in goal_defs}
    for cm in camps:
        t_cost += cm["cost"]; t_imp += cm["imp"]; t_clicks += cm["clicks"]; t_conv += cm["conv"]
        for g in goal_defs:
            t_by_goal[g["id"]] += float(cm["byGoal"].get(g["id"], 0)) if cm["byGoal"] else 0.0

    m = [intro, "", "Клиент: " + client_name,
         "Период: {} — {}".format(per["label_from"], per["label_to"]), ""]

    if len(camps) >= 2:
        m.append("Итого по аккаунту:")
        m.append(format_metrics(t_cost, t_imp, t_clicks, t_conv, t_by_goal, goal_defs, ""))
        m.append("")
        m.append("По кампаниям:")
        for i, cm in enumerate(camps, 1):
            m.append("{}) {}".format(i, cm["name"]))
            if cm["spent"]:
                m.append(format_metrics(cm["cost"], cm["imp"], cm["clicks"], cm["conv"], cm["byGoal"],
                                        goal_defs, "   ", show_extra=False))
            else:
                m.append("   ⚠️ Кампания за прошлую неделю не расходовала средств, проверьте причину.")
            m.append("")
    else:
        cm = camps[0]
        m.append("Кампания: " + cm["name"])
        if cm["spent"]:
            m.append(format_metrics(cm["cost"], cm["imp"], cm["clicks"], cm["conv"], cm["byGoal"], goal_defs, ""))
        else:
            m.append("⚠️ Кампания за прошлую неделю не расходовала средств, проверьте причину.")
        m.append("")

    if note:                       # приписка опциональна: пустая — не добавляем
        m.append(note)
    while m and m[-1] == "":       # без «висящей» пустой строки в конце
        m.pop()
    return "\n".join(m)


# ---------- определение «лид»-целей (для отчётов) ----------
_LEAD_GOAL_TYPES = {"form", "phone", "messenger", "e_purchase",
                    "a_purchase", "a_create_order", "contact_data_sent"}
_MICRO_GOAL_TYPES = {"number", "search", "step", "file", "social"}
_LEAD_GOAL_WORDS = ("заявк", "звон", "покупк", "заказ", "купить", "обратн", "лид")
MAX_KEY_GOALS = 15      # сколько целей формируют «Конверсии»/CR/CPA (состав итога не меняем)
MAX_REPORT_GOALS = 30   # сколько целей вообще показываем (запросы к Директу идут батчами по 10)


def _is_lead_goal(name, gtype):
    """Бизнес-лид (заявка/звонок/покупка/заказ), а не микро-действие (просмотр/поиск)?"""
    t = (gtype or "").lower()
    if t in _LEAD_GOAL_TYPES:
        return True
    if t in _MICRO_GOAL_TYPES:
        return False
    return any(w in (name or "").lower() for w in _LEAD_GOAL_WORDS)


# ---------- высокоуровневые операции поверх базы ----------
def goal_defs_from_client(client_row, only_active=True, only_ids=None):
    """Цели клиента -> [{'id','name'}].

    only_ids — взять ровно эти id (конструктор: выбор пользователя), игнорируя active.
    При only_active=True в отчёт идут ВСЕ цели, отмеченные специалистом галочкой «в отчёт».

    Раньше здесь стояло `lead or active`: если находилась хоть одна «лид-цель», все остальные
    включённые вручную цели молча выбрасывались (у клиента с 13 включёнными в отчёт попадали 2 —
    «Написать в WhatsApp/Telegram» система считала мусором). Галочка обязана работать.
    Лид-фильтр остался только как приоритет, если целей больше потолка MAX_REPORT_GOALS:
    тогда сначала берём лид-цели, потом остальные — чтобы не потерять главное.
    """
    try:
        items = json.loads(client_row["goals"] or "[]")
    except (ValueError, TypeError):
        items = []
    norm = []
    for g in items:
        if isinstance(g, dict):
            gid = str(g.get("id"))
            norm.append({"id": gid, "name": g.get("name") or "Цель " + gid,
                         "active": (g.get("active") is not False), "type": g.get("type", "")})
        else:
            norm.append({"id": str(g), "name": "Цель " + str(g), "active": True, "type": ""})
    key_ids = None                            # None = все выбранные цели формируют итог
    if only_ids is not None:
        want = set(str(x) for x in only_ids)
        sel = [g for g in norm if g["id"] in want]
    elif only_active:
        active = [g for g in norm if g["active"]]
        lead = [g for g in active if _is_lead_goal(g["name"], g.get("type"))]
        # состав «Конверсий» оставляем ровно тем, что был до фикса, — цифры у клиентов не едут
        key_ids = {g["id"] for g in (lead or active)[:MAX_KEY_GOALS]}
        if len(active) <= MAX_REPORT_GOALS:
            sel = active                      # показываем всё, что отметил специалист
        else:                                 # слишком много: лид-цели вперёд, остальные добираем
            rest = [g for g in active if g not in lead]
            sel = (lead + rest)[:MAX_REPORT_GOALS]
    else:
        sel = norm
    # key — цель формирует «Конверсии»/CR/CPA; остальные печатаются справочно (см. split_goal_defs)
    return [{"id": g["id"], "name": g["name"],
             "key": True if key_ids is None else (g["id"] in key_ids)} for g in sel]


def build_for_login(token, db, login, intro, note, default_attr="LSCCD", _post=None, _sleep=None):
    """Возвращает (text|None, camps, period) для одного клиента."""
    c = db.get_client(login)
    if not c:
        raise RuntimeError("Клиент {} не найден в базе".format(login))
    goal_defs = goal_defs_from_client(c)
    # Приписка ведущего специалиста: у владельца клиента может быть своя (users.note).
    # NULL = общая (аргумент note из Настроек), '' = совсем без приписки, текст = своя.
    # Работает везде, где строится отчёт: превью, рассылка кнопкой, cron, «Сторонние».
    owner = c["owner"] if "owner" in c.keys() else None
    if owner:
        try:
            u = db.get_user(owner)
            if u is not None and "note" in u.keys() and u["note"] is not None:
                note = u["note"]
        except Exception:  # noqa: BLE001 — нет таблицы users (старая база) — остаётся общая
            pass
    attr = (c["attribution"] if c["attribution"] else None) or default_attr or "LSCCD"
    per = period()
    camps = build_campaign_data(token, login, goal_defs, attr, per, _post=_post, _sleep=_sleep)
    text = build_message(c["name"] or login, goal_defs, camps, per, intro, note)
    return text, camps, per


def send_for_login(token, tg, db, login, intro, note, default_attr="LSCCD", dry_run=False):
    """Строит отчёт и отправляет во все привязанные к клиенту чаты. Пишет в send_log.
    dry_run=True — только строит отчёт, НЕ отправляет клиентам и НЕ пишет в лог (безопасный тест)."""
    text, camps, per = build_for_login(token, db, login, intro, note, default_attr)
    chats = db.bindings_for_login(login)
    # собираем ВСЕ проблемы разом (раньше возвращалась только первая): напр. и «нет открута»,
    # и «нет чата» одновременно — чтобы в пробе/рассылке было видно всё.
    reasons = []
    if text is None:
        reasons.append("нет открута за 4 недели")
    if not chats:
        reasons.append("нет привязанного чата")
    if reasons:
        # приоритет статуса для счётчиков: нет открута → skipped, иначе → no_chat
        status = "skipped" if text is None else "no_chat"
        return {"status": status, "reason": " · ".join(reasons), "reasons": reasons}
    if dry_run:
        return {"status": "dry", "chats": len(chats), "campaigns": len(camps)}
    sent = 0
    for b in chats:
        try:
            tg.send_message(b["chat_id"], text)
            db.log_send(login, b["chat_id"], per["date_from"], per["date_to"], "sent")
            sent += 1
        except Exception as e:  # noqa: BLE001
            db.log_send(login, b["chat_id"], per["date_from"], per["date_to"], "error", str(e))
    return {"status": "sent", "chats": sent, "campaigns": len(camps)}


def run_weekly(token, tg, db, intro, note, default_attr="LSCCD", on_progress=None, logins=None, dry_run=False):
    """Прогон по всем привязанным клиентам (для планировщика/кнопки «Запустить рассылку»).

    on_progress(done, total, detail) — колбэк после каждого клиента (для окна прогресса).
    logins — если задан, слать только этим (для «переотправить недошедшим»).
    dry_run=True — прогнать построение отчётов без отправки клиентам (безопасная проверка)."""
    if logins is None:
        logins = sorted({b["login"] for b in db.list_bindings()})
    # сторонние (копипаст) в бот-рассылку не попадают — их доставляют вручную
    try:
        ext = db.external_logins()
        if ext:
            logins = [l for l in logins if l not in ext]
    except Exception:  # noqa: BLE001 — старая база без колонки delivery
        pass
    total = len(logins)
    results = {"sent": 0, "skipped": 0, "no_chat": 0, "errors": 0, "dry": 0, "total": total, "details": []}
    per = period()

    def _name(lg):
        c = db.get_client(lg)
        return (c["name"] if c and c["name"] else lg)

    for i, login in enumerate(logins, 1):
        detail = {"login": login, "name": _name(login)}
        if on_progress:            # «взял в работу» — чтобы в окне было видно, на ком сейчас стоим
            try:
                on_progress(i - 1, total, dict(detail, status="running"))
            except Exception:      # noqa: BLE001 — прогресс не должен ронять рассылку
                pass
        try:
            res = send_for_login(token, tg, db, login, intro, note, default_attr, dry_run=dry_run)
            detail.update(res)
        except Exception as e:  # noqa: BLE001
            results["errors"] += 1
            try:                       # фиксируем ошибку в Историю (раньше терялась)
                db.log_send(login, None, per["date_from"], per["date_to"], "error", str(e))
            except Exception:          # noqa: BLE001
                pass
            try:                       # и в файловый лог — для авто-рассылки на сервере без консоли
                from .settings import log_error
                log_error("weekly." + login, e)
            except Exception:          # noqa: BLE001
                pass
            detail.update({"status": "error", "reason": str(e)})
            results["details"].append(detail)
            if on_progress:
                on_progress(i, total, detail)
            continue
        if res["status"] == "sent":
            results["sent"] += 1
        elif res["status"] == "dry":
            results["dry"] += 1
        elif res["status"] == "skipped":
            results["skipped"] += 1
            if not dry_run:   # логируем «пропущено» (нет открута) — для контроля обязательств
                try:
                    db.log_send(login, None, per["date_from"], per["date_to"], "skipped", res.get("reason"))
                except Exception:  # noqa: BLE001
                    pass
        elif res["status"] == "no_chat":
            results["no_chat"] += 1
        results["details"].append(detail)
        if on_progress:
            on_progress(i, total, detail)
    return results
