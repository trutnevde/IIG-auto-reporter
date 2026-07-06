# -*- coding: utf-8 -*-
"""Выгрузка данных Яндекс.Директа в Google-таблицы клиентов.

Одна таблица на клиента, заголовок «Auto-Reporter ОТЧЕТ <домен>», доступ — сервисный
аккаунт Google (ключ sa_key.json рядом с программой). Модуль заполняет листы-разрезы
СЫРЫМИ входными значениями из Директа:
  * метрик-блок: Показы, Клики, Расход (с НДС), Ср. позиция показов/кликов;
  * колонки целей: каждая цель Метрики — отдельный столбец, матчится по названию.

Производные столбцы (CTR, CPC, CR, Конверсии total/Лиды общие, CPA, …) в таблицах —
ФОРМУЛЫ, привязанные к номеру строки (напр. `=IFERROR(C2/B2;0)`, `=SUM(J2:O2)`).
Поэтому мы их НЕ перезаписываем значениями, а ПРОДЛЯЕМ формулу из предыдущей строки
(бампим номер строки). Внешние value-столбцы (Callibri/Ticketscloud-данные) не трогаем —
оставляем пустыми, их заполняет человек.

Проверено вживую (kst21/mnak/gazovichkof): таблицы построены на атрибуции LYDC
(Reports API отдаёт колонку с суффиксом `_AUTO`); метрик-блок и цели воспроизводятся точно.
Ограничения Reports API: поле `Conversions` обязательно для колонок целей; не более
10 целей в одном запросе (батчим).
"""
import os
import re

from . import settings
from . import report as R
from . import report_custom as RC

# ---- доступы ----
SCOPES_RO = ["https://www.googleapis.com/auth/spreadsheets.readonly",
             "https://www.googleapis.com/auth/drive.readonly"]
SCOPES_RW = ["https://www.googleapis.com/auth/spreadsheets",
             "https://www.googleapis.com/auth/drive.readonly"]

TITLE_RE = re.compile(r"Auto-?Reporter\s+ОТЧЕ?Т\s+(.+)$", re.IGNORECASE)
GOALS_PER_REQUEST = 10
DEFAULT_ATTR = "LYDC"   # атрибуция, на которой построены таблицы (см. модульную доку)


# ---- путь к ключу сервисного аккаунта (зеркалит settings._secrets_candidates) ----
def _key_candidates():
    if settings.FROZEN:
        return [os.path.join(settings.BASE_DIR, "sa_key.json")]
    return [os.path.join(settings._REPO_ROOT_DEV, "sa_key.json"),
            os.path.join(settings._APP_DIR_DEV, "sa_key.json")]


def key_path():
    return settings._first_existing(*_key_candidates())


def available():
    """True, если ключ сервисного аккаунта на месте (можно работать с таблицами)."""
    return key_path() is not None


def client(readonly=True):
    import gspread
    from google.oauth2.service_account import Credentials
    p = key_path()
    if not p:
        raise FileNotFoundError(
            "sa_key.json не найден ({}). Положи ключ сервисного аккаунта Google рядом с программой."
            .format(settings.BASE_DIR if settings.FROZEN else "корень репозитория"))
    creds = Credentials.from_service_account_file(p, scopes=(SCOPES_RO if readonly else SCOPES_RW))
    return gspread.authorize(creds)


def discover(gc=None):
    """{домен: {'id','title'}} по всем таблицам, расшаренным на сервисный аккаунт."""
    gc = gc or client()
    out = {}
    for f in gc.list_spreadsheet_files():
        m = TITLE_RE.search((f.get("name") or "").strip())
        if m:
            out[m.group(1).strip().lower()] = {"id": f.get("id"), "title": f.get("name")}
    return out


# ---- классификация столбцов листа ----
def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


_PERIOD = {"период", "неделя", "дата", "месяц"}
_METRIC = {
    "показы": "imp", "клики": "clicks",
    "расход (с ндс)": "cost", "расход с ндс": "cost", "расход, ₽": "cost", "расход": "cost",
    "ср. позиция показов": "pos_imp", "ср. позиция показа": "pos_imp",
    "ср. позиция кликов": "pos_clk", "ср. позиция клика": "pos_clk",
}
_COST_NOVAT = {"расход (без ндс)/евро", "расход (без ндс)", "расход без ндс", "расход (без ндс)/€"}


def match_goal(title, goals):
    """Сопоставляет заголовок столбца с целью Метрики. Возвращает goal_id или None.

    Сначала точное совпадение имени, затем — по суффиксу (у mnak цели названы с
    категориальным префиксом «Ticketsсloud …», а в столбце он вынесен в шапку-категорию).
    """
    n = _norm(title)
    if not n:
        return None
    for g in goals:
        if _norm(g["name"]) == n:
            return g["id"]
    cands = [g for g in goals if _norm(g["name"]).endswith(" " + n)]
    if len(cands) == 1:
        return cands[0]["id"]
    return None


