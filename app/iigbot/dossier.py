# -*- coding: utf-8 -*-
"""Досье по проекту за произвольный период — готовая отписка клиенту.

Отличие от конструктора: тот отдаёт таблицу, а тут собирается СМЫСЛ.
Три запроса к Reports API:
  1) кампании за выбранный период;
  2) кампании за предыдущий равный период — чтобы было с чем сравнивать;
  3) аккаунт с разбивкой по датам — динамика внутри периода.
Дальше на правилах считаются выводы: что выросло, что упало и за счёт каких
кампаний, где деньги тратятся зря, что запустили и что остановили.

Никакой внешней модели: текст собирается детерминированно из цифр, поэтому
работает без ключей, бесплатно и одинаково на одних и тех же данных.
"""
from datetime import date as _date, timedelta as _td

from . import report as R
from . import report_custom as RC

# Порог, ниже которого изменение считаем шумом и не выносим в выводы
NOISE_PCT = 8.0
# Сколько кампаний-драйверов показывать в каждую сторону
TOP_DRIVERS = 3


def _d(s):
    return _date(int(s[0:4]), int(s[5:7]), int(s[8:10]))


def prev_period(date_from, date_to):
    """Предыдущий период той же длины, заканчивающийся за день до начала текущего."""
    a, b = _d(date_from), _d(date_to)
    days = (b - a).days + 1
    pb = a - _td(days=1)
    pa = pb - _td(days=days - 1)
    return pa.isoformat(), pb.isoformat()


def pick_grain(date_from, date_to):
    """Дни для короткого периода, недели для среднего, месяцы для длинного."""
    days = (_d(date_to) - _d(date_from)).days + 1
    if days <= 31:
        return "day"
    if days <= 120:
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
    """Кампании из результата конструктора: имя -> метрики."""
    out = {}
    for r in res.get("rows") or []:
        name = " / ".join(str(d) for d in (r.get("dims") or []) if d) or "—"
        m = r["m"]
        cur = out.get(name)
        if cur is None:
            out[name] = dict(m)
        else:                       # одинаковые имена кампаний встречаются — складываем
            for k in ("imp", "clicks", "cost", "conv"):
                cur[k] += m[k]
    return out


def _remetric(m):
    """Пересчитать производные метрики после сложения строк."""
    return RC._metrics(m["imp"], m["clicks"], m["cost"], m["conv"])


def build(token, login, client_name, date_from, date_to,
          attribution=None, goal_defs=None, _post=None, _sleep=None):
    grain = pick_grain(date_from, date_to)
    pf, pt = prev_period(date_from, date_to)

    kw = dict(attribution=attribution, goal_defs=goal_defs, _post=_post, _sleep=_sleep)
    cur = RC.build(token, login, "campaign", date_from, date_to, limit=500, **kw)
    prev = RC.build(token, login, "campaign", pf, pt, limit=500, **kw)
    dyn = RC.build(token, login, "account", date_from, date_to,
                   segments=["date"], date_grain=grain, limit=500, **kw)

    t_now, t_was = cur["totals"], prev["totals"]
    totals = {k: _delta(t_now[k], t_was[k])
              for k in ("imp", "clicks", "cost", "conv", "ctr", "cpc", "cr", "cpa")}

    a, b = _rows_by_name(cur), _rows_by_name(prev)
    has_conv = (t_now["conv"] > 0) or (t_was["conv"] > 0)
    key = "conv" if has_conv else "clicks"      # без конверсий сравниваем по кликам

    camps = []
    for name in set(list(a.keys()) + list(b.keys())):
        m_now = a.get(name) or {"imp": 0, "clicks": 0, "cost": 0, "conv": 0}
        m_was = b.get(name) or {"imp": 0, "clicks": 0, "cost": 0, "conv": 0}
        camps.append({
            "name": name,
            "now": _remetric(m_now), "was": _remetric(m_was),
            "d_key": m_now[key] - m_was[key],
            "d_cost": m_now["cost"] - m_was["cost"],
            "started": (name not in b) and m_now["cost"] > 0,     # появилась в этом периоде
            "stopped": (name not in a) and m_was["cost"] > 0,     # была и пропала
        })
    camps.sort(key=lambda c: -c["now"]["cost"])

    ups = sorted([c for c in camps if c["d_key"] > 0], key=lambda c: -c["d_key"])[:TOP_DRIVERS]
    downs = sorted([c for c in camps if c["d_key"] < 0], key=lambda c: c["d_key"])[:TOP_DRIVERS]

    # эффективность: считаем только там, где набралась статистика
    scored = [c for c in camps if c["now"]["conv"] >= 3]
    best = min(scored, key=lambda c: c["now"]["cpa"]) if scored else None
    # деньги без результата: тратили заметно, но не принесли ни одной конверсии
    spend_cut = max(500.0, t_now["cost"] * 0.03)
    wasted = [c for c in camps if c["now"]["conv"] == 0 and c["now"]["cost"] >= spend_cut]
    wasted.sort(key=lambda c: -c["now"]["cost"])

    series = []
    for r in dyn.get("rows") or []:
        label = (r.get("dims") or ["—"])[0]
        series.append({"label": label, **r["m"]})
    series.sort(key=lambda x: x["label"])
    peak = max(series, key=lambda x: x[key]) if series else None

    res = {
        "login": login, "client_name": client_name or login,
        "date_from": date_from, "date_to": date_to,
        "prev_from": pf, "prev_to": pt,
        "days": (_d(date_to) - _d(date_from)).days + 1,
        "grain": grain, "attribution": cur.get("attribution"),
        "goals": cur.get("goals") or [], "goal_totals": cur.get("goal_totals") or {},
        "has_conv": has_conv, "compare_by": key,
        "totals": totals, "campaigns": camps[:25], "n_campaigns": len(camps),
        "ups": ups, "downs": downs,
        "best": best, "wasted": wasted[:TOP_DRIVERS],
        "started": [c["name"] for c in camps if c["started"]][:5],
        "stopped": [c["name"] for c in camps if c["stopped"]][:5],
        "series": series, "peak": peak,
    }
    res["highlights"] = highlights(res)
    res["text"] = to_text(res)
    return res


