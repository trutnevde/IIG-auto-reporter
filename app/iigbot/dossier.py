# -*- coding: utf-8 -*-
"""Досье по проекту: сравнение двух периодов — «что сделано» и «что случилось».

Всё в модуле построено на паре периодов A и B, которые задаются снаружи:
A — то, что смотрим, B — то, с чем сравниваем. Они не обязаны идти подряд и
не обязаны быть одной длины: можно сравнить неделю с той же неделей год назад,
месяц с прошлым месяцем или два любых куска. Кабинет считает даты сам и
присылает готовые — здесь никакой «магии по умолчанию».

Запросы к Reports API:
  1) кампании за A;
  2) кампании за B;
  3) аккаунт с разбивкой по датам за A — динамика внутри периода;
  4) по паре запросов на каждый включённый разрез (устройства, сеть, гео,
     возраст, пол, поисковые запросы) — тоже A против B.

Выводы считаются на правилах, без внешних моделей: результат детерминированный,
не требует ключей и не стоит денег.
"""
from datetime import date as _date, timedelta as _td

from . import report as R
from . import report_custom as RC

# Ниже этого порога изменение считаем шумом и не выносим в выводы
NOISE_PCT = 8.0
# Сколько строк показывать в каждую сторону
TOP_DRIVERS = 3

# Разрезы: ключ -> (уровень, сегмент, заголовок, отдаёт ли Директ конверсии).
# У поисковых запросов конверсий нет — Директ их для этого отчёта не считает,
# поэтому там сравниваем по кликам и честно об этом пишем.
CUTS = {
    "device":      ("account", "device",  "Устройства",        True),
    "network":     ("account", "network", "Поиск и РСЯ",       True),
    "geo":         ("account", "geo",     "География",         True),
    "age":         ("account", "age",     "Возраст",           True),
    "gender":      ("account", "gender",  "Пол",               True),
    "searchquery": ("searchquery", None,  "Поисковые запросы", False),
}
CUT_ORDER = ["network", "device", "geo", "age", "gender", "searchquery"]


def cut_options():
    return [{"key": k, "label": CUTS[k][2], "has_conv": CUTS[k][3]} for k in CUT_ORDER]


def _d(s):
    return _date(int(s[0:4]), int(s[5:7]), int(s[8:10]))


def _days(a, b):
    return (_d(b) - _d(a)).days + 1


def pick_grain(date_from, date_to):
    """Дни для короткого периода, недели для среднего, месяцы для длинного."""
    n = _days(date_from, date_to)
    if n <= 31:
        return "day"
    if n <= 120:
        return "week"
    return "month"


def _pct(now, was):
    """Изменение в процентах; None — если сравнивать не с чем."""
    if not was:
        return None if not now else 100.0
    return (now - was) / was * 100.0


def _delta(now, was):
    return {"now": now, "was": was, "abs": now - was, "pct": _pct(now, was)}


def _rows_by_name(res):
    """Строки отчёта: читаемое имя -> метрики (одноимённые складываем)."""
    out = {}
    for r in res.get("rows") or []:
        name = " / ".join(str(d) for d in (r.get("dims") or []) if d) or "—"
        m = r["m"]
        cur = out.get(name)
        if cur is None:
            out[name] = dict(m)
        else:
            for k in ("imp", "clicks", "cost", "conv"):
                cur[k] += m[k]
    return out


def _remetric(m):
    """Пересчитать производные метрики после сложения строк."""
    return RC._metrics(m["imp"], m["clicks"], m["cost"], m["conv"])


def _pair(a_rows, b_rows, key):
    """Сопоставить строки двух периодов по имени."""
    out = []
    for name in set(list(a_rows.keys()) + list(b_rows.keys())):
        m_now = a_rows.get(name) or {"imp": 0, "clicks": 0, "cost": 0, "conv": 0}
        m_was = b_rows.get(name) or {"imp": 0, "clicks": 0, "cost": 0, "conv": 0}
        out.append({
            "name": name,
            "now": _remetric(m_now), "was": _remetric(m_was),
            "d_key": m_now[key] - m_was[key],
            "d_cost": m_now["cost"] - m_was["cost"],
            "started": (name not in b_rows) and m_now["cost"] > 0,
            "stopped": (name not in a_rows) and m_was["cost"] > 0,
        })
    out.sort(key=lambda c: -c["now"]["cost"])
    return out