def classify_columns(header, formula_row, goals):
    """Размечает столбцы листа.

    header       — строка-шапка (отображаемые заголовки).
    formula_row  — любая строка С ДАННЫМИ в режиме FORMULA (чтобы понять, где формула).
    goals        — список целей клиента [{'id','name'}].

    Возвращает список спеков по столбцам: {idx, title, kind, goal_id}.
    kind: period | metric:<key> | goal | formula | cost_novat | external | empty
    """
    specs = []
    for i, h in enumerate(header):
        title = str(h or "").strip()
        n = _norm(title)
        cell = str(formula_row[i]) if i < len(formula_row) else ""
        is_formula = cell.startswith("=")
        if not title:
            specs.append({"idx": i, "title": title, "kind": "empty", "goal_id": None})
            continue
        if is_formula:
            specs.append({"idx": i, "title": title, "kind": "formula", "goal_id": None})
            continue
        if n in _PERIOD:
            specs.append({"idx": i, "title": title, "kind": "period", "goal_id": None})
            continue
        if n in _METRIC:
            specs.append({"idx": i, "title": title, "kind": "metric:" + _METRIC[n], "goal_id": None})
            continue
        if n in _COST_NOVAT:
            specs.append({"idx": i, "title": title, "kind": "cost_novat", "goal_id": None})
            continue
        gid = match_goal(title, goals)
        if gid:
            specs.append({"idx": i, "title": title, "kind": "goal", "goal_id": gid})
            continue
        # value-столбец без источника в Директе/Метрике (Callibri/Ticketscloud-данные и пр.)
        specs.append({"idx": i, "title": title, "kind": "external", "goal_id": None})
    return specs


def find_header_row(values):
    """Индекс строки-шапки: первая строка, где >4 непустых ячеек (над ней может быть
    строка-категория с объединёнными подписями, как у mnak)."""
    for i, r in enumerate(values[:4]):
        if sum(1 for c in r if str(c).strip()) > 4:
            return i
    return 0


# ---- данные из Директа за период (account-level) ----
def account_period(token, login, date_from, date_to, goal_defs,
                   attribution=DEFAULT_ATTR, want_positions=False, _post=None, _sleep=None):
    """Метрик-блок + конверсии по каждой цели за период.

    Возвращает {'imp','clicks','cost','pos_imp','pos_clk','by_goal':{gid:val}}.
    Конверсии тянутся батчами по <=10 целей (ограничение Reports API).
    """
    base_fields = ["Impressions", "Clicks", "Cost"]
    if want_positions:
        base_fields += ["AvgImpressionPosition", "AvgClickPosition"]
    base = R.fetch_report(token, login, date_from, date_to, base_fields,
                          report_type="ACCOUNT_PERFORMANCE_REPORT", _post=_post, _sleep=_sleep)
    imp = sum(R.parse_num(r.get("Impressions")) for r in base)
    clk = sum(R.parse_num(r.get("Clicks")) for r in base)
    cost = sum(R.parse_num(r.get("Cost")) for r in base)
    pos_imp = max((R.parse_num(r.get("AvgImpressionPosition")) for r in base), default=0.0)
    pos_clk = max((R.parse_num(r.get("AvgClickPosition")) for r in base), default=0.0)

    by_goal = {}
    gids = [g["id"] for g in (goal_defs or [])]
    for s in range(0, len(gids), GOALS_PER_REQUEST):
        batch = gids[s:s + GOALS_PER_REQUEST]
        rows = R.fetch_report(token, login, date_from, date_to,
                              ["Impressions", "Clicks", "Cost", "Conversions"],
                              goal_ids=batch, attribution=attribution,
                              report_type="ACCOUNT_PERFORMANCE_REPORT", _post=_post, _sleep=_sleep)
        for r in rows:
            for gid in batch:
                col = R._find_goal_col(r, gid)
                if col:
                    by_goal[gid] = by_goal.get(gid, 0.0) + R.parse_num(r.get(col))
    return {"imp": imp, "clicks": clk, "cost": cost,
            "pos_imp": pos_imp, "pos_clk": pos_clk, "by_goal": by_goal}


# ---- продление формулы на новую строку (бамп номера строки) ----
def bump_formula(formula, from_row, to_row):
    """`=IFERROR(C2/B2;0)` при from_row=2,to_row=3 -> `=IFERROR(C3/B3;0)`.

    Бампим только ссылки-ячейки (буква столбца + номер строки), не задевая прочие числа.
    Формулы в таблицах ссылаются на собственную строку — этого достаточно.
    """
    pat = re.compile(r"(\$?[A-Z]{1,3}\$?)" + str(from_row) + r"(?![0-9])")
    return pat.sub(lambda m: m.group(1) + str(to_row), formula)


