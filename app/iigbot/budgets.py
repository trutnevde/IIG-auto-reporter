# -*- coding: utf-8 -*-
"""Бюджеты клиентов: остаток общего счёта, темп трат и «на сколько дней хватит».

Собирает по рабочему пулу (клиенты с владельцем или привязкой), фильтрует активных
(тратили последние 3 недели), считает:
  • баланс общего счёта — Live v4 AccountManagement (тот же OAuth-токен);
  • темп трат — Reports API, расход по дням за 21 день (последние 7 — темп);
  • статусы кампаний — v5 campaigns (State/StatusPayment/DailyBudget).
Результат кладёт в таблицу budgets; критичные (< 3 дней) шлёт в личку Telegram
(username в kv budget_alert_username, по умолчанию iig_dtrutnev) не чаще раза в сутки.

Только чтение из Директа, изменяющих вызовов нет.
"""
import json
import time
import datetime as dt

import requests

from . import report as R

V4_URL = "https://api.direct.yandex.ru/live/v4/json/"
V5_API = "https://api.direct.yandex.com/json/v5/"

CRIT_DAYS = 3      # красная зона: меньше трёх дней
WARN_DAYS = 7      # жёлтая зона


# ---------- Директ: баланс общего счёта (Live v4) ----------
def get_balances(token, logins, _post=None):
    """{login: {'amount': float, 'currency': str}} по общим счетам. Логины без общего
    счёта в ответ не попадают. Батчами по 50 (лимит SelectionCriteria)."""
    post = _post or requests.post
    out = {}
    logins = [str(l) for l in logins if l]
    for i in range(0, len(logins), 50):
        batch = logins[i:i + 50]
        body = {"method": "AccountManagement", "token": token, "locale": "ru",
                "param": {"Action": "Get", "SelectionCriteria": {"Logins": batch}}}
        r = post(V4_URL, json=body, timeout=60)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError("Директ v4 вернул не-JSON (HTTP {})".format(getattr(r, "status_code", "?")))
        if isinstance(data, dict) and data.get("error_code"):
            raise RuntimeError("Директ v4: {} — {}".format(
                data.get("error_str"), data.get("error_detail") or ""))
        for acc in ((data.get("data") or {}).get("Accounts") or []):
            lg = str(acc.get("Login") or "").lower()
            try:
                amount = float(acc.get("Amount"))
            except (TypeError, ValueError):
                continue
            out[lg] = {"amount": amount, "currency": str(acc.get("Currency") or "RUB")}
    return out


