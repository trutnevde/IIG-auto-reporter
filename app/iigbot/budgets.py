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
def working_pool(db, logins=None):
    """Логины, которыми реально занимаются: есть владелец ИЛИ привязка к чату.
    logins — ограничить пул этим набором (ручной сбор «только мои клиенты»)."""
    owned = {c["login"] for c in db.list_clients("all")
             if ("owner" in c.keys()) and c["owner"] is not None}
    bound = {b["login"] for b in db.list_bindings("all")}
    pool = owned | bound
    if logins is not None:
        pool &= set(logins)
    return sorted(pool)


# ---------- основной сбор ----------
def collect(db, token, on_progress=None, _post=None, _sleep=None, logins=None):
    """Обновляет таблицу budgets по рабочему пулу. logins — только эти клиенты. Возвращает сводку."""
    pool = working_pool(db, logins=logins)
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
def _alert_line(r):
    nm = r["name"] or r["login"]
    if r["days_left"] is not None:
        return "• {} ({}): осталось ~{} дн. — баланс {:,.0f} {}, темп {:,.0f}/день".format(
            nm, r["login"], r["days_left"], r["balance"] or 0, r["currency"], r["rate"] or 0).replace(",", " ")
    return "• {} ({}): {} кампаний остановлено по оплате".format(nm, r["login"], r["camps_pay_stopped"])


def _priv_index(db):
    """Индексы приватных чатов: по chat_id, по @username и по title (для матча без @username)."""
    by_id, by_un, by_title = {}, {}, {}
    for c in db.list_chats():
        if (c["type"] or "") != "private":
            continue
        by_id[c["chat_id"]] = c
        if c["username"]:
            by_un[str(c["username"]).lower()] = c
        if c["title"]:
            by_title[str(c["title"]).lower()] = c
    return by_id, by_un, by_title


def _resolve_chat(u, by_id, by_un, by_title):
    """Чат пользователя для алертов: сперва привязанный alert_chat_id (deep-link, надёжно),
    затем @username — по полю username ИЛИ по title (у кого нет публичного @username)."""
    if not u:
        return None
    cid = u["alert_chat_id"] if "alert_chat_id" in u.keys() else None
    if cid and cid in by_id:
        return by_id[cid]
    un = ((u["alert_username"] if "alert_username" in u.keys() else None) or "").strip().lstrip("@").lower()
    if un:
        return by_un.get(un) or by_title.get(un)
    return None


def send_alerts(db, tg, logins=None):
    """Критичные (<3 дней) и остановленные по оплате — в личку ВЛАДЕЛЬЦУ клиента. Чат владельца
    ищем: привязанный alert_chat_id (надёжно, deep-link) → @username (username или title). Ничьи /
    без чата → общий получатель (kv budget_alert_username или первый админ с привязкой). Группируем
    по чату, не чаще раза в сутки на клиента. logins — ограничить набором (ручной сбор своих)."""
    from .settings import log_error
    lset = set(logins) if logins is not None else None
    rows = [r for r in db.list_budgets()
            if (lset is None or r["login"] in lset)
            and (r["status"] == "critical" or (r["status"] == "warning" and (r["camps_pay_stopped"] or 0) > 0))]
    if not rows:
        return {"sent": 0, "reason": "нет критичных"}

    users = {u["id"]: u for u in db.list_users()}
    owner_of = {c["login"]: (c["owner"] if "owner" in c.keys() else None)
                for c in db.list_clients("all")}
    by_id, by_un, by_title = _priv_index(db)
    # общий/фолбэк получатель — по kv budget_alert_username, иначе первый админ с привязанной личкой
    global_un = (db.get_kv("budget_alert_username") or "iig_dtrutnev").strip().lstrip("@").lower()
    global_chat = by_un.get(global_un) or by_title.get(global_un)
    if not global_chat:
        for u in users.values():
            if u["role"] == "admin":
                gc = _resolve_chat(u, by_id, by_un, by_title)
                if gc:
                    global_chat = gc
                    break

    today = dt.date.today().isoformat()
    by_chat = {}   # chat_id -> (chat, [rows])
    unroutable = []
    for r in rows:
        chat = _resolve_chat(users.get(owner_of.get(r["login"])), by_id, by_un, by_title) or global_chat
        if not chat:
            unroutable.append(r["login"])
            continue
        by_chat.setdefault(chat["chat_id"], (chat, []))[1].append(r)

    total_sent = 0
    for cid, (chat, rs) in by_chat.items():
        fresh = [r for r in rs if db.get_kv("budget_alerted_" + r["login"]) != today]
        if not fresh:
            continue
        lines = ["⚠️ БЮДЖЕТ НА ИСХОДЕ"] + [_alert_line(r) for r in fresh]
        lines += ["", "Проверь и пополни: вкладка «Бюджеты» в кабинете."]
        try:
            tg.send_message(cid, "\n".join(lines))
            for r in fresh:
                db.set_kv("budget_alerted_" + r["login"], today)
            total_sent += len(fresh)
        except Exception as e:  # noqa: BLE001
            log_error("budgets.alert", "не отправить в чат {}: {}".format(cid, e))
    # антиспам: про «некому слать» — в журнал не чаще раза в день
    if unroutable and db.get_kv("budget_alert_nolog") != today:
        db.set_kv("budget_alert_nolog", today)
        log_error("budgets.alert",
                  "{} критичных клиентов некому слать — получатель не привязал личку "
                  "(Настройки → «Алерты по бюджету» → «Привязать Telegram»): {}".format(
                      len(unroutable), ", ".join(unroutable[:10])))
    return {"sent": total_sent, "recipients": len(by_chat), "unroutable": len(unroutable)}


def collect_and_alert(db, token, tg=None, on_progress=None, logins=None):
    """Полный проход: сбор + алерты. logins — только эти клиенты (ручной сбор своих)."""
    res = collect(db, token, on_progress=on_progress, logins=logins)
    if tg is not None:
        try:
            res["alerts"] = send_alerts(db, tg, logins=logins)
        except Exception as e:  # noqa: BLE001
            from .settings import log_error
            log_error("budgets.alert", e)
    return res