def _period_label(date_from, date_to, sample, grain="week"):
    """Формат периода как в таблице. sample — пример из соседней строки (для угадывания)."""
    if grain == "month":
        y, m, _ = date_from.split("-")
        name = _MONTHS_RU[int(m)]
        if sample and re.search(r"[А-Яа-яЁё]+\s+\d{2}\b", sample) and not re.search(r",", sample):
            return "{} {}".format(name, y[2:])      # «Декабрь 25»
        return "{}, {}".format(name, y)             # «Сентябрь, 2025»
    def fmt(d, short):
        y, m, dd = d.split("-")
        return "{}.{}.{}".format(dd, m, y[2:] if short else y)
    short = bool(sample and re.search(r"\d{2}\.\d{2}\.\d{2}(?!\d)", sample))
    dash = "–" if (sample and "–" in sample) else "-"
    sep = " {} ".format(dash)
    return "{}{}{}".format(fmt(date_from, short), sep, fmt(date_to, short))


def build_weekly_row(specs, data, date_from, date_to, target_row, from_row,
                     tmpl_formula_row, sample_period, grain="week"):
    """Формирует массив ячеек новой строки-периода (для USER_ENTERED-записи).

    specs            — разметка столбцов (classify_columns).
    data             — результат account_period.
    target_row       — 1-based номер строки, куда пишем (для бампа формул).
    from_row         — 1-based номер строки-образца (для бампа: from_row -> target_row).
    tmpl_formula_row — строка-образец в режиме FORMULA (откуда тянем формулы).
    sample_period    — пример ячейки периода (для угадывания формата дат).
    Возвращает list ячеек: числа/строки/формулы; "" = очистить/не трогать.
    """
    out = []
    for s in specs:
        k = s["kind"]
        if k == "period":
            out.append(_period_label(date_from, date_to, sample_period, grain))
        elif k == "metric:imp":
            out.append(int(round(data["imp"])))
        elif k == "metric:clicks":
            out.append(int(round(data["clicks"])))
        elif k == "metric:cost":
            out.append(round(data["cost"], 2))
        elif k == "metric:pos_imp":
            out.append(round(data["pos_imp"], 2) if data["pos_imp"] else "")
        elif k == "metric:pos_clk":
            out.append(round(data["pos_clk"], 2) if data["pos_clk"] else "")
        elif k == "goal":
            out.append(int(round(data["by_goal"].get(s["goal_id"], 0))))
        elif k == "formula":
            f = str(tmpl_formula_row[s["idx"]]) if s["idx"] < len(tmpl_formula_row) else ""
            out.append(bump_formula(f, from_row, target_row) if f.startswith("=") else "")
        else:  # cost_novat / external / empty — не заполняем
            out.append("")
    return out


def _last_data_row(values, header_idx):
    """0-based индекс последней непустой строки с данными (ниже шапки)."""
    for i in range(len(values) - 1, header_idx, -1):
        if any(str(c).strip() for c in values[i]):
            return i
    return header_idx


_NONPERIOD = {"total", "итого", "контекст итог", "итог"}


_MONTHS_RU = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
              "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
_MONTH_IDX = {n.lower(): i for i, n in enumerate(_MONTHS_RU) if n}


def _row_start_iso(label):
    """Из подписи периода («15.09.2025 - 21.09.2025» / «29.12.25 – 04.01.26») берём
    начальную дату в ISO (YYYY-MM-DD) для сравнения. None, если не распознали."""
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{2,4})", str(label or ""))
    if not m:
        return None
    d, mo, y = m.group(1), m.group(2), m.group(3)
    if len(y) == 2:
        y = "20" + y
    return "{}-{}-{}".format(y, mo, d)


def _label_key(label, grain):
    """Ключ строки для дедупа: неделя -> ISO начала; месяц -> YYYY-MM (из «Сентябрь, 2025»)."""
    if grain == "month":
        m = re.search(r"([А-Яа-яЁё]+)\D*(\d{2,4})", str(label or ""))
        if not m:
            return None
        mi = _MONTH_IDX.get(m.group(1).lower())
        if not mi:
            return None
        y = m.group(2)
        y = "20" + y if len(y) == 2 else y
        return "{}-{:02d}".format(y, mi)
    return _row_start_iso(label)


def _target_key(date_from, grain):
    """Ключ нового периода для дедупа (то же пространство, что _label_key)."""
    return date_from[:7] if grain == "month" else date_from


def _period_rows(values, header_idx):
    """0-based индексы строк-периодов (недель/месяцев): в колонке A есть цифры и это не
    итоговая строка (total/Контекст итог). Так отсекаем шапку и строки-итоги."""
    out = []
    for i in range(header_idx + 1, len(values)):
        a = str(values[i][0]).strip() if values[i] else ""
        if not a:
            continue
        if _norm(a) in _NONPERIOD:
            continue
        if re.search(r"\d", a):
            out.append(i)
    return out


_INPUT_KINDS = ("period", "goal")  # + всё metric:* (см. _is_input)


def _is_input(kind):
    return kind in _INPUT_KINDS or kind.startswith("metric:")


