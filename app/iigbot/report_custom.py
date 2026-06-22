# -*- coding: utf-8 -*-
"""Конструктор отчётов: произвольный разрез Директа за свободный период.

Те же запросы Reports API, что и недельная рассылка, но с выбором уровня
(аккаунт/кампании/группы/объявления/фразы/поисковые запросы), модели атрибуции и дат.
Конверсии считаются по целям клиента под выбранной атрибуцией (если цели заданы).
"""
from . import report as R

# уровень -> (тип отчёта Директа, поля-измерения, заголовки колонок-измерений)
LEVELS = {
    "account":     ("ACCOUNT_PERFORMANCE_REPORT", [], []),
    "campaign":    ("CAMPAIGN_PERFORMANCE_REPORT", ["CampaignName"], ["Кампания"]),
    "adgroup":     ("ADGROUP_PERFORMANCE_REPORT", ["CampaignName", "AdGroupName"], ["Кампания", "Группа"]),
    "ad":          ("AD_PERFORMANCE_REPORT", ["CampaignName", "AdGroupName", "AdId"], ["Кампания", "Группа", "ID объявл."]),
    "keyword":     ("CRITERIA_PERFORMANCE_REPORT", ["CampaignName", "AdGroupName", "Criterion"], ["Кампания", "Группа", "Фраза"]),
    "searchquery": ("SEARCH_QUERY_PERFORMANCE_REPORT", ["CampaignName", "Query"], ["Кампания", "Запрос"]),
}
LEVEL_LABELS = {
    "account": "Аккаунт (сводка)", "campaign": "Кампании", "adgroup": "Группы объявлений",
    "ad": "Объявления", "keyword": "Фразы", "searchquery": "Поисковые запросы",
}
# модели атрибуции Директа (максимум): обычные + кросс-девайс (…D) + авто
ATTRIBUTION_MODELS = ["LSC", "LC", "FC", "LYDC", "LSCD", "LCD", "FCD", "LYDCD", "AUTO"]
CONV_LEVELS = {"account", "campaign", "adgroup", "ad", "keyword"}   # где доступны конверсии по целям


def options():
    return {
        "levels": [{"key": k, "label": LEVEL_LABELS[k]} for k in LEVELS],
        "attributions": ATTRIBUTION_MODELS,
        "conv_levels": sorted(CONV_LEVELS),
    }


def _metrics(imp, clk, cost, conv):
    return {
        "imp": imp, "clicks": clk, "cost": cost, "conv": conv,
        "ctr": (clk / imp * 100 if imp else 0),
        "cpc": (cost / clk if clk else 0),
        "cr": (conv / clk * 100 if clk else 0),
        "cpa": (cost / conv if conv else 0),
    }


def build(token, login, level, date_from, date_to, attribution="LSC", goal_defs=None, limit=100):
    if level not in LEVELS:
        raise RuntimeError("Неизвестный разрез: {}".format(level))
    rtype, dims, dim_titles = LEVELS[level]
    goal_defs = goal_defs or []
    use_conv = bool(goal_defs) and level in CONV_LEVELS
    goal_ids = [g["id"] for g in goal_defs] if use_conv else None
    attribution = attribution if attribution in ATTRIBUTION_MODELS else "LSC"

    fields = list(dims) + ["Impressions", "Clicks", "Cost"]
    if use_conv:
        fields.append("Conversions")

    raw = R.fetch_report(token, login, date_from, date_to, fields,
                         goal_ids=goal_ids, attribution=attribution, report_type=rtype)

    rows = []
    t_imp = t_clk = t_cost = t_conv = 0.0
    for r in raw:
        imp = R.parse_num(r.get("Impressions"))
        clk = R.parse_num(r.get("Clicks"))
        cost = R.parse_num(r.get("Cost"))
        conv = 0.0
        if use_conv:
            for g in goal_defs:
                col = R._find_goal_col(r, g["id"])
                if col:
                    conv += R.parse_num(r.get(col))
        dim_vals = [str(r.get(d) or "") for d in dims] if dims else ["Весь аккаунт"]
        rows.append({"dims": dim_vals, "imp": imp, "clk": clk, "cost": cost, "conv": conv})
        t_imp += imp; t_clk += clk; t_cost += cost; t_conv += conv

    rows.sort(key=lambda x: -x["cost"])
    n_total = len(rows)
    rows = rows[:max(1, int(limit or 100))]
    out_rows = [{"dims": r["dims"], "m": _metrics(r["imp"], r["clk"], r["cost"], r["conv"])} for r in rows]

    return {
        "level": level, "level_label": LEVEL_LABELS[level],
        "dim_titles": dim_titles or ["Аккаунт"],
        "attribution": attribution, "use_conv": use_conv,
        "date_from": date_from, "date_to": date_to,
        "rows": out_rows, "totals": _metrics(t_imp, t_clk, t_cost, t_conv),
        "n_total": n_total, "n_shown": len(out_rows),
    }


def to_text(login, client_name, res, top=25):
    """Текст для Telegram/копирования (топ-N строк по расходу)."""
    L = [
        "Отчёт: {} ({})".format(client_name or login, login),
        "Разрез: {} · Атрибуция: {} · Период: {} — {}".format(
            res["level_label"], res["attribution"], res["date_from"], res["date_to"]),
        "",
        "ИТОГО:",
        "  Расход: {}  Показы: {}  Клики: {}".format(
            R.fmt_money(res["totals"]["cost"]), R.fmt_int(res["totals"]["imp"]), R.fmt_int(res["totals"]["clicks"])),
        "  CTR: {}  CPC: {}".format(R.fmt_pct(res["totals"]["ctr"]), R.fmt_money(res["totals"]["cpc"])),
    ]
    if res["use_conv"]:
        L.append("  Конверсии: {}  CR: {}  CPA: {}".format(
            R.fmt_int(res["totals"]["conv"]), R.fmt_pct(res["totals"]["cr"]), R.fmt_money(res["totals"]["cpa"])))
    if res["level"] != "account":
        L.append("")
        L.append("Топ {} по расходу:".format(min(top, res["n_shown"])))
        for i, row in enumerate(res["rows"][:top], 1):
            name = " / ".join([d for d in row["dims"] if d]) or "—"
            m = row["m"]
            line = "{}) {} — {}, кликов {}".format(i, name, R.fmt_money(m["cost"]), R.fmt_int(m["clicks"]))
            if res["use_conv"]:
                line += ", конв. {}".format(R.fmt_int(m["conv"]))
            L.append(line)
        if res["n_total"] > res["n_shown"]:
            L.append("… показано {} из {} строк.".format(res["n_shown"], res["n_total"]))
    return "\n".join(L)