def _ru_date(iso):
    M = ["января", "февраля", "марта", "апреля", "мая", "июня",
         "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    d = _d(iso)
    return "{} {}".format(d.day, M[d.month - 1])


def period_label(res):
    a, b = _d(res["date_from"]), _d(res["date_to"])
    if a.year == b.year:
        return "{} — {} {}".format(_ru_date(res["date_from"]), _ru_date(res["date_to"]), b.year)
    return "{} {} — {} {}".format(_ru_date(res["date_from"]), a.year, _ru_date(res["date_to"]), b.year)


def _sign(pct, good_up=True):
    """Метка настроения для подсветки в кабинете."""
    if pct is None or abs(pct) < NOISE_PCT:
        return "flat"
    up = pct > 0
    return "good" if (up == good_up) else "bad"


def _chg(pct, up_word="больше", down_word="меньше"):
    if pct is None:
        return ""
    return "на {:.0f}% {}".format(abs(pct), up_word if pct > 0 else down_word)


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


def highlights(res):
    """Выводы человеческим языком: что случилось и из-за чего."""
    T, out = res["totals"], []
    conv, cost, cpa = T["conv"], T["cost"], T["cpa"]
    started, stopped = res["started"], res["stopped"]

    # 1. Главный результат периода
    if res["has_conv"]:
        if conv["pct"] is None or abs(conv["pct"]) < NOISE_PCT:
            out.append({"tone": "flat", "text": "Заявок примерно столько же, сколько в прошлый период: {} против {}.".format(
                R.fmt_int(conv["now"]), R.fmt_int(conv["was"]))})
        else:
            out.append({"tone": _sign(conv["pct"]), "text": "Заявок {}: {} против {} в прошлый период.".format(
                _chg(conv["pct"]), R.fmt_int(conv["now"]), R.fmt_int(conv["was"]))})
        if cpa["was"] and cpa["now"] and abs(cpa["pct"] or 0) >= NOISE_PCT:
            out.append({"tone": _sign(cpa["pct"], good_up=False),
                        "text": "Цена заявки {}: {} против {}.".format(
                            _chg(cpa["pct"], "выше", "ниже"), R.fmt_money(cpa["now"]), R.fmt_money(cpa["was"]))})
    else:
        out.append({"tone": _sign(T["clicks"]["pct"]), "text": "Переходов на сайт: {}{}.".format(
            R.fmt_int(T["clicks"]["now"]),
            (", " + _chg(T["clicks"]["pct"])) if T["clicks"]["pct"] is not None else "")})

    # 2. Деньги
    if cost["pct"] is not None and abs(cost["pct"]) >= NOISE_PCT:
        out.append({"tone": "flat", "text": "Расход {}: {} против {}.".format(
            _chg(cost["pct"]), R.fmt_money(cost["now"]), R.fmt_money(cost["was"]))})

    # 3. Перезапуск кампаний — одной фразой, иначе одна и та же кампания попадёт
    #    и в «запустили», и в «больше всего добавила», и выйдет каша
    if started and stopped:
        out.append({"tone": "flat", "text": "Перестроили структуру: остановили {}, вместо неё запустили {}.".format(
            ", ".join("«" + _short(n) + "»" for n in stopped),
            ", ".join("«" + _short(n) + "»" for n in started))})
    elif started:
        out.append({"tone": "flat", "text": "Запустили: {}.".format(
            ", ".join("«" + _short(n) + "»" for n in started))})
    elif stopped:
        out.append({"tone": "flat", "text": "Остановили: {}.".format(
            ", ".join("«" + _short(n) + "»" for n in stopped))})

    # 4. За счёт чего изменился результат — только среди кампаний, работавших оба периода:
    #    у новых и остановленных изменение объясняется самим фактом запуска или остановки
    ups = [c for c in res["ups"] if not c["started"]]
    downs = [c for c in res["downs"] if not c["stopped"]]
    if ups:
        c = ups[0]
        out.append({"tone": "good", "text": "Лучше других сработала «{}»: на {} {} больше.".format(
            _short(c["name"]), R.fmt_int(c["d_key"]), _units(res, c["d_key"]))})
    if downs:
        c = downs[0]
        out.append({"tone": "bad", "text": "Просела «{}»: на {} {} меньше.".format(
            _short(c["name"]), R.fmt_int(abs(c["d_key"])), _units(res, c["d_key"]))})

    # 5. Где деньги уходят впустую
    if res["wasted"]:
        w = res["wasted"][0]
        out.append({"tone": "bad", "text": "«{}» израсходовала {} и не дала ни одной заявки — разбираем.".format(
            _short(w["name"]), R.fmt_money(w["now"]["cost"]))})

    # 6. Что работает лучше всего
    if res["best"] and res["best"]["now"]["cpa"]:
        b = res["best"]
        out.append({"tone": "good", "text": "Дешевле всего заявки даёт «{}» — {} при {} {}.".format(
            _short(b["name"]), R.fmt_money(b["now"]["cpa"]), R.fmt_int(b["now"]["conv"]),
            _plural(b["now"]["conv"], "заявке", "заявках", "заявках"))})
    return out


def to_text(res):
    """Готовый текст для отправки клиенту — правится руками перед отправкой."""
    T = res["totals"]
    L = ["{} — итоги за {}".format(res["client_name"], period_label(res)), ""]
    L.append("Расход: {}".format(R.fmt_money(T["cost"]["now"])))
    L.append("Показы: {}   Клики: {}   CTR: {}".format(
        R.fmt_int(T["imp"]["now"]), R.fmt_int(T["clicks"]["now"]), R.fmt_pct(T["ctr"]["now"])))
    if res["has_conv"]:
        L.append("Заявки: {}   Цена заявки: {}".format(
            R.fmt_int(T["conv"]["now"]), R.fmt_money(T["cpa"]["now"])))
        goals = res.get("goals") or []
        gt = res.get("goal_totals") or {}
        named = [(g["name"], gt.get(g["id"], 0)) for g in goals if gt.get(g["id"], 0) > 0]
        if named:
            L.append("")
            L.append("По целям:")
            for name, v in named:
                L.append("  {} — {}".format(name, R.fmt_int(v)))
    L.append("")
    L.append("Сравнение с предыдущим периодом ({} — {}):".format(
        _ru_date(res["prev_from"]), _ru_date(res["prev_to"])))
    for h in res["highlights"]:
        L.append("  " + h["text"])
    top = [c for c in res["campaigns"] if c["now"]["cost"] > 0][:5]
    if top:
        L.append("")
        L.append("Крупнейшие кампании периода:")
        for c in top:
            line = "  {} — {}".format(c["name"], R.fmt_money(c["now"]["cost"]))
            if res["has_conv"]:
                line += ", заявок {}".format(R.fmt_int(c["now"]["conv"]))
                if c["now"]["cpa"]:
                    line += " по {}".format(R.fmt_money(c["now"]["cpa"]))
            L.append(line)
    return "\n".join(L)