def fill_weekly(ws, token, login, all_goals, date_from, date_to, query_to=None,
                attribution=DEFAULT_ATTR, dry_run=True, grain="week"):
    """Заполняет строку-период (неделя/месяц) в листе-ленте «свежими данными».

    date_from/date_to — границы периода для ПОДПИСИ (полная неделя Пн–Вс / месяц).
    query_to — фактический конец запроса к Директу (по умолчанию date_to). Для «живого»
        текущего периода передают сегодняшнюю дату → строка включает данные за сегодня.
    UPSERT: если строка периода уже есть — ОБНОВЛЯЕТ только входные ячейки (Период/Показы/
        Клики/Расход/цели), не трогая формулы и внешние столбцы (Callibri/Ticketscloud).
        Если строки нет — дописывает (формулы продлеваются), вставляя перед футером-итогом.
    """
    query_to = query_to or date_to
    values = ws.get_all_values()
    formulas = ws.get_all_values(value_render_option="FORMULA")
    hi = find_header_row(values)
    header = values[hi]
    periods = _period_rows(values, hi)
    tkey = _target_key(date_from, grain)
    existing = next((i for i in periods if _label_key(values[i][0], grain) == tkey), None)

    # образец для разметки/формата: сама строка (если обновляем) или последняя строка-период
    tmpl = existing if existing is not None else (periods[-1] if periods else _last_data_row(values, hi))
    specs = classify_columns(header, formulas[tmpl], all_goals)
    goal_defs = [{"id": s["goal_id"], "name": s["title"]} for s in specs if s["kind"] == "goal"]
    data = account_period(token, login, date_from, query_to, goal_defs, attribution,
                          want_positions=True)
    sample_period = next((str(values[tmpl][s["idx"]]) for s in specs
                          if s["kind"] == "period" and s["idx"] < len(values[tmpl])), "")

    if existing is not None:
        # ОБНОВЛЕНИЕ на месте: пишем только входные ячейки, формулы/внешние не трогаем
        target_row = existing + 1
        row = build_weekly_row(specs, data, date_from, date_to, target_row, target_row,
                               formulas[existing], sample_period, grain)
        if dry_run:
            return {"target_row": target_row, "header": header, "row": row, "specs": specs,
                    "mode": "update"}
        from gspread.utils import rowcol_to_a1
        batch = [{"range": rowcol_to_a1(target_row, s["idx"] + 1), "values": [[row[k]]]}
                 for k, s in enumerate(specs) if _is_input(s["kind"])]
        if batch:
            ws.batch_update(batch, value_input_option="USER_ENTERED")
        return {"target_row": target_row, "header": header, "row": row,
                "mode": "update", "written": True}

    # ДОБАВЛЕНИЕ новой строки-периода
    has_footer = any(any(str(c).strip() for c in values[i]) for i in range(tmpl + 1, len(values)))
    target_row = tmpl + 2
    row = build_weekly_row(specs, data, date_from, date_to, target_row, tmpl + 1,
                           formulas[tmpl], sample_period, grain)
    if dry_run:
        return {"target_row": target_row, "header": header, "row": row, "specs": specs,
                "mode": "insert" if has_footer else "append"}
    if has_footer:
        ws.insert_row(row, index=target_row, value_input_option="USER_ENTERED")
    else:
        from gspread.utils import rowcol_to_a1
        rng = "A{}:{}".format(target_row, rowcol_to_a1(target_row, len(row)))
        ws.update(values=[row], range_name=rng, value_input_option="USER_ENTERED")
    return {"target_row": target_row, "header": header, "row": row,
            "mode": "insert" if has_footer else "append", "written": True}


# ---- разрез-листы (снимок за период новым листом) ----
# ключ -> (имя листа, уровень Директа, срезы)
BREAKDOWNS = {
    "campaign":    ("По РК", "campaign", None),
    "adgroup":     ("По группам", "adgroup", None),
    "keyword":     ("По ключам", "keyword", None),
    "searchquery": ("Поисковые фразы", "searchquery", None),
    "geo":         ("По регионам", "account", ["geo"]),
}


def _month_label(date_from):
    y, m, _ = date_from.split("-")
    return "{} {}".format(_MONTHS_RU[int(m)], y)


def build_breakdown_values(res):
    """2D-массив для разрез-листа из результата report_custom.build:
    [измерения…] | Показы | Клики | Расход (с НДС) | CTR | CPC [| Конверсии | CR | CPA]."""
    dimt = list(res["dim_titles"])
    use_conv = bool(res.get("use_conv"))
    metric_h = ["Показы", "Клики", "Расход (с НДС)", "CTR", "CPC"]
    if use_conv:
        metric_h += ["Конверсии", "CR", "CPA"]

    def mcells(m):
        c = [R.fmt_int(m["imp"]), R.fmt_int(m["clicks"]), R.fmt_money(m["cost"]),
             R.fmt_pct(m["ctr"]), R.fmt_money(m["cpc"])]
        if use_conv:
            c += [R.fmt_int(m["conv"]), R.fmt_pct(m["cr"]), R.fmt_money(m["cpa"])]
        return c

    out = [dimt + metric_h]
    out.append((["ИТОГО"] + [""] * (len(dimt) - 1)) + mcells(res["totals"]))
    for row in res["rows"]:
        dims = [(d or "—") for d in row["dims"]]
        dims = (dims + [""] * len(dimt))[:len(dimt)]
        out.append(dims + mcells(row["m"]))
    return out