def build_cut(token, login, ck, a_from, a_to, b_from, b_to,
              attribution=None, goal_defs=None, _post=None, _sleep=None):
    """Один разрез отдельным вызовом: два отчёта Директа вместо пятнадцати за раз.

    Собирать все разрезы внутри одного HTTP-запроса нельзя — Reports API ставит
    каждый отчёт в очередь, и пятнадцать отчётов подряд занимают минуты, а на
    shared-хостинге один занятый процесс держит весь кабинет.
    """
    if ck not in CUTS:
        raise RuntimeError("Неизвестный разрез: {}".format(ck))
    level, seg, label, cut_conv = CUTS[ck]
    segs = [seg] if seg else None
    kw = dict(attribution=attribution, goal_defs=goal_defs, _post=_post, _sleep=_sleep)
    if _post is not None:
        ca = RC.build(token, login, level, a_from, a_to, segments=segs, limit=200, **kw)
        cb = RC.build(token, login, level, b_from, b_to, segments=segs, limit=200, **kw)
    else:   # два периода одного разреза независимы — считаем одновременно
        from concurrent import futures as _f
        with _f.ThreadPoolExecutor(max_workers=2) as ex:
            fa = ex.submit(RC.build, token, login, level, a_from, a_to,
                           segments=segs, limit=200, **kw)
            fb = ex.submit(RC.build, token, login, level, b_from, b_to,
                           segments=segs, limit=200, **kw)
            ca, cb = fa.result(), fb.result()
    has_conv = cut_conv and ((ca["totals"]["conv"] > 0) or (cb["totals"]["conv"] > 0))
    ckey = "conv" if has_conv else "clicks"
    rows = _pair(_rows_by_name(ca), _rows_by_name(cb), ckey)
    rows = [r for r in rows if r["now"]["cost"] > 0 or r["was"]["cost"] > 0][:12]
    # Итог по ВСЕМУ разрезу (до среза в двенадцать строк) — для честной доли лидера.
    полный = 0
    try:
        полный = float((ca.get("totals") or {}).get(ckey) or 0)
    except (TypeError, ValueError):
        полный = 0
    cut = {"key": ck, "label": label, "has_conv": has_conv, "compare_by": ckey,
           "rows": rows, "total_now": полный}
    cut["summary"] = cut_summary({"compare_by": ckey}, cut)
    return cut