# ---------- Директ: статусы кампаний (v5) ----------
def get_campaign_states(token, login, _post=None):
    """Сводка по кампаниям клиента: {'total','on','pay_stopped','daily_budget'}.
    on — State=ON и оплата разрешена; pay_stopped — остановлены по деньгам (StatusPayment=DISALLOWED
    среди неархивных); daily_budget — суммарный дневной бюджет включённых (руб/день, 0 если не задан)."""
    post = _post or requests.post
    headers = {
        "Authorization": "Bearer {}".format(token),
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {"method": "get", "params": {
        "SelectionCriteria": {},
        "FieldNames": ["Id", "Name", "State", "Status", "StatusPayment", "DailyBudget"],
    }}
    r = post(V5_API + "campaigns", json=body, headers=headers, timeout=60)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("Директ вернул не-JSON (HTTP {})".format(getattr(r, "status_code", "?")))
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise RuntimeError("Директ API: {} — {}".format(err.get("error_string"), err.get("error_detail")))
    total = on = pay_stopped = 0
    daily = 0.0
    for c in (data.get("result") or {}).get("Campaigns", []):
        state = c.get("State")
        if state in ("ARCHIVED", "CONVERTED"):
            continue
        total += 1
        if c.get("StatusPayment") == "DISALLOWED":
            pay_stopped += 1
        if state == "ON" and c.get("StatusPayment") != "DISALLOWED":
            on += 1
            db_ = c.get("DailyBudget") or {}
            try:
                daily += float(db_.get("Amount") or 0) / 1e6   # микро-единицы -> валюта
            except (TypeError, ValueError):
                pass
    return {"total": total, "on": on, "pay_stopped": pay_stopped, "daily_budget": round(daily, 2)}


# ---------- Reports: расход по дням за 21 день ----------
def get_daily_costs(token, login, _post=None, _sleep=None):
    """(cost7, cost21) — расход за последние 7 и 21 день (с НДС, руб)."""
    today = dt.date.today()
    d_from = (today - dt.timedelta(days=21)).isoformat()
    d_to = (today - dt.timedelta(days=1)).isoformat()   # по вчера: сегодня ещё неполное
    rows = R.fetch_report(token, login, d_from, d_to, ["Date", "Cost"],
                          report_type="ACCOUNT_PERFORMANCE_REPORT", _post=_post, _sleep=_sleep)
    week_edge = (today - dt.timedelta(days=7)).isoformat()
    cost7 = cost21 = 0.0
    for r in rows:
        c = R.parse_num(r.get("Cost"))
        cost21 += c
        if str(r.get("Date") or "") >= week_edge:
            cost7 += c
    return round(cost7, 2), round(cost21, 2)


# ---------- рабочий пул ----------
def working_pool(db):
    """Логины, которыми реально занимаются: есть владелец ИЛИ привязка к чату."""
    owned = {c["login"] for c in db.list_clients("all")
             if ("owner" in c.keys()) and c["owner"] is not None}
    bound = {b["login"] for b in db.list_bindings("all")}
    return sorted(owned | bound)


# ---------- основной сбор ----------
def collect(db, token, on_progress=None, _post=None, _sleep=None):
    """Обновляет таблицу budgets по рабочему пулу. Возвращает сводку."""
    pool = working_pool(db)
    try:
        balances = get_balances(token, pool, _post=_post)
    except Exception as e:  # noqa: BLE001 — нет доступа к v4: работаем без баланса
        balances = {}
        from .settings import log_error
        log_error("budgets.balance", e)
    names = {c["login"]: (c["name"] or c["login"]) for c in db.list_clients("all")}
    res = {"clients": len(pool), "active": 0, "critical": 0, "warning": 0, "errors": 0}
    for i, login in enumerate(pool, 1):
        row = {"login": login, "name": names.get(login) or login,
               "balance": None, "currency": "RUB", "cost7": 0.0, "cost21": 0.0,
               "rate": 0.0, "days_left": None, "camps_total": 0, "camps_on": 0,
               "camps_pay_stopped": 0, "daily_budget": 0.0, "status": "inactive", "note": ""}
        try:
            cost7, cost21 = get_daily_costs(token, login, _post=_post, _sleep=_sleep)
            row["cost7"], row["cost21"] = cost7, cost21
            b = balances.get(login.lower())
            if b:
                row["balance"], row["currency"] = b["amount"], b["currency"]
            if cost21 <= 0:
                row["status"] = "inactive"
                row["note"] = "не тратил последние 3 недели"
            else:
                res["active"] += 1
                camps = get_campaign_states(token, login, _post=_post)
                row.update({"camps_total": camps["total"], "camps_on": camps["on"],
                            "camps_pay_stopped": camps["pay_stopped"],
                            "daily_budget": camps["daily_budget"]})
                rate = round(cost7 / 7.0, 2)
                row["rate"] = rate
                if row["balance"] is not None and rate > 0:
                    row["days_left"] = round(row["balance"] / rate, 1)
                    if row["days_left"] < CRIT_DAYS:
                        row["status"] = "critical"; res["critical"] += 1
                    elif row["days_left"] < WARN_DAYS:
                        row["status"] = "warning"; res["warning"] += 1
                    else:
                        row["status"] = "ok"
                elif row["balance"] is None:
                    row["status"] = "ok" if camps["pay_stopped"] == 0 else "warning"
                    row["note"] = "баланс недоступен (нет общего счёта или прав)"
                    if camps["pay_stopped"]:
                        row["note"] += "; есть кампании, остановленные по оплате"
                        res["warning"] += 1
                else:   # баланс есть, но не тратит по темпу (rate 0 при cost21>0 — редкость)
                    row["status"] = "ok"
        except Exception as e:  # noqa: BLE001
            row["status"] = "error"
            row["note"] = str(e)[:300]
            res["errors"] += 1
        db.save_budget(row)
        if on_progress:
            on_progress(i, len(pool), {"login": login, "status": row["status"]})
    db.set_kv("budgets_updated", dt.datetime.now().isoformat(timespec="seconds"))
    return res


# ---------- алерты в личку ----------
def send_alerts(db, tg):
    """Критичные (< 3 дней) и остановленные по оплате — одним сообщением в личку
    (kv budget_alert_username, по умолчанию iig_dtrutnev). Не чаще раза в сутки на клиента."""
    username = (db.get_kv("budget_alert_username") or "iig_dtrutnev").lstrip("@").lower()
    chat = None
    for c in db.list_chats():
        if (c["type"] or "") == "private" and (c["username"] or "").lower() == username:
            chat = c
            break
    rows = [r for r in db.list_budgets()
            if r["status"] == "critical"
            or (r["status"] in ("warning",) and (r["camps_pay_stopped"] or 0) > 0)]
    if not rows:
        return {"sent": 0, "reason": "нет критичных"}
    if not chat:
        from .settings import log_error
        log_error("budgets.alert", "личка @{} не найдена — пусть напишет боту /start".format(username))
        return {"sent": 0, "reason": "нет лички @" + username}
    today = dt.date.today().isoformat()
    fresh = [r for r in rows if db.get_kv("budget_alerted_" + r["login"]) != today]
    if not fresh:
        return {"sent": 0, "reason": "уже слали сегодня"}
    lines = ["⚠️ БЮДЖЕТ НА ИСХОДЕ"]
    for r in fresh:
        nm = r["name"] or r["login"]
        if r["days_left"] is not None:
            lines.append("• {} ({}): осталось ~{} дн. — баланс {:,.0f} {}, темп {:,.0f}/день".format(
                nm, r["login"], r["days_left"], r["balance"], r["currency"], r["rate"]).replace(",", " "))
        else:
            lines.append("• {} ({}): {} кампаний остановлено по оплате".format(
                nm, r["login"], r["camps_pay_stopped"]))
    lines.append("")
    lines.append("Проверь и пополни: вкладка «Бюджеты» в кабинете.")
    tg.send_message(chat["chat_id"], "\n".join(lines))
    for r in fresh:
        db.set_kv("budget_alerted_" + r["login"], today)
    return {"sent": len(fresh)}


def collect_and_alert(db, token, tg=None, on_progress=None):
    """Полный проход для планировщика: сбор + алерты (если есть кому слать)."""
    res = collect(db, token, on_progress=on_progress)
    if tg is not None:
        try:
            res["alerts"] = send_alerts(db, tg)
        except Exception as e:  # noqa: BLE001
            from .settings import log_error
            log_error("budgets.alert", e)
    return res