def push_breakdown(gc, sid, token, login, which, date_from, date_to,
                   attribution=DEFAULT_ATTR, limit=200, replace=True):
    """Создаёт НОВЫЙ лист-снимок разреза за период (имя «По группам (Июнь 2026)»).

    which — ключ из BREAKDOWNS. Конверсии берутся «голым» полем Conversions (без целей —
    надёжно и без лимита 10 целей). Если лист с таким именем уже есть: replace=True пересоздаёт.
    """
    if which not in BREAKDOWNS:
        raise RuntimeError("Неизвестный разрез: {}".format(which))
    name, level, segments = BREAKDOWNS[which]
    res = RC.build(token, login, level, date_from, date_to, attribution=attribution,
                   goal_defs=None, segments=segments, limit=limit)
    values = build_breakdown_values(res)
    title = "{} ({})".format(name, _month_label(date_from))
    sh = gc.open_by_key(sid)
    existing = {w.title: w for w in sh.worksheets()}
    if title in existing:
        if not replace:
            return {"skipped": title, "reason": "лист уже есть"}
        sh.del_worksheet(existing[title])
    ws = sh.add_worksheet(title=title, rows=len(values) + 5, cols=len(values[0]) + 1)
    from gspread.utils import rowcol_to_a1
    rng = "A1:{}".format(rowcol_to_a1(len(values), len(values[0])))
    ws.update(values=values, range_name=rng, value_input_option="USER_ENTERED")
    return {"created": title, "n_rows": res["n_shown"], "n_total": res["n_total"]}


# ---- составные помесячные листы («Июнь 26»): блок по кампаниям + под-блок по неделям ----
def _campaign_period(token, login, date_from, date_to, goal_defs, attribution=DEFAULT_ATTR):
    """{имя_кампании: {imp,clicks,cost,pos_imp,pos_clk,by_goal}} за период (CAMPAIGN-отчёт)."""
    base = R.fetch_report(token, login, date_from, date_to,
                          ["CampaignName", "Impressions", "Clicks", "Cost",
                           "AvgImpressionPosition", "AvgClickPosition"],
                          report_type="CAMPAIGN_PERFORMANCE_REPORT")
    out = {}
    for r in base:
        nm = str(r.get("CampaignName") or "")
        out[nm] = {"name": nm, "imp": R.parse_num(r.get("Impressions")),
                   "clicks": R.parse_num(r.get("Clicks")), "cost": R.parse_num(r.get("Cost")),
                   "pos_imp": R.parse_num(r.get("AvgImpressionPosition")),
                   "pos_clk": R.parse_num(r.get("AvgClickPosition")), "by_goal": {}}
    gids = [g["id"] for g in goal_defs]
    for s in range(0, len(gids), GOALS_PER_REQUEST):
        batch = gids[s:s + GOALS_PER_REQUEST]
        rows = R.fetch_report(token, login, date_from, date_to,
                              ["CampaignName", "Impressions", "Clicks", "Cost", "Conversions"],
                              goal_ids=batch, attribution=attribution,
                              report_type="CAMPAIGN_PERFORMANCE_REPORT")
        for r in rows:
            o = out.get(str(r.get("CampaignName") or ""))
            if o:
                for gid in batch:
                    col = R._find_goal_col(r, gid)
                    if col:
                        o["by_goal"][gid] = o["by_goal"].get(gid, 0.0) + R.parse_num(r.get(col))
    return out


def _build_entity_row(specs, ent, target_row, from_row, tmpl_formula_row, name=None):
    """Строка сущности (кампании): столбец 0 = имя, метрики/цели/формулы как обычно.
    ent — словарь с imp/clicks/cost/pos_*/by_goal. name переопределяет столбец 0."""
    out = []
    for s in specs:
        k = s["kind"]
        if s["idx"] == 0:
            out.append(name if name is not None else ent.get("name", ""))
        elif k == "metric:imp":
            out.append(int(round(ent["imp"])))
        elif k == "metric:clicks":
            out.append(int(round(ent["clicks"])))
        elif k == "metric:cost":
            out.append(round(ent["cost"], 2))
        elif k == "metric:pos_imp":
            out.append(round(ent["pos_imp"], 2) if ent.get("pos_imp") else "")
        elif k == "metric:pos_clk":
            out.append(round(ent["pos_clk"], 2) if ent.get("pos_clk") else "")
        elif k == "goal":
            out.append(int(round(ent["by_goal"].get(s["goal_id"], 0))))
        elif k == "formula":
            f = str(tmpl_formula_row[s["idx"]]) if s["idx"] < len(tmpl_formula_row) else ""
            out.append(bump_formula(f, from_row, target_row) if f.startswith("=") else "")
        else:
            out.append("")
    return out