def build(token, login, client_name, a_from, a_to, b_from, b_to,
          attribution=None, goal_defs=None, cuts=None,
          client_note=None, signature=None, sheet_url=None, today=None,
          _post=None, _sleep=None):
    grain = pick_grain(a_from, a_to)
    kw = dict(attribution=attribution, goal_defs=goal_defs, _post=_post, _sleep=_sleep)

    # Три независимых отчёта: текущий период, прошлый и динамика. Раньше шли подряд, и
    # при медленном Директе досье не успевало собраться до таймаута — теперь одновременно.
    def _cur():
        return RC.build(token, login, "campaign", a_from, a_to, limit=500, **kw)

    def _prev():
        return RC.build(token, login, "campaign", b_from, b_to, limit=500, **kw)

    def _dyn():
        return RC.build(token, login, "account", a_from, a_to,
                        segments=["date"], date_grain=grain, limit=500, **kw)

    if _post is not None:
        cur, prev, dyn = _cur(), _prev(), _dyn()
    else:
        from concurrent import futures as _f
        with _f.ThreadPoolExecutor(max_workers=3) as ex:
            f1, f2, f3 = ex.submit(_cur), ex.submit(_prev), ex.submit(_dyn)
            cur, prev, dyn = f1.result(), f2.result(), f3.result()

    t_now, t_was = cur["totals"], prev["totals"]
    totals = {k: _delta(t_now[k], t_was[k])
              for k in ("imp", "clicks", "cost", "conv", "ctr", "cpc", "cr", "cpa")}

    has_conv = (t_now["conv"] > 0) or (t_was["conv"] > 0)
    key = "conv" if has_conv else "clicks"

    camps = _pair(_rows_by_name(cur), _rows_by_name(prev), key)

    # ── ЧТО СДЕЛАНО: запуски, остановки, перекладка бюджета
    started = [c for c in camps if c["started"]]
    stopped = [c for c in camps if c["stopped"]]
    mix = []
    if t_now["cost"] and t_was["cost"]:
        for c in camps:
            if c["started"] or c["stopped"]:
                continue                      # про них уже сказано отдельно
            s_now = c["now"]["cost"] / t_now["cost"] * 100
            s_was = c["was"]["cost"] / t_was["cost"] * 100
            if abs(s_now - s_was) >= 5:       # доли меньше 5 п.п. — не перекладка, а колебание
                mix.append({"name": c["name"], "share_now": s_now, "share_was": s_was,
                            "d": s_now - s_was})
        mix.sort(key=lambda x: -abs(x["d"]))
        mix = mix[:4]

    # ── ЧТО СЛУЧИЛОСЬ: драйверы, эффективность, слив
    ups = sorted([c for c in camps if c["d_key"] > 0], key=lambda c: -c["d_key"])[:TOP_DRIVERS]
    downs = sorted([c for c in camps if c["d_key"] < 0], key=lambda c: c["d_key"])[:TOP_DRIVERS]
    scored = [c for c in camps if c["now"]["conv"] >= 3]
    best = min(scored, key=lambda c: c["now"]["cpa"]) if scored else None
    spend_cut = max(500.0, t_now["cost"] * 0.03)
    wasted = sorted([c for c in camps if c["now"]["conv"] == 0 and c["now"]["cost"] >= spend_cut],
                    key=lambda c: -c["now"]["cost"])[:TOP_DRIVERS]

    # ── цели по отдельности: не только «сколько заявок», но и каких именно
    goals = []
    gt_now, gt_was = cur.get("goal_totals") or {}, prev.get("goal_totals") or {}
    for g in (cur.get("goals") or []):
        n, w = gt_now.get(g["id"], 0), gt_was.get(g["id"], 0)
        if n or w:
            goals.append({"id": g["id"], "name": g["name"], **_delta(n, w)})
    goals.sort(key=lambda x: -x["now"])

    # ── динамика внутри периода
    series = []
    for r in dyn.get("rows") or []:
        series.append({"label": (r.get("dims") or ["—"])[0], **r["m"]})
    series.sort(key=lambda x: x["label"])
    peak = max(series, key=lambda x: x[key]) if series else None

    # ── прогноз: только когда период A — это текущий месяц с начала и по вчера.
    #    Иначе пришлось бы догадываться, сколько уже накоплено с 1-го числа.
    forecast = None
    today = _d(today) if today else _date.today()
    a1, a2 = _d(a_from), _d(a_to)
    if a1.day == 1 and a1.year == today.year and a1.month == today.month and a2 <= today:
        gone = (a2 - a1).days + 1
        nxt = a2 + _td(days=1)
        month_end = (_date(a2.year + (a2.month == 12), (a2.month % 12) + 1, 1) - _td(days=1))
        left = (month_end - a2).days
        if gone >= 3 and left > 0:
            rate_conv = t_now["conv"] / gone
            rate_cost = t_now["cost"] / gone
            forecast = {
                "days_gone": gone, "days_left": left,
                "from": nxt.isoformat(), "to": month_end.isoformat(),
                "conv": t_now["conv"] + rate_conv * left,
                "cost": t_now["cost"] + rate_cost * left,
                "per_day_conv": rate_conv, "per_day_cost": rate_cost,
            }

    # разрезы кабинет запрашивает по одному отдельными вызовами — см. build_cut
    cut_res = []

    res = {
        "login": login, "client_name": client_name or login,
        "a_from": a_from, "a_to": a_to, "b_from": b_from, "b_to": b_to,
        "a_days": _days(a_from, a_to), "b_days": _days(b_from, b_to),
        "same_length": _days(a_from, a_to) == _days(b_from, b_to),
        "grain": grain, "attribution": cur.get("attribution"),
        "has_conv": has_conv, "compare_by": key,
        "totals": totals, "goals": goals,
        "campaigns": camps[:25], "n_campaigns": len(camps),
        "started": started, "stopped": stopped, "mix": mix,
        "ups": ups, "downs": downs, "best": best, "wasted": wasted,
        "series": series, "peak": peak, "forecast": forecast, "cuts": cut_res,
        "client_note": (client_note or "").strip(),
        "signature": (signature or "").strip(),
        "sheet_url": sheet_url or "",
    }
    res["done"] = done_items(res)
    res["happened"] = happened_items(res)
    res["text"] = to_text(res)
    return res