def _block_bounds(values, header_idx):
    """(индексы строк-данных, индекс футера|None) для блока, начиная под шапкой header_idx,
    до строки-итога (NONPERIOD) или конца."""
    foot = None
    for i in range(header_idx + 1, len(values)):
        if _norm(values[i][0]) in _NONPERIOD:
            foot = i
            break
    end = (foot - 1) if foot is not None else (len(values) - 1)
    return list(range(header_idx + 1, end + 1)), foot


def _derive_month_label(template_title, month_from):
    """Имя листа текущего месяца в формате шаблона: «Июнь 26» + 2026-07 → «Июль 26»."""
    y, m, _ = month_from.split("-")
    new_month = _MONTHS_RU[int(m)]
    has4 = bool(re.search(r"\d{4}", template_title))
    yr = y if has4 else y[2:]
    t = re.sub(r"[А-Яа-яЁё]+", new_month, template_title, count=1)
    t = re.sub(r"\d{2,4}", yr, t, count=1)
    return t.strip()


def _month_weeks(month_from, query_to):
    """Недели, чей ПОНЕДЕЛЬНИК приходится на месяц month_from, до сегодня (query_to).
    Каждая: (date_from, date_to_подписи, query_to). Так недели не перетекают между месяцами."""
    from datetime import date, timedelta
    y, m, _ = (int(x) for x in month_from.split("-"))
    qy, qm, qd = (int(x) for x in query_to.split("-"))
    today = date(qy, qm, qd)
    d = date(y, m, 1)
    monday = d - timedelta(days=d.weekday())
    if monday.month != m:
        monday += timedelta(days=7)
    out = []
    while monday.month == m and monday <= today:
        wk_to = monday + timedelta(days=6)
        out.append((monday.isoformat(), wk_to.isoformat(), min(wk_to, today).isoformat()))
        monday += timedelta(days=7)
    return out


def ensure_month_detail(gc, sid, month_from):
    """(worksheet, is_new) для составного листа месяца month_from. Если листа нет — клонирует
    самый свежий ПРЕДЫДУЩИЙ месячный лист (та же структура/формулы) и переименовывает в текущий.
    (None, False) — если нет ни одного месячного листа-шаблона."""
    sh = gc.open_by_key(sid)
    cur = month_from[:7]
    prior, prior_key = None, None
    for w in sh.worksheets():
        k = _label_key(w.title, "month")
        if not k:
            continue
        if k == cur:
            return w, False
        if k < cur and (prior_key is None or k > prior_key):
            prior, prior_key = w, k
    if prior is None:
        return None, False
    label = _derive_month_label(prior.title, month_from)
    try:
        return sh.worksheet(label), False       # вдруг уже создан с таким именем
    except Exception:  # noqa: BLE001
        pass
    return prior.duplicate(new_sheet_name=label), True