# ────────────────────────────── формулировки ──────────────────────────────

def _ru_date(iso, with_year=False):
    M = ["января", "февраля", "марта", "апреля", "мая", "июня",
         "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    d = _d(iso)
    return "{} {}{}".format(d.day, M[d.month - 1], " " + str(d.year) if with_year else "")


def period_label(a, b):
    """«20 июля — 2 августа 2026»; год пишем один раз, если он общий."""
    da, db = _d(a), _d(b)
    if da.year == db.year:
        return "{} — {} {}".format(_ru_date(a), _ru_date(b), db.year)
    return "{} — {}".format(_ru_date(a, True), _ru_date(b, True))


def _plural(n, one, few, many):
    n = abs(int(round(n)))
    m, h = n % 10, n % 100
    if m == 1 and h != 11:
        return one
    if 2 <= m <= 4 and (h < 10 or h >= 20):
        return few
    return many


def _units(res, n):
    if res["compare_by"] == "conv":
        return _plural(n, "заявку", "заявки", "заявок")
    return _plural(n, "переход", "перехода", "переходов")


def _short(name, limit=48):
    name = str(name or "")
    return name if len(name) <= limit else name[:limit - 1].rstrip() + "…"


def _sign(pct, good_up=True):
    if pct is None or abs(pct) < NOISE_PCT:
        return "flat"
    return "good" if ((pct > 0) == good_up) else "bad"


def _chg(pct, up_word="больше", down_word="меньше"):
    if pct is None:
        return ""
    return "на {:.0f}% {}".format(abs(pct), up_word if pct > 0 else down_word)


def done_items(res):
    """ЧТО СДЕЛАНО — действия за период, а не следствия."""
    out = []
    st, sp, mix = res["started"], res["stopped"], res["mix"]

    if st and sp:
        out.append({"tone": "flat", "icon": "refresh", "text": "Перестроили структуру: остановили {}, запустили {}.".format(
            ", ".join("«" + _short(c["name"]) + "»" for c in sp),
            ", ".join("«" + _short(c["name"]) + "»" for c in st))})
    else:
        if st:
            out.append({"tone": "good", "icon": "add-circle", "text": "Запустили {}: {}.".format(
                _plural(len(st), "кампанию", "кампании", "кампаний"),
                ", ".join("«" + _short(c["name"]) + "»" for c in st))})
        if sp:
            out.append({"tone": "flat", "icon": "forbid-2", "text": "Остановили {}: {}.".format(
                _plural(len(sp), "кампанию", "кампании", "кампаний"),
                ", ".join("«" + _short(c["name"]) + "»" for c in sp))})

    for m in mix:
        word = "увеличили" if m["d"] > 0 else "сократили"
        out.append({"tone": "flat", "icon": "wallet-3",
                    "text": "Долю «{}» в бюджете {} с {:.0f}% до {:.0f}%.".format(
                        _short(m["name"]), word, m["share_was"], m["share_now"])})

    if res["client_note"]:
        out.append({"tone": "flat", "icon": "draft", "text": res["client_note"]})

    if not out:
        out.append({"tone": "flat", "icon": "checkbox-circle",
                    "text": "Структура кампаний не менялась — работали с тем, что было: ставки, площадки, минус-слова."})
    return out


def happened_items(res):
    """ЧТО СЛУЧИЛОСЬ — результат периода."""
    T, out = res["totals"], []
    conv, cost, cpa, clicks = T["conv"], T["cost"], T["cpa"], T["clicks"]

    if res["has_conv"]:
        if conv["pct"] is None or abs(conv["pct"]) < NOISE_PCT:
            out.append({"tone": "flat", "icon": "equalizer",
                        "text": "Заявок примерно столько же: {} против {}.".format(
                            R.fmt_int(conv["now"]), R.fmt_int(conv["was"]))})
        else:
            out.append({"tone": _sign(conv["pct"]), "icon": "focus-3",
                        "text": "Заявок {}: {} против {}.".format(
                            _chg(conv["pct"]), R.fmt_int(conv["now"]), R.fmt_int(conv["was"]))})
        if cpa["was"] and cpa["now"] and abs(cpa["pct"] or 0) >= NOISE_PCT:
            out.append({"tone": _sign(cpa["pct"], good_up=False), "icon": "wallet",
                        "text": "Цена заявки {}: {} против {}.".format(
                            _chg(cpa["pct"], "выше", "ниже"),
                            R.fmt_money(cpa["now"]), R.fmt_money(cpa["was"]))})
    else:
        out.append({"tone": _sign(clicks["pct"]), "icon": "links",
                    "text": "Переходов на сайт: {}{}.".format(
                        R.fmt_int(clicks["now"]),
                        (", " + _chg(clicks["pct"])) if clicks["pct"] is not None else "")})

    if cost["pct"] is not None and abs(cost["pct"]) >= NOISE_PCT:
        out.append({"tone": "flat", "icon": "wallet-3", "text": "Расход {}: {} против {}.".format(
            _chg(cost["pct"]), R.fmt_money(cost["now"]), R.fmt_money(cost["was"]))})

    # драйверы считаем среди кампаний, работавших в обоих периодах: у запущенных
    # и остановленных изменение объясняется самим фактом запуска или остановки
    ups = [c for c in res["ups"] if not c["started"]]
    downs = [c for c in res["downs"] if not c["stopped"]]
    if ups:
        c = ups[0]
        out.append({"tone": "good", "icon": "checkbox-circle",
                    "text": "Лучше других сработала «{}»: на {} {} больше.".format(
                        _short(c["name"]), R.fmt_int(c["d_key"]), _units(res, c["d_key"]))})
    if downs:
        c = downs[0]
        out.append({"tone": "bad", "icon": "alert",
                    "text": "Просела «{}»: на {} {} меньше.".format(
                        _short(c["name"]), R.fmt_int(abs(c["d_key"])), _units(res, c["d_key"]))})

    # цели: что именно изменилось в характере заявок
    for g in res["goals"][:4]:
        if g["pct"] is not None and abs(g["pct"]) >= 25 and (g["now"] >= 3 or g["was"] >= 3):
            out.append({"tone": _sign(g["pct"]), "icon": "focus-3",
                        "text": "{}: {} — {} против {}.".format(
                            g["name"], _chg(g["pct"]), R.fmt_int(g["now"]), R.fmt_int(g["was"]))})

    # Про «не дала ни одной заявки» можно говорить, только если заявки вообще
    # измеряются. Иначе это утверждение не о рекламе, а о нашем учёте.
    if res.get("has_conv") and res["wasted"]:
        w = res["wasted"][0]
        out.append({"tone": "bad", "icon": "error-warning",
                    "text": "«{}» израсходовала {} и не дала ни одной заявки — разбираем.".format(
                        _short(w["name"]), R.fmt_money(w["now"]["cost"]))})

    if res["best"] and res["best"]["now"]["cpa"]:
        b = res["best"]
        out.append({"tone": "good", "icon": "wallet",
                    "text": "Дешевле всего заявки даёт «{}» — {} при {} {}.".format(
                        _short(b["name"]), R.fmt_money(b["now"]["cpa"]), R.fmt_int(b["now"]["conv"]),
                        _plural(b["now"]["conv"], "заявке", "заявках", "заявках"))})

    f = res["forecast"]
    if f and res.get("has_conv"):
        out.append({"tone": "flat", "icon": "line-chart",
                    "text": "При текущем темпе ({} в день) к концу месяца выйдет около {} {} при расходе ~{}.".format(
                        round(f["per_day_conv"], 1), R.fmt_int(f["conv"]),
                        _plural(f["conv"], "заявки", "заявок", "заявок"), R.fmt_money(f["cost"]))})
    return out


def cut_summary(res, cut):
    """Одна фраза по разрезу: кто главный и кто дороже."""
    rows = [r for r in cut["rows"] if r["now"]["cost"] > 0]
    if not rows:
        return ""
    kk = cut["compare_by"]
    top = max(rows, key=lambda r: r["now"][kk])
    # Знаменатель — весь разрез, а не показанные двенадцать строк. В «Географии»
    # и «Поисковых запросах» строк сотни, и доля от верхушки завышена в разы.
    total = cut.get("total_now") or (sum(r["now"][kk] for r in rows) or 1)
    share = min(100.0, top["now"][kk] / total * 100)
    unit = "заявок" if kk == "conv" else "переходов"
    txt = "Больше всего {} даёт «{}» — {:.0f}% от всех".format(unit, _short(top["name"], 30), share)
    if cut["has_conv"]:
        scored = [r for r in rows if r["now"]["conv"] >= 3]
        if len(scored) > 1:
            cheap = min(scored, key=lambda r: r["now"]["cpa"])
            dear = max(scored, key=lambda r: r["now"]["cpa"])
            if dear["now"]["cpa"] and cheap["now"]["cpa"] and dear["now"]["cpa"] > cheap["now"]["cpa"] * 1.3:
                txt += "; дешевле всего заявка в «{}» ({}), дороже всего в «{}» ({})".format(
                    _short(cheap["name"], 24), R.fmt_money(cheap["now"]["cpa"]),
                    _short(dear["name"], 24), R.fmt_money(dear["now"]["cpa"]))
    return txt + "."


def to_text(res):
    """Готовый текст для клиента: сначала что сделали, потом что получилось."""
    T = res["totals"]
    L = ["{} — отчёт за {}".format(res["client_name"], period_label(res["a_from"], res["a_to"])), ""]
    L.append("Сравнение с периодом {}{}.".format(
        period_label(res["b_from"], res["b_to"]),
        "" if res["same_length"] else " (другой длины, поэтому сравниваем характер, а не объём)"))
    L.append("")

    L.append("ЧТО СДЕЛАЛИ")
    for d in res["done"]:
        L.append("  " + d["text"])
    L.append("")

    L.append("ЧТО ПОЛУЧИЛОСЬ")
    for h in res["happened"]:
        L.append("  " + h["text"])
    L.append("")

    L.append("ЦИФРЫ ЗА ПЕРИОД")
    L.append("  Расход: {}".format(R.fmt_money(T["cost"]["now"])))
    L.append("  Показы: {}   Клики: {}   CTR: {}".format(
        R.fmt_int(T["imp"]["now"]), R.fmt_int(T["clicks"]["now"]), R.fmt_pct(T["ctr"]["now"])))
    if res["has_conv"]:
        L.append("  Заявки: {}   Цена заявки: {}".format(
            R.fmt_int(T["conv"]["now"]), R.fmt_money(T["cpa"]["now"])))
        if res["goals"]:
            L.append("")
            L.append("  По целям (сейчас / было):")
            for g in res["goals"]:
                L.append("    {} — {} / {}".format(g["name"], R.fmt_int(g["now"]), R.fmt_int(g["was"])))

    top = [c for c in res["campaigns"] if c["now"]["cost"] > 0][:5]
    if top:
        L.append("")
        L.append("КРУПНЕЙШИЕ КАМПАНИИ")
        for c in top:
            line = "  {} — {}".format(c["name"], R.fmt_money(c["now"]["cost"]))
            if res["has_conv"]:
                line += ", заявок {}".format(R.fmt_int(c["now"]["conv"]))
                if c["now"]["cpa"]:
                    line += " по {}".format(R.fmt_money(c["now"]["cpa"]))
            L.append(line)

    for cut in res["cuts"]:
        s = cut_summary(res, cut)
        if not s:
            continue
        L.append("")
        L.append(cut["label"].upper())
        L.append("  " + s)
        for r in cut["rows"][:6]:
            if not r["now"]["cost"]:
                continue
            line = "    {} — {}".format(_short(r["name"], 40), R.fmt_money(r["now"]["cost"]))
            if cut["has_conv"]:
                line += ", заявок {}".format(R.fmt_int(r["now"]["conv"]))
            else:
                line += ", переходов {}".format(R.fmt_int(r["now"]["clicks"]))
            L.append(line)

    if res["sheet_url"]:
        L.append("")
        L.append("Подробная таблица: {}".format(res["sheet_url"]))
    if res["signature"]:
        L.append("")
        L.append(res["signature"])
    return "\n".join(L)