def fill_month_detail(ws, token, login, all_goals, month_from, query_to, weeks=None,
                      reset=False, attribution=DEFAULT_ATTR, dry_run=True):
    """Составной помесячный лист («Июнь 26»): обновляет верхний блок по кампаниям за месяц
    (1-е..сегодня) и недели в нижнем под-блоке. Пишет только входные ячейки — формулы, футеры
    и внешние столбцы (Комиссия) не трогает.
    weeks — недели (date_from, date_to_подписи, query_to); None → текущая (или все недели месяца
    при reset). reset=True — ПЕРЕПИСАТЬ недельный блок с нуля (для только что клонированного листа
    прошлого месяца): недели месяца по порядку, лишние строки очистить."""
    from datetime import date as _d
    from gspread.utils import rowcol_to_a1
    values = ws.get_all_values()
    formulas = ws.get_all_values(value_render_option="FORMULA")

    top_h = next((i for i, r in enumerate(values) if r and _norm(r[0]) == "кампания"), None)
    bot_h = next((i for i, r in enumerate(values) if r and _norm(r[0]) in ("период", "неделя")), None)
    if top_h is None:
        raise RuntimeError("Не нашёл шапку «Кампания» в составном листе")

    batch = []
    info = {}

    # --- верхний блок: по кампаниям ---
    tf_top = formulas[top_h + 1] if top_h + 1 < len(formulas) else []
    top_specs = classify_columns(values[top_h], tf_top, all_goals)
    top_goals = [{"id": s["goal_id"], "name": s["title"]} for s in top_specs if s["kind"] == "goal"]
    camps = _campaign_period(token, login, month_from, query_to, top_goals, attribution)
    camp_list = sorted(camps.values(), key=lambda c: -c["cost"])
    slots, _ = _block_bounds(values[:bot_h] if bot_h else values, top_h)
    for k, ridx in enumerate(slots):
        tr = ridx + 1
        if k < len(camp_list):
            row = _build_entity_row(top_specs, camp_list[k], tr, top_h + 2, tf_top)
        else:
            row = None  # лишний слот — чистим входы
        for j, s in enumerate(top_specs):
            if s["idx"] == 0 or _is_input(s["kind"]):
                batch.append({"range": rowcol_to_a1(tr, s["idx"] + 1),
                              "values": [[row[j] if row else ""]]})
    info["campaigns"] = len(camp_list)

    # --- нижний блок: недели ---
    if bot_h is not None:
        tf_bot = formulas[bot_h + 1] if bot_h + 1 < len(formulas) else []
        bot_specs = classify_columns(values[bot_h], tf_bot, all_goals)
        bot_goals = [{"id": s["goal_id"], "name": s["title"]} for s in bot_specs if s["kind"] == "goal"]
        week_rows = []

        if reset:
            # ПЕРЕПИСАТЬ блок с нуля: все недели месяца по порядку в строки блока (свежий клон)
            if weeks is None:
                weeks = _month_weeks(month_from, query_to)
            block_rows, _foot = _block_bounds(values, bot_h)
            for i, ridx in enumerate(block_rows):
                tr = ridx + 1
                if i < len(weeks):
                    wf, wt, wq = weeks[i]
                    wdata = account_period(token, login, wf, wq, bot_goals, attribution,
                                           want_positions=True)
                    tf = formulas[ridx] if ridx < len(formulas) else tf_bot
                    sample = next((str(values[ridx][s["idx"]]) for s in bot_specs
                                   if s["kind"] == "period" and s["idx"] < len(values[ridx])), "")
                    wrow = build_weekly_row(bot_specs, wdata, wf, wt, tr, tr, tf, sample, "week")
                    for j, s in enumerate(bot_specs):
                        if s["idx"] == 0 or _is_input(s["kind"]):
                            batch.append({"range": rowcol_to_a1(tr, s["idx"] + 1), "values": [[wrow[j]]]})
                    week_rows.append(tr)
                else:                       # лишняя строка клона — чистим входные ячейки
                    for s in bot_specs:
                        if s["idx"] == 0 or _is_input(s["kind"]):
                            batch.append({"range": rowcol_to_a1(tr, s["idx"] + 1), "values": [[""]]})
            info["reset"] = True
        else:
            # UPSERT переданных недель в существующий лист (добавить/обновить)
            if not weeks:
                y, m, d = (int(x) for x in query_to.split("-"))
                today = _d(y, m, d)
                monday = today.fromordinal(today.toordinal() - today.weekday())
                weeks = [(monday.isoformat(),
                          monday.fromordinal(monday.toordinal() + 6).isoformat(), query_to)]
            bperiods = [i for i in _block_bounds(values, bot_h)[0]
                        if values[i] and re.search(r"\d", str(values[i][0]))
                        and _norm(values[i][0]) not in _NONPERIOD]
            append_at = (bperiods[-1] if bperiods else bot_h) + 1
            last_p = bperiods[-1] if bperiods else bot_h
            for (wf, wt, wq) in weeks:
                wdata = account_period(token, login, wf, wq, bot_goals, attribution, want_positions=True)
                existing = next((i for i in bperiods if _row_start_iso(values[i][0]) == wf), None)
                if existing is not None:
                    tr, tmpl_i = existing + 1, existing
                else:
                    tr, tmpl_i = append_at + 1, last_p
                    append_at += 1
                sample = next((str(values[tmpl_i][s["idx"]]) for s in bot_specs
                               if s["kind"] == "period" and s["idx"] < len(values[tmpl_i])), "")
                wrow = build_weekly_row(bot_specs, wdata, wf, wt, tr, tmpl_i + 1,
                                        formulas[tmpl_i], sample, "week")
                for j, s in enumerate(bot_specs):
                    if s["idx"] == 0 or _is_input(s["kind"]):
                        batch.append({"range": rowcol_to_a1(tr, s["idx"] + 1), "values": [[wrow[j]]]})
                week_rows.append(tr)
        info["week_rows"] = week_rows

    if dry_run:
        return dict(info, batch_size=len(batch))
    if batch:
        ws.batch_update(batch, value_input_option="USER_ENTERED")
    return dict(info, written=True)


# ---- высокоуровневая выгрузка по клиенту (общая для кнопки и headless-синка) ----
def push_timeseries(gc, sid, token, login, all_goals):
    """Заполняет листы-ленты («по неделям»/«по месяцам») + составной лист текущего месяца:
    ПРОШЛЫЙ закрытый период (финализация) + ТЕКУЩИЙ (live до сегодня). Список результатов."""
    from datetime import date, timedelta
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    prev_monday = monday - timedelta(days=7)
    first = today.replace(day=1)
    prev_last = first - timedelta(days=1)
    prev_first = prev_last.replace(day=1)
    tod = today.isoformat()

    def _wk(mon, q):
        return mon.isoformat(), (mon + timedelta(days=6)).isoformat(), q
    weeks = [_wk(prev_monday, (monday - timedelta(days=1)).isoformat()), _wk(monday, tod)]
    months = [(prev_first.isoformat(), prev_last.isoformat(), prev_last.isoformat()),
              (first.isoformat(), tod, tod)]
    plan = {"week": weeks, "month": months}
    mode = {"update": "обновлено", "insert": "добавлено", "append": "добавлено"}
    cur_key = "{:04d}-{:02d}".format(today.year, today.month)
    comp_weeks = [w for w in weeks if w[0][:7] == first.isoformat()[:7]]
    # составной лист текущего месяца: создать (клон прошлого), если его ещё нет
    md_new = False
    try:
        _md_ws, md_new = ensure_month_detail(gc, sid, first.isoformat())
    except Exception:  # noqa: BLE001
        md_new = False
    sh = gc.open_by_key(sid)
    results = []
    for ws in sh.worksheets():
        t = ws.title.lower()
        grain = "week" if "по неделям" in t else ("month" if "по месяц" in t else None)
        try:
            if grain:
                parts = []
                for df, dl, qt in plan[grain]:
                    r = fill_weekly(ws, token, login, all_goals, df, dl, query_to=qt,
                                    dry_run=False, grain=grain)
                    parts.append("{} — {} (стр {})".format(
                        df, mode.get(r.get("mode"), "записано"), r.get("target_row")))
                status = "; ".join(parts)
            elif _label_key(ws.title, "month") == cur_key:
                r = fill_month_detail(ws, token, login, all_goals, first.isoformat(), tod,
                                      weeks=(None if md_new else comp_weeks),
                                      reset=md_new, dry_run=False)
                grain = "month-detail"
                status = "{}кампаний {}, недель {}".format(
                    "СОЗДАН + " if md_new else "", r.get("campaigns"), len(r.get("week_rows") or []))
            else:
                continue
        except Exception as e:  # noqa: BLE001 — один лист не должен ронять остальные
            grain = grain or "?"
            status = "ошибка: " + str(e)
        results.append({"tab": ws.title, "grain": grain, "status": status})
    return results


def goals_for_login(token, login):
    """Все цели клиента из Метрики БЕЗ обращения к локальной базе (для headless-синка):
    счётчики из настроек кампаний → их цели."""
    from . import yandex, metrika
    goals, seen = [], set()
    try:
        counters = yandex.get_campaign_counters(token, login)
    except Exception:  # noqa: BLE001
        counters = []
    for cid in counters:
        try:
            gs = metrika.get_counter_goals(token, cid)
        except Exception:  # noqa: BLE001 — 403/нет доступа: пропускаем счётчик
            continue
        for g in gs:
            if g["id"] not in seen:
                seen.add(g["id"])
                goals.append(g)
    return goals


def sync_all(token, log=None, do_breakdowns=False):
    """Headless-выгрузка по ВСЕМ клиентам с таблицами (для cron/планировщика). Без локальной базы:
    таблицы берём из Drive (расшарены на сервисный аккаунт), логин — по домену из agencyclients."""
    from datetime import date
    from . import yandex
    log = log or (lambda *a: None)
    gc = client(readonly=False)
    sheets = discover(gc)                                   # {домен: {id,title}}
    dom2login = {}
    for c in yandex.get_agency_clients(token):
        info = str(c.get("ClientInfo") or "").strip().lower().replace("www.", "")
        if info:
            dom2login[info] = c.get("Login")
    out = []
    for domain, sh in sorted(sheets.items()):
        login = dom2login.get(domain) or dom2login.get(domain.replace("www.", ""))
        if not login:
            log("· {}: логин не найден в agencyclients — пропуск".format(domain))
            out.append({"domain": domain, "ok": False, "error": "логин не найден"})
            continue
        try:
            goals = goals_for_login(token, login)
            results = push_timeseries(gc, sh["id"], token, login, goals)
            if do_breakdowns:
                t = date.today()
                for which in BREAKDOWNS:
                    try:
                        push_breakdown(gc, sh["id"], token, login, which,
                                       t.replace(day=1).isoformat(), t.isoformat())
                    except Exception as e:  # noqa: BLE001
                        log("    ! {} разрез {}: {}".format(domain, which, str(e)[:80]))
            errs = [r for r in results if r["status"].startswith("ошибка")]
            log("  ✓ {} ({}): листов {}{}".format(
                domain, login, len(results), (", ошибок " + str(len(errs))) if errs else ""))
            out.append({"domain": domain, "login": login, "ok": True,
                        "tabs": len(results), "errors": len(errs)})
        except Exception as e:  # noqa: BLE001
            log("  ✗ {} ({}): {}".format(domain, login, str(e)[:120]))
            out.append({"domain": domain, "login": login, "ok": False, "error": str(e)})
    return out
