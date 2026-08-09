# -*- coding: utf-8 -*-
"""Backend для десктоп-приложения (pywebview js_api).

Каждый метод возвращает {"ok": True, "data": ...} или {"ok": False, "error": "..."},
чтобы интерфейс показывал понятные сообщения, а не падал. Сетевые/тяжёлые операции
ловят исключения здесь.
"""
import json
import re
import difflib
import functools

from . import yandex, report, listener
from .storage import Storage
from .telegram_api import Telegram, TelegramError
from .settings import (
    load_secrets, load_app_config, load_report_config, default_attribution,
    save_app_config, save_report_config, save_secrets as _save_secrets,
    log_error,
)
from .import_config import normalize_goals



def _unwrap(bound):
    """Функция из-под декоратора @safe: в фоне нам нужен результат, а не {ok,data}."""
    fn = getattr(bound, "__func__", bound)
    return getattr(fn, "__wrapped__", fn)


def safe(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return {"ok": True, "data": fn(self, *args, **kwargs)}
        except Exception as e:  # noqa: BLE001
            log_error("api." + fn.__name__, e)   # в iig_errors.log рядом с базой (виден по SFTP)
            try:                       # в exe stdout перенаправлен в iig.log — ошибки будут видны
                print("[api] {}: {}".format(fn.__name__, e))
            except Exception:          # noqa: BLE001
                pass
            return {"ok": False, "error": str(e)}
    return wrapper


# «Ключевые» цели — те, что по умолчанию активны для отчётов (покупки/заявки/звонки и т.п.).
KEY_GOAL_TYPES = {"e_purchase", "form", "phone", "messenger", "action", "contact_data_sent"}
KEY_GOAL_WORDS = ("покупк", "заявк", "звон", "форм", "заказ", "оплат", "корзин", "купить",
                  "лид", "обратн", "заполн", "контакт", "checkout", "purchase", "order", "lead", "call")


def _is_key_goal(name, gtype):
    if (gtype or "") in KEY_GOAL_TYPES:
        return True
    n = (name or "").lower()
    return any(w in n for w in KEY_GOAL_WORDS)


# Фоновый сбор бюджетов — глобальный лок/прогресс (сбор агентский, Api — на пользователя).
_BUDGET_RUN = {"running": False, "done": 0, "total": 0, "error": None, "summary": None}


class Api:
    def __init__(self, user=None):
        self.cfg = load_app_config()
        self.db = Storage(self.cfg["db_path"])
        self.user = user               # dict текущего пользователя (веб) или None (десктоп/легаси = «всё»)
        self._tg = None
        self._bot_username = None
        self._mk_counters = None       # кэш списка счётчиков Метрики

    def _owner(self):
        """Скоуп ВИДИМОСТИ данных (клиенты/чаты/отчёты): 'all' — только десктоп/легаси и
        НАБЛЮДАТЕЛЬ (работодатель видит всё). Админ — тоже специалист со своим стеком и чужих
        клиентов НЕ видит; админские функции (пользователи, раздача пула, журнал, синк,
        контроль) от этого скоупа не зависят."""
        u = self.user
        if not u or u.get("role") == "observer":
            return "all"
        return u.get("id")

    def _is_admin_scope(self):
        return self._owner() == "all"

    def _is_admin(self):
        return (not self.user) or self.user.get("role") == "admin"

    def _is_observer(self):
        return bool(self.user) and self.user.get("role") == "observer"

    def _require_admin(self):
        if not self._is_admin():
            raise RuntimeError("Доступно только администратору")

    def _require_supervisor(self):
        """Контроль и сообщения — наблюдатель или админ."""
        if not (self._is_admin() or self._is_observer()):
            raise RuntimeError("Доступно только наблюдателю или администратору")

    def _require_write(self):
        """Наблюдатель работает в режиме просмотра — правки/отправки запрещены."""
        if self._is_observer():
            raise RuntimeError("Наблюдатель — режим просмотра, изменения недоступны")

    def _owned_set(self):
        """Множество логинов клиентов пользователя; None = видит всё (админ/десктоп)."""
        if self._is_admin_scope():
            return None
        return set(self.db.owned_logins(self.user["id"]))

    def _require_owned(self, login):
        s = self._owned_set()
        if s is not None and login not in s:
            raise RuntimeError("Этот клиент не в вашем доступе")

    def _scope_logins(self, logins):
        """Рассылка (кнопка) — по СВОИМ клиентам текущего пользователя: и специалист, и админ
        ведут свой стек и шлют только своих. «Отправить всем разом» делает недельный cron
        (agency-wide), а не кнопка. Десктоп/легаси без пользователя — все. Сторонние (копипаст)
        из Telegram-рассылки/пробы исключаются — они доставляются вручную."""
        external = self.db.external_logins()
        if not self.user:
            return [l for l in logins if l not in external] if logins else logins
        own = set(self.db.owned_logins(self.user["id"])) - external
        if logins is None:
            return sorted(own)
        return [l for l in logins if l in own]

    def _visible_chats(self):
        """Чаты, видимые пользователю: непривязанные + привязанные к его клиентам. Админ — все."""
        chats = self.db.list_chats()
        s = self._owned_set()
        if s is None:
            return chats
        owner_of = {b["chat_id"]: b["login"] for b in self.db.list_bindings("all")}
        return [c for c in chats
                if owner_of.get(c["chat_id"]) is None or owner_of.get(c["chat_id"]) in s]

    def _require_chat_visible(self, chat_id):
        """Чат либо свободен, либо привязан к клиенту пользователя (иначе — чужой)."""
        s = self._owned_set()
        if s is None:
            return
        b = self.db.get_binding(chat_id)
        if b and b["login"] not in s:
            raise RuntimeError("Этот чат не в вашем доступе")

    def _client_owner(self, login):
        c = self.db.get_client(login)
        return (c["owner"] if (c and "owner" in c.keys()) else None)

    def _require_bindable(self, login):
        """Специалист может привязать чат только к СВОЕМУ или СВОБОДНОМУ клиенту (чужого — нельзя)."""
        if self._is_admin_scope():
            return
        owner = self._client_owner(login)
        if owner is not None and owner != self.user["id"]:
            raise RuntimeError("Клиент закреплён за другим специалистом")

    def _claim_if_pool(self, login):
        """Правило «привязал → взял»: если клиент свободен (ничей), закрепляем за тем, кто привязал.
        Работает и у специалиста, и у админа (админ тоже ведёт свой стек). Раздачу не отменяет —
        владельца можно переназначить (assign_client)."""
        if self.user and self._client_owner(login) is None:
            self.db.set_client_owner(login, self.user["id"])

    def _bindable_clients(self):
        """Клиенты, доступные специалисту для привязки: свои + свободные (пул). Админ — все."""
        if self._is_admin_scope():
            return self.db.list_clients("all")
        return list(self.db.list_clients(self.user["id"])) + list(self.db.list_clients(None))

    # ---------- helpers ----------
    def _metrika_counters(self):
        if self._mk_counters is None:
            from . import metrika
            self._mk_counters = metrika.get_counters(load_secrets()["yandex_oauth_token"])
        return self._mk_counters

    @staticmethod
    def _client_domains(name):
        toks = re.split(r"[\s,/]+", (name or "").lower())
        return [t.strip(".") for t in toks if "." in t and len(t) > 3]

    @staticmethod
    def _dom_match(site, dom):
        return bool(site) and (site == dom or site.endswith("." + dom) or dom.endswith("." + site))

    def _tg_client(self):
        if self._tg is None:
            token = (load_secrets().get("telegram_bot_token") or "").strip()
            if not token or "ВСТАВЬ" in token:
                raise RuntimeError("Не задан telegram_bot_token в secrets.json")
            self._tg = Telegram(token, timeout=20)
        return self._tg

    def _bot_name(self):
        if self._bot_username is None:
            self._bot_username = self._tg_client().get_me().get("username")
        return self._bot_username

    def _report_ctx(self):
        rep = load_report_config()
        intro = rep.get("intro") or "Отчёт за прошлую неделю."
        note = rep.get("specialist_note") or ""   # приписка опциональна (пусто = не добавлять)
        attr = rep.get("attribution_model") or default_attribution()
        return intro, note, attr

    def _notify(self, to_user, kind, title, text=None, link=None, dedup=False):
        """Создать уведомление в колокольчик (to_user=None — всем)."""
        try:
            return self.db.add_notification(to_user, kind, title, text, link, dedup_key=dedup)
        except Exception:  # noqa: BLE001 — уведомление не должно ломать основную операцию
            return None

    def _audit(self, action, target=None, detail=None):
        """Записать действие в аудит-лог (кто/что/когда) — для «Журнала действий» работодателя."""
        u = self.user or {}
        self.db.log_action(u.get("id"), (u.get("name") or u.get("email") or "система"),
                           action, target, detail)

    def _chat_title(self, chat_id):
        c = self.db.get_chat(chat_id)
        return c["title"] if c else str(chat_id)

    # ---------- dashboard ----------
    @safe
    def dashboard(self):
        """Сводка Обзора. Десяток обращений к базе на каждое открытие — а открывают
        его по несколько раз подряд. Держим готовый ответ минуту: за это время
        ни отчёты, ни бюджеты не меняются."""
        import time as _t
        uid = (self.user or {}).get("id") or 0
        key = "dash:{}".format(uid)
        cached = getattr(self, "_dash_cache", None)
        if cached and cached.get("key") == key and (_t.time() - cached["at"]) < 60:
            return cached["data"]
        data = self._dashboard_build()
        self._dash_cache = {"key": key, "at": _t.time(), "data": data}
        return data

    def _dashboard_build(self):
        chats = [c for c in self._visible_chats() if c["status"] == "active"]
        clients = self.db.list_clients(self._owner())
        bindings = self.db.list_bindings(self._owner())
        bound_chat_ids = {b["chat_id"] for b in bindings}
        bound_logins = {b["login"] for b in bindings}
        unbound_chats = [c for c in chats if c["chat_id"] not in bound_chat_ids]
        clients_no_chat = [c for c in clients if c["login"] not in bound_logins]
        owned = self._owned_set()
        if owned is None:
            rows = self.db.conn.execute(
                "SELECT status, COUNT(*) n FROM send_log GROUP BY status").fetchall()
        elif owned:
            ph = ",".join("?" * len(owned))
            rows = self.db.conn.execute(
                "SELECT status, COUNT(*) n FROM send_log WHERE login IN (%s) "
                "GROUP BY status" % ph, tuple(owned)).fetchall()
        else:
            rows = []
        by_status = {r["status"]: r["n"] for r in rows}

        # ---- ролевые сводки для Обзора (скоуп по видимости: наблюдатель — всё, иначе своё) ----
        import datetime as _dt
        external_logins = {c["login"] for c in clients
                           if ("delivery" in c.keys()) and c["delivery"] == "external"}
        obligations = bound_logins | external_logins   # недельные обязательства в моём скоупе
        today = _dt.date.today()
        mon = today - _dt.timedelta(days=today.weekday())

        def _iso(d):
            return d.isoformat() + "T00:00:00"
        sent_this = self.db.sent_logins_between(_iso(mon), _iso(today + _dt.timedelta(days=1)))
        skip_this = self.db.status_logins_between("skipped", _iso(mon), _iso(today + _dt.timedelta(days=1)))
        excused = set(self.db.excused_logins(mon.isoformat()).keys())
        delivered = obligations & sent_this
        auto_skip = (obligations & skip_this) - delivered      # нет открута — авто-уважительно
        closed = (obligations & excused) - delivered - auto_skip   # долг закрыт причиной
        covered = delivered | auto_skip | closed
        debt = obligations - covered
        week = {"obligations": len(obligations), "sent": len(delivered),
                "no_spend": len(auto_skip), "excused": len(closed),
                "debt": len(debt), "covered": len(covered),
                "coverage": (round(100 * len(covered) / len(obligations)) if obligations else None)}
        # сторонние, по которым на этой неделе ещё не собирали отчёт (нет ни sent, ни skipped)
        ext_pending = sorted(external_logins - sent_this - skip_this)

        # динамика по дням текущей недели — для графика на Обзоре
        week_days = []
        for i in range(7):
            day = mon + _dt.timedelta(days=i)
            if day > today:
                week_days.append({"day": day.isoformat(), "sent": None})   # ещё не наступил
                continue
            got = self.db.sent_logins_between(_iso(day), _iso(day + _dt.timedelta(days=1)))
            week_days.append({"day": day.isoformat(), "sent": len(obligations & got)})

        # последние рассылки: группируем строки лога по минуте отправки
        if owned is None:
            hrows = self.db.conn.execute(
                "SELECT sent_at,status FROM send_log ORDER BY id DESC LIMIT 400").fetchall()
        elif owned:
            ph = ",".join("?" * len(owned))
            hrows = self.db.conn.execute(
                "SELECT sent_at,status FROM send_log WHERE login IN (%s) "
                "ORDER BY id DESC LIMIT 400" % ph, tuple(owned)).fetchall()
        else:
            hrows = []
        runs, order = {}, []
        for r in hrows:
            key = (r["sent_at"] or "")[:16]          # до минуты
            if not key:
                continue
            if key not in runs:
                runs[key] = {"at": r["sent_at"], "n": 0, "sent": 0, "err": 0, "skip": 0}
                order.append(key)
            g = runs[key]; g["n"] += 1
            if r["status"] == "sent": g["sent"] += 1
            elif r["status"] == "error": g["err"] += 1
            elif r["status"] == "skipped": g["skip"] += 1
        recent = [runs[k] for k in order[:3]]

        # бюджеты в моём скоупе
        visible = {c["login"] for c in clients}
        brows = [r for r in self.db.list_budgets() if r["login"] in visible]
        bcrit = [r for r in brows if r["status"] == "critical"]
        bwarn = [r for r in brows if r["status"] == "warning"]
        btop = sorted(bcrit, key=lambda r: (r["days_left"] if r["days_left"] is not None else 1e9))[:5]
        names = {c["login"]: (c["name"] or c["login"]) for c in clients}
        budgets = {
            "critical": len(bcrit), "warning": len(bwarn),
            "updated": self.db.get_kv("budgets_updated"),
            "top": [{"login": r["login"], "name": r["name"] or names.get(r["login"], r["login"]),
                     "days_left": r["days_left"], "balance": r["balance"], "currency": r["currency"]}
                    for r in btop],
        }

        def _epoch_iso(key):
            v = self.db.get_kv(key)
            try:
                return _dt.datetime.fromtimestamp(float(v)).isoformat(timespec="seconds") if v else None
            except (TypeError, ValueError):
                return v
        health = {"autosync": _epoch_iso("autosync_last"), "budgets": self.db.get_kv("budgets_updated")}

        # диалоги: сколько клиентов ждут ответа (для карточки на Обзоре)
        try:
            _wait, _silent, _hidden = self._chat_dialogs()
            dialogs = {"waiting": len(_wait),
                       "overdue": sum(1 for w in _wait if (w.get("wait_h") or 0) >= self.WAIT_CRIT_HOURS),
                       "silent": len(_silent),
                       "top": [{"name": w.get("client_name") or w.get("chat_title"),
                                "wait_h": w.get("wait_h")} for w in _wait[:3]]}
        except Exception:  # noqa: BLE001
            dialogs = {"waiting": 0, "overdue": 0, "silent": 0, "top": []}

        return {
            "role": (self.user.get("role") if self.user else "admin"),
            "clients": len(clients),
            "chats": len(chats),
            "bound": len(bound_chat_ids),
            "unbound_chats": len(unbound_chats),
            "clients_no_chat": len(clients_no_chat),
            "errors": by_status.get("error", 0),
            "week": week,
            "dialogs": dialogs,
            "external_pending": len(ext_pending),
            "week_days": week_days,
            "recent": recent,
            "budgets": budgets,
            "health": health,
            "alerts": {
                "unbound_chats": len(unbound_chats),
                "clients_no_chat": len(clients_no_chat),
            },
        }

    # ---------- clients ----------
    @safe
    def clients(self):
        binds = {b["login"]: b for b in self.db.list_bindings(self._owner())}
        out = []
        sent = self.db.last_send_map()
        for c in self.db.list_clients(self._owner()):
            b = binds.get(c["login"])
            try:
                goals = json.loads(c["goals"] or "[]")
            except (ValueError, TypeError):
                goals = []
            out.append({
                "login": c["login"], "name": c["name"], "source": c["source"],
                "attribution": c["attribution"] or "",
                "goals": goals,
                "delivery": (c["delivery"] if "delivery" in c.keys() else None) or "telegram",
                "added_at": (c["added_at"] if "added_at" in c.keys() else None) or c["updated_at"],
                "chat_id": b["chat_id"] if b else None,
                "chat_title": self._chat_title(b["chat_id"]) if b else None,
                # когда клиента последний раз сдавали: у сторонних по этому видно, кого забыли
                "last_sent": sent.get(c["login"]),
            })
        return out

    @safe
    def client(self, login):
        self._require_owned(login)
        c = self.db.get_client(login)
        if not c:
            raise RuntimeError("Клиент не найден")
        try:
            goals = json.loads(c["goals"] or "[]")
        except (ValueError, TypeError):
            goals = []
        binds = self.db.bindings_for_login(login)
        return {
            "login": c["login"], "name": c["name"], "source": c["source"],
            # пусто = «как в Настройках»; подставлять дефолт нельзя, иначе он запишется
            # клиенту явно и перестанет следовать за общей настройкой агентства
            "attribution": c["attribution"] or "", "attribution_default": default_attribution(),
            "goals": goals,
            "delivery": (c["delivery"] if "delivery" in c.keys() else None) or "telegram",
            "chats": [{"chat_id": b["chat_id"], "title": self._chat_title(b["chat_id"])} for b in binds],
        }

    @safe
    def save_client(self, login, name=None, goals=None, attribution=None, delivery=None):
        self._require_write()
        self._require_owned(login)
        self.db.upsert_client(
            login=login, name=name,
            goals=normalize_goals(goals) if goals is not None else None,
            attribution=attribution,
        )
        if delivery is not None:
            self.db.set_client_delivery(login, delivery)
        if goals is not None:
            self._cloud_push_safe()
        return True

    @safe
    def set_delivery(self, login, mode):
        """Быстрый тумблер способа доставки клиента (из вкладки «Сторонние»):
        'external' — копипаст (сторонний мессенджер, вне бот-рассылки) или 'telegram'."""
        self._require_write()
        self._require_owned(login)
        self.db.set_client_delivery(login, "external" if mode == "external" else "telegram")
        return {"login": login, "delivery": "external" if mode == "external" else "telegram"}

    def _metrika_goals_for(self, login):
        """Ядро: находит доступные счётчики клиента (кампании ∪ домен) и собирает все их цели
        с авто-пометкой active (ключевые → True). Бросает исключения (оборачивается в @safe выше)."""
        from . import metrika
        token = load_secrets()["yandex_oauth_token"]
        c = self.db.get_client(login)
        if not c:
            raise RuntimeError("Клиент не найден")
        try:
            camp_ids = list(yandex.get_campaign_counters(token, login))
        except Exception:  # noqa: BLE001
            camp_ids = []
        accessible = self._metrika_counters()
        acc_ids = {x["id"] for x in accessible}
        domains = self._client_domains(c["name"])
        dom_ids = [x["id"] for x in accessible if any(self._dom_match(x["site"], d) for d in domains)]
        candidates = []
        for cid in camp_ids + dom_ids:                 # кампании в приоритете, затем домен
            if cid in acc_ids and cid not in candidates:
                candidates.append(cid)
        if not candidates:                             # список доступных мог быть неполон — пробуем напрямую
            for cid in camp_ids:
                if cid not in candidates:
                    candidates.append(cid)
        goals, used, seen = [], [], set()
        for cid in candidates:
            try:
                gs = metrika.get_counter_goals(token, cid)
            except Exception:  # noqa: BLE001 — 403/нет доступа: пропускаем счётчик
                continue
            used.append(cid)
            for g in gs:
                if g["id"] not in seen:
                    seen.add(g["id"])
                    g["active"] = _is_key_goal(g["name"], g.get("type"))
                    goals.append(g)
        note = "" if goals else "Не нашёл доступного счётчика Метрики для этого клиента (нет доступа к его счётчику)."
        return {"goals": goals, "counters": used, "note": note,
                "from_campaigns": camp_ids, "from_domain": dom_ids}

    @safe
    def metrika_goals(self, login):
        """Для карточки клиента: цели из Метрики с пресетом active (ключевые отмечены). Не сохраняет."""
        self._require_owned(login)
        return self._metrika_goals_for(login)

    @safe
    def client_goals_pull(self, login):
        """Подтянуть цели из Метрики для ОДНОГО клиента и СОХРАНИТЬ (кнопка в конструкторе, когда
        у клиента нет целей). Ключевые помечаются active, ручные галки сохраняются. Возвращает цели
        для конструктора: [{'id','name','active'}]."""
        self._require_write()
        self._require_owned(login)
        found = self._metrika_goals_for(login)
        goals = found["goals"]
        if not goals:
            return {"goals": [], "note": found.get("note") or "Цели не найдены"}
        cur = self.db.get_client(login)
        prev = {}
        try:
            for g in json.loads((cur and cur["goals"]) or "[]"):
                if isinstance(g, dict):
                    prev[str(g.get("id"))] = (g.get("active") is not False)
        except Exception:  # noqa: BLE001
            pass
        merged = [{"id": g["id"], "name": g["name"], "type": g.get("type", ""),
                   "active": prev.get(g["id"], g["active"])} for g in goals]
        self.db.upsert_client(login=login, goals=normalize_goals(merged))
        self._cloud_push_safe()
        out = [{"id": str(g["id"]), "name": g["name"], "active": (g["active"] is not False)} for g in merged]
        return {"goals": out, "counters": found.get("counters", []),
                "note": "Подтянуто целей: {} (ключевых вкл.: {})".format(
                    len(out), sum(1 for g in out if g["active"]))}

    @safe
    def metrika_goals_bulk(self):
        """Подтягивает цели из Метрики для всех ПРИВЯЗАННЫХ клиентов и СОХРАНЯЕТ их (с пресетом
        ключевых). Если у клиента цель уже была — её флаг active сохраняется (ручные правки не теряются)."""
        self._require_write()
        logins = sorted({b["login"] for b in self.db.list_bindings(self._owner())})
        res = {"clients": len(logins), "with_goals": 0, "no_counter": 0, "errors": 0, "details": []}
        for login in logins:
            try:
                found = self._metrika_goals_for(login)
            except Exception as e:  # noqa: BLE001
                res["errors"] += 1
                res["details"].append({"login": login, "status": "error", "reason": str(e)})
                continue
            goals = found["goals"]
            if not goals:
                res["no_counter"] += 1
                res["details"].append({"login": login, "status": "no_counter"})
                continue
            cur = self.db.get_client(login)
            prev = {}
            try:
                for g in json.loads((cur and cur["goals"]) or "[]"):
                    if isinstance(g, dict):
                        prev[str(g.get("id"))] = (g.get("active") is not False)
            except Exception:  # noqa: BLE001
                pass
            merged = [{"id": g["id"], "name": g["name"], "type": g.get("type", ""),
                       "active": prev.get(g["id"], g["active"])} for g in goals]
            self.db.upsert_client(login=login, goals=normalize_goals(merged))
            res["with_goals"] += 1
            res["details"].append({"login": login, "status": "ok",
                                   "goals": len(merged), "active": sum(1 for g in merged if g["active"])})
        return res

    @safe
    def client_goals(self, login):
        """Цели клиента для выбора в конструкторе: [{'id','name','active'}]."""
        self._require_owned(login)
        c = self.db.get_client(login)
        if not c:
            return []
        try:
            items = json.loads(c["goals"] or "[]")
        except (ValueError, TypeError):
            items = []
        out = []
        for g in items:
            if isinstance(g, dict):
                gid = str(g.get("id"))
                out.append({"id": gid, "name": g.get("name") or ("Цель " + gid),
                            "active": (g.get("active") is not False)})
            else:
                out.append({"id": str(g), "name": "Цель " + str(g), "active": True})
        return out

    @safe
    def sync_clients(self):
        """Подтянуть список клиентов агентства из Директа. Доступно админу И наблюдателю:
        работодатель раздаёт проекты, значит ему нужен свежий справочник (раньше упирался
        в «только администратор»). Чужие настройки при этом не трогаются — только справочник."""
        self._require_supervisor()
        clients = yandex.get_agency_clients(load_secrets()["yandex_oauth_token"])
        known = {c["login"] for c in self.db.list_clients("all")}
        n, fresh = 0, []
        for c in clients:
            if c.get("Login"):
                if c["Login"] not in known:
                    fresh.append(c.get("ClientInfo") or c["Login"])
                self.db.upsert_client(login=c["Login"], name=c.get("ClientInfo") or c["Login"], source="yandex")
                n += 1
        if fresh:
            self._audit("sync", "Директ", "новых клиентов: {} ({})".format(
                len(fresh), ", ".join(fresh[:5]) + ("…" if len(fresh) > 5 else "")))
            self._notify(None, "client", "Новые клиенты из Директа",
                         "{}: {}".format(len(fresh), ", ".join(fresh[:5]) + ("…" if len(fresh) > 5 else "")),
                         "clients")
        return {"synced": n, "new": len(fresh), "new_names": fresh[:20]}

    @safe
    def import_config(self):
        self._require_admin()
        rep = load_report_config()
        attribution = rep.get("attribution_model")
        n_cli = n_bind = 0
        for c in rep.get("clients") or []:
            login = c.get("login")
            if not login:
                continue
            self.db.upsert_client(login=login, name=c.get("name") or login,
                                  goals=normalize_goals(c.get("goals")),
                                  attribution=attribution, source="config")
            n_cli += 1
            if c.get("chat_id"):
                try:
                    self.db.set_binding(int(c["chat_id"]), login)
                    n_bind += 1
                except (TypeError, ValueError):
                    pass
        return {"clients": n_cli, "bindings": n_bind}

    # ---------- chats ----------
    @safe
    def chats(self):
        binds = {b["chat_id"]: b for b in self.db.list_bindings(self._owner())}
        names = {c["login"]: c["name"] for c in self.db.list_clients(self._owner())}
        out = []
        for c in self._visible_chats():
            b = binds.get(c["chat_id"])
            out.append({
                "chat_id": c["chat_id"], "title": c["title"], "type": c["type"],
                "status": c["status"], "added_at": c["added_at"],
                "login": b["login"] if b else None,
                "client_name": names.get(b["login"]) if b else None,
            })
        return out

    @safe
    def bind(self, chat_id, login):
        self._require_write()
        cid = int(chat_id)
        self._require_chat_visible(cid)
        if not login:
            self.db.remove_binding(cid)
            self._cloud_push_safe()
            return {"bound": False}
        self._require_bindable(login)      # свой или свободный клиент (не чужой)
        self.db.set_binding(cid, login)
        self._claim_if_pool(login)          # привязал свободного → закрепил за собой
        self._cloud_push_safe()
        self._audit("bind", login, "чат «{}»".format(self._chat_title(cid)))
        return {"bound": True}

    @safe
    def bind_for(self, chat_id, login, owner_id=None, delivery=None):
        """Привязать чат к клиенту и СРАЗУ назначить владельца-специалиста (админ/наблюдатель).
        Правило «привязал → взял» тут НЕ работает: раньше работодателю приходилось привязывать
        под собой, а потом переназначать десятки проектов вручную. owner_id=None — оставить
        владельца как есть; delivery — опционально задать способ доставки."""
        self._require_supervisor()
        cid = int(chat_id)
        if not login:
            self.db.remove_binding(cid)
            return {"bound": False}
        if not self.db.get_client(login):
            raise RuntimeError("Клиент не найден")
        self.db.set_binding(cid, login)
        if owner_id not in (None, "", 0, "0"):
            oid = int(owner_id)
            if not self.db.get_user(oid):
                raise RuntimeError("Специалист не найден")
            self.db.set_client_owner(login, oid)
        if delivery is not None:
            self.db.set_client_delivery(login, delivery)
        self._cloud_push_safe()
        c = self.db.get_client(login)
        who = self.db.get_user(int(owner_id)) if owner_id not in (None, "", 0, "0") else None
        self._audit("bind", login, "чат «{}»{}".format(
            self._chat_title(cid), (" → " + (who["name"] or who["email"])) if who else ""))
        if who and who["id"] != (self.user or {}).get("id"):
            self._notify(who["id"], "client", "Вам назначили проект",
                         "{} — чат «{}»".format((c["name"] if c else login), self._chat_title(cid)), "clients")
        return {"bound": True, "login": login,
                "owner": (c["owner"] if "owner" in c.keys() else None)}

    @safe
    def unbind(self, chat_id):
        self._require_write()
        cid = int(chat_id)
        self._require_chat_visible(cid)
        self.db.remove_binding(cid)
        self._cloud_push_safe()
        return {"bound": False}

    @safe
    def delete_chat(self, chat_id):
        """Удаляет чат из базы (для «висяков» — когда бота уже выгнали, а строка осталась)."""
        self._require_admin()
        self.db.delete_chat(int(chat_id))
        return {"deleted": True}

    # ---------- matcher (подсказки привязок) ----------
    def _suggest_matches(self):
        all_bound_ids = {b["chat_id"] for b in self.db.list_bindings("all")}
        bound_logins = {b["login"] for b in self.db.list_bindings("all")}
        chats = [c for c in self._visible_chats()
                 if c["status"] == "active" and c["chat_id"] not in all_bound_ids]
        # свои + свободные клиенты (специалист привязкой закрепляет свободного за собой)
        clients = [c for c in self._bindable_clients() if c["login"] not in bound_logins]
        free_clients = [{"login": c["login"], "name": c["name"]} for c in clients]
        out = []
        for ch in chats:
            best, best_score = None, 0.0
            title = (ch["title"] or "").lower()
            for c in clients:
                name = (c["name"] or "").lower()
                login = (c["login"] or "").lower()
                score = max(
                    difflib.SequenceMatcher(None, title, name).ratio(),
                    difflib.SequenceMatcher(None, title, login).ratio(),
                )
                # буст за прямое вхождение домена/имени/логина в название чата —
                # это почти всегда верная привязка (для массового авто-подключения)
                dom = name.split(".")[0].strip()
                if title and dom and len(dom) >= 3 and dom in title:
                    score = max(score, 0.95)
                if title and name and len(name) >= 4 and name in title:
                    score = max(score, 0.97)
                if title and login and len(login) >= 5 and login in title:
                    score = max(score, 0.95)
                if score > best_score:
                    best, best_score = c, score
            out.append({
                "chat_id": ch["chat_id"], "chat_title": ch["title"],
                "added_at": ch["added_at"],
                "suggest_login": best["login"] if best and best_score >= 0.45 else None,
                "suggest_name": best["name"] if best and best_score >= 0.45 else None,
                "confidence": int(best_score * 100),
            })
        return {"matches": out, "free_clients": free_clients}

    @safe
    def suggestions(self):
        return self._suggest_matches()

    @safe
    def bind_bulk(self, min_confidence=75, owner_id=None):
        """Массовая привязка: привязывает все непривязанные чаты, где уверенность подсказки
        >= порога. owner_id (админ/наблюдатель) — назначить все привязанные проекты этому
        специалисту вместо правила «привязал → взял себе». Остальное правишь вручную."""
        assign_to = None
        if owner_id not in (None, "", 0, "0"):
            self._require_supervisor()          # раздавать чужие проекты — только супервайзер
            assign_to = int(owner_id)
            if not self.db.get_user(assign_to):
                raise RuntimeError("Специалист не найден")
        else:
            self._require_write()
        thr = int(min_confidence)
        bound, details = 0, []
        for x in self._suggest_matches()["matches"]:
            if x.get("suggest_login") and x.get("confidence", 0) >= thr:
                try:
                    if assign_to is None:
                        self._require_bindable(x["suggest_login"])
                    self.db.set_binding(int(x["chat_id"]), x["suggest_login"])
                    if assign_to is not None:
                        self.db.set_client_owner(x["suggest_login"], assign_to)
                    else:
                        self._claim_if_pool(x["suggest_login"])   # привязал свободного → закрепил
                    bound += 1
                    details.append({"chat_title": x["chat_title"], "login": x["suggest_login"],
                                    "confidence": x["confidence"]})
                except Exception as e:  # noqa: BLE001
                    details.append({"chat_title": x["chat_title"], "error": str(e)})
        if bound:
            self._cloud_push_safe()
        return {"bound": bound, "min_confidence": thr, "details": details}

    # ---------- reports ----------
    @safe
    def preview(self, login):
        self._require_owned(login)
        token = load_secrets()["yandex_oauth_token"]
        intro, note, attr = self._report_ctx()
        text, camps, per = report.build_for_login(token, self.db, login, intro, note, attr)
        if text is None:
            return {"text": None, "reason": "Нет активных кампаний за последние 4 недели — клиент пропускается."}
        return {"text": text, "campaigns": len(camps), "period": per}

    @safe
    def copy_reports(self, logins=None):
        """Собирает недельные отчёты пачкой для КОПИПАСТА в сторонние мессенджеры (WhatsApp/VK/MAX/
        Яндекс и т.п.), куда бот не достаёт: ничего не отправляет — специалист сам копирует текст и
        вставляет в чат. logins — список клиентов (скоупится по владельцу); None = все свои.
        Возвращает блоки {login, name, text, status[ok|skipped|error], reason, campaigns}."""
        token = load_secrets()["yandex_oauth_token"]
        intro, note, attr = self._report_ctx()
        # скоуп как у списка клиентов (видимость): админ — все, специалист — свои. Не путать с
        # рассылкой (_scope_logins = только владелец): тут строим для любого ВИДИМОГО клиента.
        visible = {c["login"] for c in self.db.list_clients(self._owner())}
        targets = [l for l in (logins or sorted(visible)) if l in visible]
        external = self.db.external_logins()
        can_credit = not self._is_observer()   # наблюдатель не «сдаёт» — только смотрит
        out = []
        for login in targets:
            c = self.db.get_client(login)
            name = (c["name"] if c and c["name"] else login)
            try:
                text, camps, per = report.build_for_login(token, self.db, login, intro, note, attr)
                is_ext = can_credit and login in external
                if text is None:
                    # сторонний без открута — авто-скип в Контроле (не висит вечным долгом)
                    if is_ext:
                        self.db.log_send(login, None, per["date_from"], per["date_to"], "skipped", "нет открута")
                    out.append({"login": login, "name": name, "text": None,
                                "status": "skipped", "reason": "нет активных кампаний за 4 недели"})
                else:
                    credited = False
                    if is_ext:   # сбор отчёта стороннего = зачёт в Контроле
                        self.db.log_send(login, None, per["date_from"], per["date_to"], "sent")
                        credited = True
                    out.append({"login": login, "name": name, "text": text,
                                "status": "ok", "campaigns": len(camps), "credited": credited})
            except Exception as e:  # noqa: BLE001
                log_error("copy_reports." + login, e)
                out.append({"login": login, "name": name, "text": None,
                            "status": "error", "reason": str(e)})
        return out

    @safe
    def send_test(self, login):
        self._require_write()
        self._require_owned(login)
        token = load_secrets()["yandex_oauth_token"]
        intro, note, attr = self._report_ctx()
        return report.send_for_login(token, self._tg_client(), self.db, login, intro, note, attr)

    @safe
    def run_weekly(self):
        self._require_write()
        token = load_secrets()["yandex_oauth_token"]
        intro, note, attr = self._report_ctx()
        return report.run_weekly(token, self._tg_client(), self.db, intro, note, attr,
                                 logins=self._scope_logins(None))

    # ---------- рассылка с окном прогресса ----------
    def _run_weekly_worker(self, logins=None, dry_run=False):
        try:
            token = load_secrets()["yandex_oauth_token"]
            tg = self._tg_client()
            intro, note, attr = self._report_ctx()

            def prog(done, total, detail):
                self._run["done"] = done
                self._run["total"] = total
                if (detail or {}).get("status") == "running":
                    self._run["current"] = detail   # на ком стоим прямо сейчас
                    return
                self._run["current"] = None
                self._run["details"].append(detail)

            res = report.run_weekly(token, tg, self.db, intro, note, attr,
                                    on_progress=prog, logins=logins, dry_run=dry_run)
            self._run["summary"] = res
        except Exception as e:  # noqa: BLE001
            self._run["error"] = str(e)
        finally:
            self._run["running"] = False

    @safe
    def run_weekly_start(self, only_failed=False, dry_run=False):
        """Запускает рассылку в фоне (для окна прогресса). only_failed=True — только тем, кто в
        прошлый прогон не получил (ошибка/не отправлено). dry_run=True — «проба»: строит отчёты
        с прогрессом, но клиентам НЕ отправляет. Прогресс — через run_weekly_progress()."""
        self._require_write()
        if getattr(self, "_run", None) and self._run.get("running"):
            return {"already_running": True}
        logins = None
        if only_failed:
            prev = getattr(self, "_run", None) or {}
            summ = prev.get("summary") or {}
            logins = sorted({d["login"] for d in summ.get("details", [])
                             if d.get("status") in ("error", "no_chat")})
            if not logins:
                raise RuntimeError("Нет недошедших клиентов из прошлого прогона.")
        logins = self._scope_logins(logins)   # рассылать только СВОИХ клиентов (и админ тоже)
        if self.user and not logins:
            raise RuntimeError("У вас нет назначенных клиентов для рассылки.")
        self._run = {"running": True, "done": 0, "total": (len(logins) if logins else 0),
                     "details": [], "summary": None, "error": None, "current": None,
                     "only_failed": only_failed, "dry": bool(dry_run)}
        import threading
        threading.Thread(target=self._run_weekly_worker, args=(logins, bool(dry_run)),
                         daemon=True).start()
        return {"started": True, "only_failed": only_failed, "dry": bool(dry_run)}

    # ─────────── фоновые задачи ───────────
    # Passenger на shared-хостинге держит один процесс: пока он занят долгой
    # операцией, кабинет не отвечает никому. «Подтянуть из Директа» на 393
    # клиента, «Цели из Метрики (всем)» и выгрузка в таблицы — как раз такие.
    # Запускаем их в потоке, а кабинет опрашивает job_progress.
    # Одновременно тяжёлую работу делаем только одну: процесс на хостинге один,
    # две параллельные выгрузки просто мешают друг другу и кабинету.
    JOB_TIMEOUT = 20 * 60          # дольше двадцати минут — считаем, что задача зависла

    def _job_stale(self, j):
        """Задача помечена running, но процесс давно перезапустили или она зависла."""
        import time as _t
        if not j or j.get("state") != "running":
            return False
        started = j.get("started") or 0
        return (_t.time() - started) > self.JOB_TIMEOUT

    def _job_start(self, key, title, work):
        import time as _t
        db = self.db
        # подчистить зависшие с прошлой жизни процесса
        for j in db.jobs_running():
            if self._job_stale(j):
                db.job_set(j["key"], state="error", note="прервана",
                           error="Задача не завершилась за {} мин — вероятно, приложение перезапустили."
                           .format(self.JOB_TIMEOUT // 60))
        cur = db.job_get(key)
        if cur and cur.get("state") == "running":
            return {"already_running": True, "title": cur.get("title")}
        busy = [j for j in db.jobs_running() if j["key"] != key]
        if busy:
            return {"busy": True, "title": busy[0].get("title"),
                    "error": "Сейчас идёт другая операция: {}. Дождись её окончания."
                             .format(busy[0].get("title") or busy[0]["key"])}
        db.job_set(key, title=title, state="running", note="запускаю…", result=None, error=None,
                   started=_t.time(), finished=None,
                   owner=(self.user or {}).get("id"))

        def runner():
            try:
                res = work(lambda note: db.job_set(key, note=note))
                db.job_set(key, state="done", note="готово",
                           result=json.dumps(res, ensure_ascii=False, default=str))
            except Exception as e:  # noqa: BLE001
                db.job_set(key, state="error", note="ошибка", error=str(e)[:400])
                log_error("job." + key, str(e))

        import threading
        threading.Thread(target=runner, daemon=True).start()
        return {"started": True, "title": title}

    @safe
    def job_progress(self, key):
        """Состояние фоновой задачи. Переживает перезапуск приложения: лежит в базе."""
        j = self.db.job_get(key)
        if not j:
            return {"running": False, "unknown": True}
        if self._job_stale(j):
            j = self.db.job_set(key, state="error", note="прервана",
                                error="Задача не завершилась вовремя — вероятно, приложение перезапустили.")
        res = None
        if j.get("result"):
            try:
                res = json.loads(j["result"])
            except Exception:  # noqa: BLE001
                res = None
        return {"running": j.get("state") == "running", "title": j.get("title"),
                "note": j.get("note"), "result": res, "error": j.get("error"),
                "started": j.get("started"), "finished": j.get("finished")}

    @safe
    def jobs_state(self):
        """Что идёт прямо сейчас и что было недавно — для строки в шапке."""
        running = [j for j in self.db.jobs_running() if not self._job_stale(j)]
        return {"running": [{"key": j["key"], "title": j.get("title"), "note": j.get("note")}
                            for j in running],
                "recent": [{"key": j["key"], "title": j.get("title"), "state": j.get("state"),
                            "finished": j.get("finished")} for j in self.db.jobs_recent(5)]}

    @safe
    def sync_clients_start(self):
        """«Подтянуть из Директа» в фоне: 393 клиента — это десятки секунд."""
        self._require_admin()
        return self._job_start("sync", "Подтягиваю клиентов из Директа",
                               lambda say: _unwrap(self.sync_clients)(self))

    @safe
    def metrika_goals_bulk_start(self):
        """«Цели из Метрики (всем)» в фоне: ходит в Метрику по каждому клиенту."""
        self._require_write()
        return self._job_start("goals", "Тяну цели из Метрики",
                               lambda say: _unwrap(self.metrika_goals_bulk)(self))

    @safe
    def gsheets_push_start(self, login):
        """Выгрузка в Google-таблицу клиента в фоне."""
        self._require_write()
        self._require_owned(login)
        return self._job_start("gsheets", "Выгружаю в таблицу",
                               lambda say: _unwrap(self.gsheets_push)(self, login))

    @safe
    def run_weekly_progress(self):
        r = getattr(self, "_run", None) or {"running": False, "done": 0, "total": 0, "details": []}
        return {"running": r.get("running", False), "done": r.get("done", 0),
                "total": r.get("total", 0), "details": r.get("details", []),
                "summary": r.get("summary"), "error": r.get("error"),
                "current": r.get("current"),
                "only_failed": r.get("only_failed", False), "dry": r.get("dry", False)}

    @safe
    def history(self):
        owned = self._owned_set()
        if owned is None:
            rows = self.db.conn.execute(
                "SELECT * FROM send_log ORDER BY id DESC LIMIT 100").fetchall()
        elif owned:
            ph = ",".join("?" * len(owned))
            rows = self.db.conn.execute(
                "SELECT * FROM send_log WHERE login IN (%s) ORDER BY id DESC LIMIT 100" % ph,
                tuple(owned)).fetchall()
        else:
            rows = []
        names = {c["login"]: c["name"] for c in self.db.list_clients(self._owner())}
        return [{
            "sent_at": r["sent_at"], "login": r["login"], "client_name": names.get(r["login"], r["login"]),
            "chat_title": self._chat_title(r["chat_id"]) if r["chat_id"] else None,
            "period_from": r["period_from"], "period_to": r["period_to"],
            "status": r["status"], "error": r["error"],
        } for r in rows]

    # ---------- конструктор отчётов ----------
    @safe
    def report_options(self):
        from . import report_custom as RC
        return RC.options()

    @safe
    def report_campaigns(self, login):
        """Список кампаний клиента для фильтра конструктора (только чтение).

        Объединяет campaigns.get (быстро, настроенные кампании) и кампании из отчёта за
        90 дней — последнее ловит товарные/перформанс-кампании, которые campaigns.get v5
        не возвращает вообще (тип не поддержан методом)."""
        self._require_owned(login)
        from . import yandex, report
        from datetime import date, timedelta
        token = load_secrets()["yandex_oauth_token"]
        seen = {}
        try:
            for c in yandex.get_campaigns(token, login):
                seen[str(c.get("Id"))] = c.get("Name") or str(c.get("Id"))
        except Exception:  # noqa: BLE001
            pass
        try:
            today = date.today()
            rows = report.fetch_report(token, login, (today - timedelta(days=90)).isoformat(),
                                       today.isoformat(), ["CampaignId", "CampaignName"],
                                       report_type="CAMPAIGN_PERFORMANCE_REPORT")
            for r in rows:
                cid = str(r.get("CampaignId"))
                if cid:
                    seen[cid] = r.get("CampaignName") or seen.get(cid, cid)
        except Exception:  # noqa: BLE001 — отчёт мог не успеть; вернём хотя бы campaigns.get
            pass
        out = [{"id": cid, "name": nm} for cid, nm in seen.items()]
        out.sort(key=lambda x: (x["name"] or "").lower())
        return out

    def _report_build(self, login, level, date_from, date_to, attribution, limit,
                      segments=None, date_grain="day", campaign=None, goal_ids=None):
        self._require_owned(login)
        from . import report_custom as RC
        token = load_secrets()["yandex_oauth_token"]
        c = self.db.get_client(login)
        if not c:
            raise RuntimeError("Клиент {} не найден".format(login))
        # goal_ids передан (даже пустой) -> ровно эти цели; иначе — активные «для отчётов»
        if goal_ids is not None:
            goal_defs = report.goal_defs_from_client(c, only_ids=goal_ids)
        else:
            goal_defs = report.goal_defs_from_client(c)
        if not date_from or not date_to:
            per = report.period()
            date_from = date_from or per["date_from"]
            date_to = date_to or per["date_to"]
        res = RC.build(token, login, level or "campaign", date_from, date_to,
                       attribution or default_attribution(), goal_defs, segments, date_grain or "day", campaign, limit or 100)
        res["client_name"] = c["name"] or login
        res["text"] = RC.to_text(login, c["name"] or login, res)
        return res

    @safe
    def report_query(self, login, level="campaign", date_from=None, date_to=None, attribution=None,
                     limit=100, segments=None, date_grain="day", campaign=None, goal_ids=None):
        res = self._report_build(login, level, date_from, date_to, attribution, limit, segments, date_grain, campaign, goal_ids)
        res["chats"] = [{"chat_id": b["chat_id"], "title": self._chat_title(b["chat_id"])}
                        for b in self.db.bindings_for_login(login)]
        return res

    @safe
    def report_send(self, login, level="campaign", date_from=None, date_to=None, attribution=None,
                    limit=100, segments=None, date_grain="day", campaign=None, goal_ids=None):
        self._require_write()
        res = self._report_build(login, level, date_from, date_to, attribution, limit, segments, date_grain, campaign, goal_ids)
        chats = self.db.bindings_for_login(login)
        if not chats:
            raise RuntimeError("Клиент не привязан ни к одному чату")
        tg = self._tg_client()
        sent = 0
        for b in chats:
            tg.send_message(b["chat_id"], res["text"])
            self.db.log_send(login, b["chat_id"], res["date_from"], res["date_to"], "sent")
            sent += 1
        return {"sent": sent}

    @safe
    def dossier_options(self):
        """Справочник разрезов для вкладки «Досье»."""
        from . import dossier as DS
        return {"cuts": DS.cut_options()}

    @safe
    def dossier(self, login, a_from=None, a_to=None, b_from=None, b_to=None,
                attribution=None, goal_ids=None, cuts=None, sheet_url=None):
        """Досье: период A против периода B. Даты обоих периодов считает кабинет
        и присылает готовыми — здесь ничего не достраивается по умолчанию."""
        self._require_owned(login)
        from . import dossier as DS
        token = load_secrets()["yandex_oauth_token"]
        c = self.db.get_client(login)
        if not c:
            raise RuntimeError("Клиент {} не найден".format(login))
        if goal_ids is not None:
            goal_defs = report.goal_defs_from_client(c, only_ids=goal_ids)
        else:
            goal_defs = report.goal_defs_from_client(c)
        if not (a_from and a_to and b_from and b_to):
            raise RuntimeError("Нужны даты обоих периодов")
        if a_from > a_to:
            a_from, a_to = a_to, a_from
        if b_from > b_to:
            b_from, b_to = b_to, b_from
        note = c["note"] if "note" in c.keys() else None
        u = self.db.get_user(self.user["id"]) if self.user else None
        signature = (u["note"] if u and "note" in u.keys() else None) or ""
        return DS.build(token, login, c["name"] or login, a_from, a_to, b_from, b_to,
                        attribution or default_attribution(), goal_defs, cuts or [],
                        client_note=note, signature=signature, sheet_url=sheet_url)

    @safe
    def dossier_cut(self, login, cut, a_from=None, a_to=None, b_from=None, b_to=None,
                    attribution=None, goal_ids=None):
        """Один дополнительный разрез досье. Кабинет запрашивает их по очереди,
        чтобы каждый HTTP-запрос оставался коротким."""
        self._require_owned(login)
        from . import dossier as DS
        token = load_secrets()["yandex_oauth_token"]
        c = self.db.get_client(login)
        if not c:
            raise RuntimeError("Клиент {} не найден".format(login))
        if goal_ids is not None:
            goal_defs = report.goal_defs_from_client(c, only_ids=goal_ids)
        else:
            goal_defs = report.goal_defs_from_client(c)
        if not (a_from and a_to and b_from and b_to):
            raise RuntimeError("Нужны даты обоих периодов")
        return DS.build_cut(token, login, cut, a_from, a_to, b_from, b_to,
                            attribution or default_attribution(), goal_defs)

    # ─────────── здоровье интеграций ───────────
    def _probe(self, name, fn):
        """Один замер: сколько заняло и чем кончилось."""
        import time as _t
        t0 = _t.time()
        try:
            info = fn() or {}
            return {"name": name, "ok": True, "ms": int((_t.time() - t0) * 1000), **info}
        except Exception as e:  # noqa: BLE001
            return {"name": name, "ok": False, "ms": int((_t.time() - t0) * 1000),
                    "error": str(e)[:200]}

    def _health_probe_all(self, say=None):
        """Опрашиваем все внешние сервисы. Долго (секунды), поэтому только в фоне."""
        from . import yandex, metrika, gsheets
        out = []

        def note(t):
            if say:
                say(t)

        note("Директ…")
        def _direct():
            token = load_secrets()["yandex_oauth_token"]
            rows = self.db.list_clients("all")
            login = rows[0]["login"] if rows else None
            if not login:
                return {"detail": "нет клиентов для проверки"}
            camps = yandex.get_campaigns(token, login)
            return {"detail": "кампаний у первого клиента: {}".format(len(camps))}
        out.append(self._probe("Яндекс.Директ", _direct))

        note("Метрика…")
        def _metrika():
            token = load_secrets()["yandex_oauth_token"]
            data = metrika._get(metrika.API + "counters?per_page=1", token)
            return {"detail": "счётчиков доступно: {}".format(data.get("rows", "?"))}
        out.append(self._probe("Яндекс.Метрика", _metrika))

        note("Google-таблицы…")
        def _google():
            res = gsheets.access_check()
            if not res.get("ok"):
                raise RuntimeError(res.get("error") or "нет доступа")
            return {"detail": "таблиц видно: {}".format(res.get("sheets", 0))}
        out.append(self._probe("Google-таблицы", _google))

        note("Telegram…")
        def _tg():
            me = self._tg_client().get_me() or {}   # _call отдаёт уже result
            return {"detail": "бот @{}".format(me.get("username") or "?")}
        out.append(self._probe("Telegram", _tg))

        note("база…")
        def _db():
            n = len(self.db.list_clients("all"))
            return {"detail": "клиентов в базе: {}".format(n)}
        out.append(self._probe("База данных", _db))

        try:
            self.db.set_kv("health_last", json.dumps(out, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass
        return {"items": out, "bad": sum(1 for x in out if not x["ok"])}

    @safe
    def health_start(self):
        """Проверка всех интеграций в фоне: каждый вызов — секунды."""
        self._require_admin()
        return self._job_start("health", "Проверяю интеграции",
                               lambda say: self._health_probe_all(say))

    @safe
    def health_last(self):
        """Последний известный результат — чтобы экран не был пустым до проверки."""
        self._require_admin()
        raw = self.db.get_kv("health_last")
        try:
            return {"items": json.loads(raw) if raw else []}
        except Exception:  # noqa: BLE001
            return {"items": []}

    @safe
    def chat_check(self, chat_id):
        """Бот всё ещё в чате? Раньше это выяснялось в момент отправки клиенту."""
        self._require_chat_visible(int(chat_id))
        return self._tg_client().chat_ok(int(chat_id))

    @safe
    def counter_check(self, login):
        """Доступен ли счётчик Метрики у клиента — до того, как тянуть цели."""
        self._require_owned(login)
        from . import yandex, metrika
        token = load_secrets()["yandex_oauth_token"]
        counters = yandex.get_campaign_counters(token, login) or []
        if not counters:
            return {"ok": False, "error": "У клиента не найден счётчик Метрики в кампаниях"}
        res = metrika.counter_available(token, counters[0])
        res["counter"] = counters[0]
        return res

    # Отправка досье клиенту появится, когда режим выйдет из демо. Метода намеренно нет:
    # диспетчер /api/<method> вызывает по имени, поэтому «просто не показать кнопку» мало —
    # пока функции не существует, в чат клиента ничего уйти не может.

    @safe
    def report_export_xlsx(self, login, level="campaign", date_from=None, date_to=None, attribution=None,
                           limit=1000, segments=None, date_grain="day", campaign=None, goal_ids=None):
        """Строит отчёт и сохраняет .xlsx в подпапку reports/ рядом с программой. Ничего не отправляет."""
        import os
        import re
        from . import report_custom as RC
        from .settings import BASE_DIR
        res = self._report_build(login, level, date_from, date_to, attribution, limit, segments, date_grain, campaign, goal_ids)
        folder = os.path.join(BASE_DIR, "reports")
        os.makedirs(folder, exist_ok=True)
        safe_login = re.sub(r"[^A-Za-z0-9_.-]", "_", str(login))
        fn = "report_{}_{}_{}_{}.xlsx".format(safe_login, level or "campaign", res["date_from"], res["date_to"])
        path = os.path.join(folder, fn)
        RC.to_xlsx(res, path)
        return {"path": path, "filename": fn, "n_rows": res["n_shown"]}

    # ---------- Google-таблицы (выгрузка «как в табличках клиента») ----------
    def _find_client_sheet(self, gc, client_name):
        """Ищет Google-таблицу клиента (заголовок «Auto-Reporter ОТЧЕТ <домен>») по домену.
        Возвращает (spreadsheet_id, domain) или (None, None)."""
        from . import gsheets as G
        sheets = G.discover(gc)
        for d in self._client_domains(client_name):
            key = str(d).strip().lower().replace("www.", "")
            if key in sheets:
                return sheets[key]["id"], key
        return None, None

    @safe
    def gsheets_status(self):
        """Доступен ли сервисный ключ и какие таблицы расшарены на сервисный аккаунт."""
        from . import gsheets as G
        if not G.available():
            return {"available": False,
                    "note": "Ключ sa_key.json не найден рядом с программой — положи его туда."}
        sheets = G.discover()
        return {"available": True,
                "sheets": [{"domain": k, "title": v["title"]} for k, v in sorted(sheets.items())]}

    @safe
    def gsheets_clients(self):
        """Клиенты (в моём скоупе), у которых есть обнаруженная Google-таблица по домену —
        чтобы в выпадашке не мелькали все подряд, а только реально выгружаемые."""
        from . import gsheets as G
        if not G.available():
            return []
        sheets = G.discover()
        out = []
        for c in self.db.list_clients(self._owner()):
            for d in self._client_domains(c["name"]):
                key = str(d).strip().lower().replace("www.", "")
                if key in sheets:
                    sid = sheets[key]["id"]
                    out.append({"login": c["login"], "name": c["name"] or c["login"],
                                "sheet": sheets[key]["title"], "sheet_id": sid,
                                "url": "https://docs.google.com/spreadsheets/d/{}".format(sid)})
                    break
        return out

    @staticmethod
    def _last_full_month():
        """(date_from, date_to) прошлого ПОЛНОГО месяца относительно сегодня."""
        from datetime import date, timedelta
        first = date.today().replace(day=1)
        last_prev = first - timedelta(days=1)
        return last_prev.replace(day=1).isoformat(), last_prev.isoformat()

    @safe
    def gsheets_push(self, login):
        """Авто-заполняет листы-ленты Google-таблицы клиента свежими данными из Директа:
        «Общий по неделям» → прошлая закрытая неделя, «Общий по месяцам» → прошлый полный месяц.

        Пишет сырые входы (Показы/Клики/Расход с НДС/цели Метрики); формулы (CTR/CPC/CR/
        конверсии total/CPA) продлеваются; внешние столбцы (Callibri/Ticketscloud) не трогаются;
        повтор того же периода пропускается (дедуп).
        """
        self._require_write()
        self._require_owned(login)
        from . import gsheets as G
        if not G.available():
            raise RuntimeError("Ключ sa_key.json не найден рядом с программой.")
        c = self.db.get_client(login)
        if not c:
            raise RuntimeError("Клиент не найден")
        token = load_secrets()["yandex_oauth_token"]
        goals = self._metrika_goals_for(login).get("goals", [])
        gc = G.client(readonly=False)
        sid, domain = self._find_client_sheet(gc, c["name"])
        if not sid:
            raise RuntimeError("Не нашёл Google-таблицу «Auto-Reporter ОТЧЕТ …» для клиента {} "
                               "(домен из карточки: {})".format(c["name"] or login, c["name"]))
        results = G.push_timeseries(gc, sid, token, login, goals)
        if not results:
            raise RuntimeError("В таблице нет листов-лент («Общий по неделям»/«по месяцам»).")
        return {"domain": domain, "results": results}

    @safe
    def gsheets_breakdowns(self, login, which=None, date_from=None, date_to=None):
        """Создаёт НОВЫЕ листы-снимки разрезов (По РК/группам/ключам/поисковым фразам/регионам)
        за период (по умолчанию — прошлый полный месяц). which=None → все разрезы."""
        self._require_write()
        self._require_owned(login)
        from . import gsheets as G
        if not G.available():
            raise RuntimeError("Ключ sa_key.json не найден рядом с программой.")
        c = self.db.get_client(login)
        if not c:
            raise RuntimeError("Клиент не найден")
        token = load_secrets()["yandex_oauth_token"]
        if not (date_from and date_to):
            from datetime import date
            today = date.today()
            date_from, date_to = today.replace(day=1).isoformat(), today.isoformat()
        keys = [which] if which else list(G.BREAKDOWNS.keys())
        # цели клиента — чтобы разрезы считали конверсии как лента «по неделям» (сходятся)
        goals = self._metrika_goals_for(login).get("goals", [])
        gc = G.client(readonly=False)
        sid, domain = self._find_client_sheet(gc, c["name"])
        if not sid:
            raise RuntimeError("Не нашёл Google-таблицу «Auto-Reporter ОТЧЕТ …» для клиента {}"
                               .format(c["name"] or login))
        results = []
        for k in keys:
            try:
                r = G.push_breakdown(gc, sid, token, login, k, date_from, date_to, goals=goals)
                results.append({"which": k,
                                "status": "создан «{}» ({} из {} строк)".format(
                                    r["created"], r["n_rows"], r["n_total"])})
            except Exception as e:  # noqa: BLE001 — один разрез не должен ронять остальные
                results.append({"which": k, "status": "ошибка: " + str(e)})
        return {"domain": domain, "period": [date_from, date_to], "results": results}

    # ---------- общая онлайн-база (привязки/цели через Google-таблицу) ----------
    def _sa_email(self):
        try:
            import json as _json
            from . import gsheets as G
            return _json.load(open(G.key_path(), encoding="utf-8")).get("client_email", "")
        except Exception:  # noqa: BLE001
            return ""

    def _cloud_push_safe(self):
        """Заливает состояние в облако после изменений. Тихо, не роняет операцию при сбое.
        В веб-режиме (мультиюзер, self.user задан) НЕ трогаем общую таблицу-конфиг — источником
        истины стала БД; иначе привязки одного пользователя затирали бы общий лист."""
        if self.user is not None:
            return
        try:
            from . import cloudsync
            if cloudsync.available():
                cloudsync.push(self.db)
        except Exception as e:  # noqa: BLE001
            try:
                print("[api] cloud push: {}".format(e))
            except Exception:  # noqa: BLE001
                pass

    @safe
    def cloud_status(self):
        from . import cloudsync
        if not cloudsync.available():
            return {"available": False, "note": "Нет ключа sa_key.json рядом с программой."}
        _, sid, name = cloudsync.find_config()
        if not sid:
            return {"available": True, "configured": False, "sa_email": self._sa_email(),
                    "note": "Создай Google-таблицу «Auto-Reporter КОНФИГ» и расшарь её (Редактор) "
                            "на сервисный аккаунт — тогда привязки станут общими для всех устройств."}
        return {"available": True, "configured": True, "sheet": name}

    @safe
    def cloud_pull(self):
        """Тянет привязки/цели из общей таблицы в локальную базу."""
        self._require_admin()
        from . import cloudsync
        return cloudsync.pull(self.db)

    @safe
    def cloud_push(self):
        """Заливает локальные привязки/цели в общую таблицу."""
        self._require_admin()
        from . import cloudsync
        return cloudsync.push(self.db)

    # ---------- пользователи (админ) ----------
    @safe
    def users_list(self):
        """Список сотрудников (админ и наблюдатель — работодателю нужно управлять командой)."""
        self._require_supervisor()
        out = []
        for u in self.db.list_users():
            out.append({"id": u["id"], "email": u["email"], "name": u["name"] or "",
                        "role": u["role"], "active": bool(u["active"]),
                        "clients": len(self.db.owned_logins(u["id"])),
                        # когда человек заходил в последний раз: видно, кто уже не работает
                        "last_login": self.db.last_login(u["id"])})
        return out

    @safe
    def user_create(self, email, password, name=None, role="user"):
        """Завести сотрудника. Наблюдатель (работодатель) тоже может — он же нанимает; но выдать
        роль «Администратор» (доступ к токенам/журналу) может только админ."""
        auth.check_password_rules(password)
        self._require_supervisor()
        from . import auth
        email = (email or "").strip().lower()
        if not email or not password:
            raise RuntimeError("Нужны email и пароль")
        if role not in ("user", "admin", "observer"):
            role = "user"
        if role == "admin":
            self._require_admin()
        if self.db.get_user_by_email(email):
            raise RuntimeError("Пользователь с таким email уже есть")
        uid = self.db.create_user(email, auth.hash_password(password), name, role)
        self._audit("user", (name or email), "создан сотрудник, роль: " + role)
        return {"id": uid, "email": email, "role": role}

    def _require_manage_user(self, user_id):
        """Наблюдатель управляет специалистами и наблюдателями, но НЕ администраторами."""
        self._require_supervisor()
        u = self.db.get_user(int(user_id))
        if not u:
            raise RuntimeError("Пользователь не найден")
        if u["role"] == "admin" and not self._is_admin():
            raise RuntimeError("Администратора может менять только администратор")
        return u

    @safe
    def user_set_role(self, user_id, role):
        """Сменить роль (в т.ч. выдать «Наблюдатель»). Роль «Администратор» назначает только админ."""
        u = self._require_manage_user(user_id)
        if role not in ("user", "admin", "observer"):
            raise RuntimeError("Неизвестная роль")
        if role == "admin":
            self._require_admin()
        self.db.set_user_role(int(user_id), role)
        self._audit("user", (u["name"] or u["email"]), "роль изменена: {} → {}".format(u["role"], role))
        return {"id": int(user_id), "role": role}

    @safe
    def user_set_active(self, user_id, active):
        u = self._require_manage_user(user_id)
        self.db.set_user_active(int(user_id), bool(active))
        self._audit("user", (u["name"] or u["email"]), "разблокирован" if active else "заблокирован")
        return {"id": int(user_id), "active": bool(active)}

    @safe
    def user_set_password(self, user_id, password):
        auth.check_password_rules(password)
        u = self._require_manage_user(user_id)
        from . import auth
        if not password:
            raise RuntimeError("Пустой пароль")
        self.db.set_user_password(int(user_id), auth.hash_password(password))
        self._audit("user", (u["name"] or u["email"]), "сменён пароль")
        return {"id": int(user_id)}

    @safe
    def pool_clients(self):
        """Все клиенты агентства с владельцем и способом доставки — для раздачи (админ/наблюдатель)."""
        self._require_supervisor()
        emails = {u["id"]: u["email"] for u in self.db.list_users()}
        names = {u["id"]: (u["name"] or u["email"]) for u in self.db.list_users()}
        out = []
        for c in self.db.list_clients("all"):
            owner = c["owner"] if "owner" in c.keys() else None
            out.append({"login": c["login"], "name": c["name"],
                        "owner": owner, "owner_email": emails.get(owner),
                        "owner_name": names.get(owner),
                        "delivery": (c["delivery"] if "delivery" in c.keys() else None) or "telegram"})
        return out

    @safe
    def assignable_users(self):
        """Список специалистов/админов для раздачи проектов (наблюдателю и админу)."""
        self._require_supervisor()
        return [{"id": u["id"], "name": u["name"] or u["email"], "email": u["email"], "role": u["role"]}
                for u in self.db.list_users() if u["active"] and u["role"] in ("user", "admin")]

    @safe
    def assign_client(self, login, user_id=None, delivery=None):
        """Назначить клиента специалисту (user_id=None/'' → общий пул) и сразу задать способ
        доставки (delivery='external'|'telegram'). Доступно админу и наблюдателю (работодатель
        выставляет проекты)."""
        self._require_supervisor()
        if not self.db.get_client(login):
            raise RuntimeError("Клиент не найден")
        owner = None
        if user_id not in (None, "", 0, "0"):
            owner = int(user_id)
            if not self.db.get_user(owner):
                raise RuntimeError("Пользователь не найден")
        self.db.set_client_owner(login, owner)
        if delivery is not None:
            self.db.set_client_delivery(login, "external" if delivery == "external" else "telegram")
        who = self.db.get_user(owner) if owner else None
        self._audit("assign", login, "владелец: " + ((who["name"] or who["email"]) if who else "общий пул"))
        if owner and owner != (self.user or {}).get("id"):
            c = self.db.get_client(login)
            self._notify(owner, "client", "Вам назначили проект",
                         "{} ({})".format((c["name"] if c else login), login), "clients")
        return {"login": login, "owner": owner,
                "delivery": ("external" if delivery == "external" else "telegram") if delivery is not None else None}

    @safe
    def reassign_all(self, from_user_id, to_user_id):
        """Перекинуть ВСЕ проекты одного специалиста другому (увольнение, отпуск, передача дел).
        from_user_id='none' — раздать всех «ничьих». to_user_id='none' — вернуть в общий пул."""
        self._require_supervisor()
        src = None if from_user_id in (None, "", 0, "0", "none") else int(from_user_id)
        dst = None if to_user_id in (None, "", 0, "0", "none") else int(to_user_id)
        if src is not None and not self.db.get_user(src):
            raise RuntimeError("Специалист-источник не найден")
        if dst is not None and not self.db.get_user(dst):
            raise RuntimeError("Специалист-получатель не найден")
        if src == dst:
            raise RuntimeError("Источник и получатель совпадают")
        moved = []
        for c in self.db.list_clients("all"):
            owner = c["owner"] if "owner" in c.keys() else None
            if owner == src:
                self.db.set_client_owner(c["login"], dst)
                moved.append(c["login"])
        nm = lambda uid: (lambda u: (u["name"] or u["email"]) if u else "общий пул")(  # noqa: E731
            self.db.get_user(uid) if uid else None)
        self._audit("reassign", nm(dst), "передано {} проект(ов) от «{}»".format(len(moved), nm(src)))
        if dst and moved:
            self._notify(dst, "client", "Вам передали проекты",
                         "{} проект(ов) от «{}»".format(len(moved), nm(src)), "clients")
        return {"moved": len(moved), "logins": moved[:50], "from": src, "to": dst}

    @safe
    def set_delivery_super(self, login, mode):
        """Сменить доставку клиента при раздаче (админ/наблюдатель), НЕ трогая владельца."""
        self._require_supervisor()
        if not self.db.get_client(login):
            raise RuntimeError("Клиент не найден")
        self.db.set_client_delivery(login, "external" if mode == "external" else "telegram")
        self._audit("delivery", login, "доставка: " + ("сторонний (копипаст)" if mode == "external" else "Telegram"))
        return {"login": login, "delivery": "external" if mode == "external" else "telegram"}

    # ---------- заметки по проектам ----------
    @safe
    def set_client_note(self, login, note):
        """Заметка по проекту («клиент на паузе до августа») — видна всем причастным.
        Свой клиент — специалисту; любой — админу/наблюдателю."""
        c = self.db.get_client(login)
        if not c:
            raise RuntimeError("Клиент не найден")
        owner = c["owner"] if "owner" in c.keys() else None
        if not (self._is_admin() or self._is_observer() or (self.user and owner == self.user["id"])):
            raise RuntimeError("Можно писать заметку только по своему клиенту")
        note = (note or "").strip() or None
        self.db.set_client_note(login, note)
        self._audit("note", login, (note or "заметка удалена")[:120])
        return {"login": login, "note": note}

    # ---------- дайджест работодателю ----------
    def _digest_text(self):
        """Текст еженедельной сводки: покрытие, кто не сдал, деньги на исходе, свободные чаты."""
        sup = (self.supervision() or {}).get("data") or {}
        wl = (self.workload() or {}).get("data") or {}
        ag = sup.get("agency") or {}
        rows = sup.get("rows") or []
        L = ["📊 СВОДКА ПО АГЕНТСТВУ", ""]
        cov = ag.get("coverage")
        L.append("Сдача недели: {} · обязательств {} · сдано {} · долгов {}".format(
            (str(cov) + "%") if cov is not None else "—",
            ag.get("bound_total", 0), ag.get("sent_total", 0), ag.get("debt_total", 0)))
        L.append("")
        L.append("По сотрудникам:")
        for r in sorted(rows, key=lambda x: (x["coverage"] if x["coverage"] is not None else 999)):
            if not r["bound"]:
                continue
            mark = "✅" if not r["debt"] else ("⚠️" if r["sent"] else "❌")
            L.append("{} {}: {}% ({}/{}){}".format(
                mark, r["name"], (r["coverage"] if r["coverage"] is not None else 0),
                r["sent"], r["bound"],
                (" · долг: " + ", ".join(m["name"] for m in r["missing"][:5])) if r["debt"] else ""))
        # деньги
        brows = [dict(b) for b in self.db.list_budgets()]
        crit = [b for b in brows if b["status"] == "critical"]
        if crit:
            L += ["", "💰 Бюджет на исходе ({}):".format(len(crit))]
            for b in sorted(crit, key=lambda x: (x["days_left"] if x["days_left"] is not None else 99))[:8]:
                L.append("• {}: ~{} дн.".format(b["name"] or b["login"],
                                                b["days_left"] if b["days_left"] is not None else "?"))
        # нагрузка и хвосты
        if wl:
            L += ["", "⚖️ Нагрузка: " + " · ".join(
                "{} {}".format(r["name"], r["obligations"]) for r in (wl.get("rows") or [])[:6])]
            if wl.get("unassigned_bound"):
                L.append("📥 Без владельца, но с чатом: {} — стоит раздать".format(wl["unassigned_bound"]))
        L += ["", "Подробности — в кабинете: reports.iig.ru"]
        return "\n".join(L)

    @safe
    def digest_preview(self):
        """Посмотреть текст сводки, не отправляя."""
        self._require_supervisor()
        return {"text": self._digest_text()}

    @safe
    def digest_send(self):
        """Отправить сводку всем подписчикам «по всему агентству» (alert_scope='all')."""
        self._require_supervisor()
        text = self._digest_text()
        sent, missing = 0, []
        try:
            tg = self._tg_client()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("Telegram-бот недоступен: {}".format(e))
        from . import budgets as B
        by_id, by_un, by_title = B._priv_index(self.db)
        for u in self.db.list_users():
            if not u["active"]:
                continue
            if (u["alert_scope"] if "alert_scope" in u.keys() else None) != "all":
                continue
            ch = B._resolve_chat(u, by_id, by_un, by_title)
            if not ch:
                missing.append(u["name"] or u["email"])
                continue
            tg.send_message(ch["chat_id"], text)
            sent += 1
        self.db.set_kv("digest_last", str(__import__("time").time()))
        self._audit("digest", "сводка", "отправлено получателям: {}".format(sent))
        return {"sent": sent, "missing": missing}

    # ---------- диалоги с клиентами: кто ждёт ответа ----------
    SILENT_DAYS = 21          # столько дней без единого сообщения — «клиент забыт»
    WAIT_WARN_HOURS = 4       # ждёт дольше — подсветка
    WAIT_CRIT_HOURS = 24      # ждёт дольше — красным + уведомление

    def _chat_dialogs(self):
        """Сырые данные по диалогам в скоупе пользователя: кто ждёт ответа и кто молчит."""
        import datetime as dt

        def _age_h(iso):
            if not iso:
                return None
            try:
                t = dt.datetime.fromisoformat(iso)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=dt.timezone.utc)
                return round((dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600, 1)
            except (TypeError, ValueError):
                return None

        act = {a["chat_id"]: a for a in self.db.list_chat_activity()}
        bind = {b["chat_id"]: b["login"] for b in self.db.list_bindings("all")}
        clients = {c["login"]: c for c in self.db.list_clients("all")}
        users = {u["id"]: (u["name"] or u["email"]) for u in self.db.list_users()}
        own = self._owned_set()          # None = вижу всё (наблюдатель/десктоп)
        waiting, silent, hidden = [], [], []
        for ch in self.db.list_chats():
            if ch["status"] != "active" or (ch["type"] or "") not in ("group", "supergroup"):
                continue
            login = bind.get(ch["chat_id"])
            if own is not None and (login is None or login not in own):
                continue                  # чужой/непривязанный чат — не моя зона
            c = clients.get(login) if login else None
            owner = (c["owner"] if (c and "owner" in c.keys()) else None)
            a = act.get(ch["chat_id"])
            row = {"chat_id": ch["chat_id"], "chat_title": ch["title"],
                   "login": login, "client_name": (c["name"] if c else None),
                   "owner": owner, "owner_name": users.get(owner),
                   "last_client_at": a["last_client_at"] if a else None,
                   "last_client_text": a["last_client_text"] if a else None,
                   "last_client_name": a["last_client_name"] if a else None,
                   "last_our_at": a["last_our_at"] if a else None,
                   "last_our_name": a["last_our_name"] if a else None}
            lc, lo = row["last_client_at"], row["last_our_at"]
            # снятие с ожидания действует ровно до следующего сообщения клиента
            off = (a["wait_off_at"] if (a and "wait_off_at" in a.keys()) else None)
            off_by = (a["wait_off_by"] if (a and "wait_off_by" in a.keys()) else None)
            row["wait_off_at"] = off
            row["wait_off_by_name"] = users.get(off_by)
            if lc and (not lo or lo < lc):
                row["wait_h"] = _age_h(lc)
                (hidden if (off and off >= lc) else waiting).append(row)
            last_any = max([x for x in (lc, lo, (a["last_bot_at"] if a else None)) if x], default=None)
            row["last_any_at"] = last_any
            row["silent_days"] = round((_age_h(last_any) or 0) / 24, 1) if last_any else None
            if last_any and row["silent_days"] and row["silent_days"] >= self.SILENT_DAYS:
                silent.append(row)
        waiting.sort(key=lambda r: -(r.get("wait_h") or 0))
        hidden.sort(key=lambda r: -(r.get("wait_h") or 0))
        silent.sort(key=lambda r: -(r.get("silent_days") or 0))
        return waiting, silent, hidden

    @safe
    def import_chat_history(self, payload):
        """Импорт истории чата из экспорта Telegram Desktop (result.json).

        Bot API не отдаёт переписку задним числом, но человек может выгрузить её из
        Telegram Desktop («Экспорт истории чата» → JSON) и загрузить сюда — тогда «кто ждёт
        ответа» и «давно не общались» заполнятся реальными данными сразу.
        Принимает объект экспорта (chats.list[] или один чат с messages[])."""
        self._require_write()
        import json as _json
        import datetime as _dt
        if isinstance(payload, str):
            payload = _json.loads(payload)
        chats = payload.get("chats", {}).get("list") if isinstance(payload, dict) else None
        if chats is None:
            chats = [payload] if isinstance(payload, dict) else []
        our_ids = self.db.our_telegram_ids(self.cfg.get("admin_user_ids"))
        known = {c["chat_id"] for c in self.db.list_chats()}
        res = {"chats": 0, "matched": 0, "messages": 0, "details": []}
        for ch in chats:
            msgs = ch.get("messages") or []
            if not msgs:
                continue
            res["chats"] += 1
            # id чата в экспорте — положительный; в Bot API у супергрупп префикс -100
            raw_id = ch.get("id")
            cands = []
            if raw_id is not None:
                cands = [int(raw_id), -int(raw_id), int("-100{}".format(abs(int(raw_id))))]
            cid = next((c for c in cands if c in known), None)
            if cid is None:   # пробуем по названию чата
                nm = (ch.get("name") or "").strip().lower()
                cid = next((c["chat_id"] for c in self.db.list_chats()
                            if (c["title"] or "").strip().lower() == nm and nm), None)
            if cid is None:
                res["details"].append({"chat": ch.get("name"), "status": "не нашёл такой чат в базе"})
                continue
            last = {"client": None, "our": None, "client_name": None, "client_text": None, "our_name": None}
            for m in msgs:
                if m.get("type") != "message":
                    continue
                date = m.get("date")   # '2026-07-20T12:34:56'
                if not date:
                    continue
                txt = m.get("text")
                if isinstance(txt, list):   # текст с форматированием — склеиваем
                    txt = "".join(x if isinstance(x, str) else (x.get("text") or "") for x in txt)
                fid = str(m.get("from_id") or "")
                uid = int(fid.replace("user", "")) if fid.startswith("user") and fid[4:].isdigit() else None
                is_our = (uid in our_ids) if uid else False
                is_bot = str(m.get("from") or "").lower().endswith("bot")
                res["messages"] += 1
                if is_bot:
                    continue
                if is_our:
                    if not last["our"] or date > last["our"]:
                        last["our"], last["our_name"] = date, m.get("from")
                else:
                    if not last["client"] or date > last["client"]:
                        last["client"], last["client_name"], last["client_text"] = date, m.get("from"), (txt or "")[:300]
            # пишем как UTC-время (экспорт без таймзоны — считаем локальным МСК → в UTC)
            def _iso(d):
                if not d:
                    return None
                try:
                    t = _dt.datetime.fromisoformat(d)
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=3)))
                    return t.astimezone(_dt.timezone.utc).isoformat(timespec="seconds")
                except (TypeError, ValueError):
                    return None
            self.db.conn.execute("INSERT OR IGNORE INTO chat_activity(chat_id) VALUES(?)", (cid,))
            self.db.conn.execute(
                "UPDATE chat_activity SET last_client_at=COALESCE(?,last_client_at), "
                "last_client_name=COALESCE(?,last_client_name), last_client_text=COALESCE(?,last_client_text), "
                "last_our_at=COALESCE(?,last_our_at), last_our_name=COALESCE(?,last_our_name) WHERE chat_id=?",
                (_iso(last["client"]), last["client_name"], last["client_text"],
                 _iso(last["our"]), last["our_name"], cid))
            self.db.conn.commit()
            res["matched"] += 1
            res["details"].append({"chat": ch.get("name"), "status": "ок",
                                   "last_client": _iso(last["client"]), "last_our": _iso(last["our"])})
        self._audit("import_history", "чатов: {}".format(res["matched"]),
                    "сообщений разобрано: {}".format(res["messages"]))
        return res

    @safe
    def seed_dialogs_from_history(self):
        """Проинициализировать «последний контакт» из истории наших отправок (без переписки)."""
        self._require_write()
        n = self.db.seed_activity_from_sendlog()
        return {"seeded": n}

    @safe
    def dialogs(self):
        """Вкладка «Ждут ответа»: клиенты, которым мы не ответили, и забытые чаты."""
        waiting, silent, hidden = self._chat_dialogs()
        has_data = bool(self.db.list_chat_activity())
        return {"waiting": waiting, "silent": silent, "hidden": hidden, "has_data": has_data,
                "warn_h": self.WAIT_WARN_HOURS, "crit_h": self.WAIT_CRIT_HOURS,
                "silent_days": self.SILENT_DAYS,
                "counts": {"waiting": len(waiting),
                           "overdue": sum(1 for w in waiting if (w.get("wait_h") or 0) >= self.WAIT_CRIT_HOURS),
                           "silent": len(silent), "hidden": len(hidden)}}

    @safe
    def dialog_dismiss(self, chat_id):
        """«Ответ не нужен»: убрать чат из ожидающих (клиент написал «Отлично» — и всё).

        Снимаем именно текущее сообщение клиента: напишет новое — чат вернётся сам.
        Работодателю тоже можно: он ведёт этот список наравне со специалистами."""
        self._require_chat_visible(chat_id)
        a = next((x for x in self.db.list_chat_activity() if x["chat_id"] == int(chat_id)), None)
        mark = (a["last_client_at"] if a else None)
        if not mark:
            raise RuntimeError("В этом чате нет сообщений клиента — снимать нечего")
        self.db.dismiss_chat_wait(chat_id, mark, self.user["id"] if self.user else None)
        self._audit("dialog", self._chat_title(chat_id), "снят с ожидания ответа")
        return {"chat_id": int(chat_id), "off_at": mark}

    @safe
    def dialog_restore(self, chat_id):
        """Вернуть чат в «Ждут ответа» (отмена «ответ не нужен»). Работодателю тоже можно."""
        self._require_chat_visible(chat_id)
        self.db.restore_chat_wait(chat_id)
        self._audit("dialog", self._chat_title(chat_id), "возвращён в ожидающие ответа")
        return {"chat_id": int(chat_id)}

    def _dialog_alerts(self):
        """Уведомления владельцам: «клиент ждёт ответа больше суток» (раз в день на чат)."""
        act = {a["chat_id"]: a for a in self.db.list_chat_activity()}
        if not act:
            return 0
        bind = {b["chat_id"]: b["login"] for b in self.db.list_bindings("all")}
        clients = {c["login"]: c for c in self.db.list_clients("all")}
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        n = 0
        for cid, a in act.items():
            lc, lo = a["last_client_at"], a["last_our_at"]
            if not lc or (lo and lo >= lc):
                continue
            off = (a["wait_off_at"] if "wait_off_at" in a.keys() else None)
            if off and off >= lc:
                continue          # сняли вручную («ответ не нужен») — не напоминаем
            try:
                t = dt.datetime.fromisoformat(lc)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=dt.timezone.utc)
            except (TypeError, ValueError):
                continue
            hours = (now - t).total_seconds() / 3600
            if hours < self.WAIT_CRIT_HOURS:
                continue
            login = bind.get(cid)
            c = clients.get(login) if login else None
            owner = (c["owner"] if (c and "owner" in c.keys()) else None)
            if not owner:
                continue
            title = (c["name"] if c else None) or self._chat_title(cid)
            self._notify(owner, "chat", "Клиент ждёт ответа: " + str(title),
                         "без ответа {} ч — «{}»".format(int(hours), (a["last_client_text"] or "")[:80]),
                         "dialogs", dedup=True)
            n += 1
        return n

    # ---------- центр уведомлений (колокольчик) ----------
    @safe
    def notifications(self, limit=40):
        """Уведомления текущего пользователя + счётчик непрочитанных."""
        if not self.user:
            return {"items": [], "unread": 0}
        uid = self.user["id"]
        items = [{"id": n["id"], "kind": n["kind"], "title": n["title"], "text": n["text"],
                  "link": n["link"], "created_at": n["created_at"], "read": bool(n["is_read"])}
                 for n in self.db.notifications_for(uid, limit)]
        return {"items": items, "unread": self.db.unread_count(uid)}

    @safe
    def notif_read(self, notif_id=None):
        """Отметить одно уведомление прочитанным (или все, если id не задан)."""
        if not self.user:
            return {"unread": 0}
        if notif_id in (None, "", 0, "0"):
            self.db.mark_all_notif_read(self.user["id"])
        else:
            self.db.mark_notif_read(int(notif_id), self.user["id"])
        return {"unread": self.db.unread_count(self.user["id"])}

    # ---------- бэкапы базы (админ) ----------
    def _backup_dir(self):
        import os
        from .settings import BASE_DIR
        d = os.path.join(BASE_DIR, "backups")
        os.makedirs(d, exist_ok=True)
        return d

    def _make_backup(self, keep=14, name=None):
        """Целостный бэкап БД + ротация. Возвращает имя файла.

        name задаётся, когда копия делается не «по расписанию»: имя по минутам иначе
        совпадёт с уже существующим файлом и затрёт его."""
        import os
        import datetime as dt
        self.db.wal_checkpoint()
        d = self._backup_dir()
        name = name or "iigbot_{}.sqlite3".format(dt.datetime.now().strftime("%Y%m%d_%H%M"))
        self.db.backup_to(os.path.join(d, name))
        files = sorted(f for f in os.listdir(d) if f.startswith("iigbot_") and f.endswith(".sqlite3"))
        for old in files[:-int(keep)] if len(files) > keep else []:
            try:
                os.remove(os.path.join(d, old))
            except OSError:
                pass
        return name

    @safe
    def backup_now(self):
        """Сделать бэкап прямо сейчас (админ)."""
        self._require_admin()
        import os
        name = self._make_backup()
        size = os.path.getsize(os.path.join(self._backup_dir(), name))
        self.db.set_kv("backup_last", str(__import__("time").time()))
        self._audit("backup", name, "ручной бэкап, {} КБ".format(round(size / 1024)))
        return {"file": name, "size_kb": round(size / 1024)}

    @safe
    def backup_list(self):
        """Список бэкапов (админ): что есть, когда, сколько весит."""
        self._require_admin()
        import os
        import datetime as dt
        d = self._backup_dir()
        out = []
        for f in sorted(os.listdir(d), reverse=True):
            if not (f.startswith("iigbot_") and f.endswith(".sqlite3")):
                continue
            p = os.path.join(d, f)
            out.append({"file": f, "size_kb": round(os.path.getsize(p) / 1024),
                        "created": dt.datetime.fromtimestamp(os.path.getmtime(p)).isoformat(timespec="seconds")})
        return {"items": out[:30], "last": self.db.get_kv("backup_last")}

    # ═══════════ шаблоны кампаний ═══════════
    # Стандарт агентства, описанный один раз: стратегия, минус-слова, UTM, атрибуция,
    # корректировки. Дальше кампания на новом проекте создаётся по нему одним нажатием,
    # одинаково у всех специалистов. Всё клиент-специфичное (счётчик, цели, название)
    # подставляется в момент применения — в самом шаблоне этого нет.

    def _preset_visible(self, row):
        """Общий шаблон виден всем, личный — автору и админу."""
        if not row:
            return False
        if self._is_admin() or row.get("owner") is None:
            return True
        return row.get("owner") == (self.user or {}).get("id")

    def _preset_editable(self, row):
        """Править общий шаблон может только админ: он один на всё агентство."""
        if self._is_admin():
            return True
        return bool(row) and row.get("owner") == (self.user or {}).get("id")

    def _preset_load(self, preset_id, need_edit=False):
        row = self.db.preset_get(int(preset_id))
        if not row or not self._preset_visible(row):
            raise RuntimeError("Шаблон не найден")
        if need_edit and not self._preset_editable(row):
            raise RuntimeError("Общий шаблон агентства правит только администратор")
        try:
            row["preset"] = json.loads(row["data"] or "{}")
        except (ValueError, TypeError):
            row["preset"] = {}
        return row

    def _client_goal_ids(self, login):
        """Цели клиента, отмеченные «в отчёт», — их и делаем приоритетными в кампании."""
        c = dict(self.db.get_client(login) or {})   # sqlite отдаёт Row, а не словарь
        try:
            items = json.loads(c.get("goals") or "[]")
        except (ValueError, TypeError):
            items = []
        out = []
        for g in items:
            if isinstance(g, dict) and g.get("active") is not False and g.get("id"):
                out.append(str(g["id"]))
            elif not isinstance(g, dict) and g:
                out.append(str(g))
        return out

    def _client_goal_names(self, login):
        c = dict(self.db.get_client(login) or {})
        try:
            items = json.loads(c.get("goals") or "[]")
        except (ValueError, TypeError):
            items = []
        return [(g.get("name") or ("Цель " + str(g.get("id")))) for g in items
                if isinstance(g, dict) and g.get("active") is not False]

    LABELS_KEY = "preset_labels"

    def _preset_labels(self):
        """Свои подписи настроек, заданные в кабинете."""
        try:
            raw = self.db.get_kv(self.LABELS_KEY)
            return json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001
            return {}

    @safe
    def preset_spec(self):
        """Справочники для формы шаблона: стратегии, настройки, корректировки, атрибуция.

        Отдаём с сервера, а не держим списками в разметке: иначе они разъедутся при
        первом же изменении в Директе."""
        from . import presets as P
        return {"spec": P.spec(self._preset_labels()), "blank": P.blank(),
                "standard": P.agency_standard()}

    @safe
    def preset_label_set(self, code, text):
        """Подписать настройку так, как она названа в интерфейсе Директа.

        Часть настроек справка Яндекса дословно не называет — кабинет показывает описание
        из справочника API и помечает это. Кто видит интерфейс, подписывает точно, и
        подпись становится общей для всех.
        """
        self._require_admin()
        labels = self._preset_labels()
        code = str(code)[:64]
        txt = (text or "").strip()[:160]
        if txt:
            labels[code] = txt
        else:
            labels.pop(code, None)
        self.db.set_kv(self.LABELS_KEY, json.dumps(labels, ensure_ascii=False))
        self._audit("preset_label", code, "подпись настройки: " + (txt or "сброшена"))
        return {"ok": True, "labels": labels}

    @safe
    def presets_list(self):
        """Шаблоны, доступные текущему пользователю."""
        rows = self.db.preset_list(owner=(self.user or {}).get("id"),
                                   all_visible=self._is_admin() or self.user is None)
        out = []
        for r in rows:
            try:
                data = json.loads(r["data"] or "{}")
            except (ValueError, TypeError):
                data = {}
            out.append({"id": r["id"], "name": r["name"], "note": r["note"],
                        "shared": r["owner"] is None, "owner": r["owner"],
                        "can_edit": self._preset_editable(r),
                        "used_count": r.get("used_count") or 0, "last_used": r.get("last_used"),
                        "updated_at": r.get("updated_at"),
                        "modifiers": len(data.get("modifiers") or []),
                        "negatives": len(data.get("negative_keywords") or [])})
        return out

    @safe
    def preset_get(self, preset_id):
        """Шаблон целиком — для формы правки."""
        row = self._preset_load(preset_id)
        return {"id": row["id"], "name": row["name"], "note": row["note"],
                "shared": row["owner"] is None, "can_edit": self._preset_editable(row),
                "data": row["preset"]}

    @safe
    def preset_save(self, preset_id, name, note, data, shared=None):
        """Создать или обновить шаблон. Проверяем до записи — чтобы кривой шаблон
        не дожил до момента применения на живом аккаунте."""
        self._require_write()
        from . import presets as P
        nm = (name or "").strip()
        if not nm:
            raise RuntimeError("У шаблона должно быть название")
        if not isinstance(data, dict):
            raise RuntimeError("Шаблон повреждён")
        bad = P.validate(data)
        if bad:
            raise RuntimeError("Так шаблон не сохранить: " + "; ".join(bad))
        owner = (self.user or {}).get("id")
        if preset_id:
            row = self._preset_load(preset_id, need_edit=True)
            new_id = self.db.preset_save(row["id"], nm, note, json.dumps(data, ensure_ascii=False))
            self._audit("preset_save", nm, "правка шаблона")
        else:
            # общий шаблон агентства заводит только админ, остальные — личный
            if shared and not self._is_admin():
                raise RuntimeError("Общий шаблон может завести только администратор")
            new_id = self.db.preset_save(None, nm, note, json.dumps(data, ensure_ascii=False),
                                         owner=None if shared else owner)
            self._audit("preset_save", nm, "новый шаблон")
        return {"id": new_id}

    @safe
    def preset_delete(self, preset_id):
        row = self._preset_load(preset_id, need_edit=True)
        self.db.preset_delete(row["id"])
        self._audit("preset_delete", row["name"], "шаблон удалён")
        return {"ok": True}

    @safe
    def client_campaigns(self, login):
        """Кампании клиента: выбрать эталон для слепка или просто посмотреть, что есть."""
        self._require_owned(login)
        token = load_secrets()["yandex_oauth_token"]
        from . import yandex
        camps = yandex.campaigns_brief(token, login)
        return [{"id": c.get("Id"), "name": c.get("Name"), "type": c.get("Type"),
                 "state": c.get("State"), "status": c.get("Status")} for c in camps]

    @safe
    def preset_from_campaign(self, login, campaign_id):
        """Снять шаблон с существующей кампании. Самый короткий путь: специалист один раз
        собрал эталон руками — забираем оттуда всё, что переносится на другой аккаунт."""
        self._require_owned(login)
        from . import yandex, presets as P
        token = load_secrets()["yandex_oauth_token"]
        full = yandex.campaign_full(token, login, campaign_id)
        try:
            mods = yandex.bidmodifiers_for(token, login, [campaign_id])
        except Exception:  # noqa: BLE001 — без корректировок слепок всё равно полезен
            mods = []
        data = P.from_campaign(full, mods)
        return {"data": data, "source": full.get("Name"), "modifiers_found": len(mods),
                "describe": [{"k": k, "v": v} for k, v in P.describe(data)]}

    @safe
    def preset_preview(self, preset_id, login, custom_name=None):
        """Что именно появится на аккаунте. Показываем до записи — и только после
        подтверждения человеком что-то создаётся."""
        self._require_owned(login)
        from . import yandex, presets as P
        row = self._preset_load(preset_id)
        data = row["preset"]
        client = dict(self.db.get_client(login) or {})
        token = load_secrets()["yandex_oauth_token"]
        counters = []
        counter_err = None
        try:
            counters = yandex.get_campaign_counters(token, login)
        except Exception as e:  # noqa: BLE001 — счётчик не обязателен, но причину скажем
            counter_err = str(e)[:200]
        goals = self._client_goal_names(login)
        lines = P.describe(data, client.get("name") or login, counters=counters, goals=goals)
        warn = []
        if not counters and data.get("use_client_counter", True):
            warn.append("У клиента не нашли счётчик Метрики" +
                        (": " + counter_err if counter_err else
                         " — кампания создастся без него, привяжешь вручную"))
        if not goals and data.get("use_client_goals", True):
            warn.append("У клиента не отмечены цели — кампания создастся без приоритетных целей")
        warn.extend(P.validate(data))
        return {"name": P.campaign_name(data, client.get("name") or login, custom_name),
                "lines": [{"k": k, "v": v} for k, v in lines],
                "warnings": warn, "preset": row["name"], "login": login,
                "client": client.get("name") or login,
                "modifiers": len(data.get("modifiers") or [])}

    @safe
    def preset_apply(self, preset_id, login, custom_name=None):
        """Создать кампанию по шаблону. Фоном: два обращения к Директу, а процесс один.

        Кампания создаётся без групп и объявлений — Директ держит такую черновиком:
        показов нет, деньги не тратятся, пока специалист её не наполнит.
        """
        self._require_write()
        self._require_owned(login)
        row = self._preset_load(preset_id)
        from . import presets as P
        bad = P.validate(row["preset"])
        if bad:
            raise RuntimeError("Шаблон надо поправить: " + "; ".join(bad))
        return self._job_start("preset_apply", "Создаю кампанию по шаблону",
                               lambda say: self._preset_apply_run(row, login, custom_name, say))

    def _preset_apply_run(self, row, login, custom_name=None, say=None):
        from . import yandex, presets as P
        token = load_secrets()["yandex_oauth_token"]
        data = row["preset"]
        client = dict(self.db.get_client(login) or {})
        name = P.campaign_name(data, client.get("name") or login, custom_name)

        if say:
            say("собираю настройки…")
        counters = []
        if data.get("use_client_counter", True):
            try:
                counters = yandex.get_campaign_counters(token, login)
            except Exception:  # noqa: BLE001 — без счётчика кампания всё равно создастся
                counters = []
        goal_ids = self._client_goal_ids(login) if data.get("use_client_goals", True) else []
        payload = P.to_payload(data, client.get("name") or login, counters=counters,
                               goal_ids=goal_ids, custom_name=custom_name)

        if say:
            say("создаю кампанию…")
        try:
            res = yandex.campaigns_add(token, login, [payload])
        except Exception as e:  # noqa: BLE001 — в журнал попадает и неудача
            self.db.preset_run_log(row["id"], row["name"], login, None, name,
                                   (self.user or {}).get("id"), False, str(e)[:300])
            raise
        first = (res or [{}])[0]
        if first.get("Errors"):
            msg = "; ".join("{} ({})".format(e.get("Message"), e.get("Details"))
                            for e in first["Errors"])[:400]
            self.db.preset_run_log(row["id"], row["name"], login, None, name,
                                   (self.user or {}).get("id"), False, msg)
            raise RuntimeError("Директ не принял кампанию: " + msg)
        camp_id = first.get("Id")
        warnings = ["{}".format(w.get("Message")) for w in (first.get("Warnings") or [])]

        mods_done, mods_failed = 0, []
        payload_mods = P.modifiers_payload(data, camp_id)
        if payload_mods:
            if say:
                say("вешаю корректировки…")
            try:
                mres = yandex.bidmodifiers_add(token, login, payload_mods)
                for m in (mres or []):
                    if m.get("Errors"):
                        mods_failed.append("; ".join(e.get("Message", "") for e in m["Errors"]))
                    else:
                        mods_done += len(m.get("Ids") or ([m["Id"]] if m.get("Id") else []))
            except Exception as e:  # noqa: BLE001 — кампания уже создана, это отдельная беда
                mods_failed.append(str(e)[:200])

        self.db.preset_used(row["id"])
        self.db.preset_run_log(row["id"], row["name"], login, camp_id, name,
                               (self.user or {}).get("id"), True, None)
        self._audit("preset_apply", login,
                    "создана кампания {} ({}) по шаблону «{}»".format(name, camp_id, row["name"]))
        return {"campaign_id": camp_id, "name": name, "modifiers": mods_done,
                "modifiers_failed": mods_failed, "warnings": warnings,
                "counters": counters, "goals": len(goal_ids), "login": login}

    @safe
    def preset_runs(self, limit=50):
        """Что создавали по шаблонам: свои проекты — специалисту, всё — админу."""
        logins = None if (self._is_admin() or self.user is None) else sorted(self._owned_set())
        rows = self.db.preset_runs(int(limit or 50), logins=logins)
        names = {u["id"]: (u["name"] or u["email"]) for u in self.db.list_users()}
        clients = {c["login"]: c["name"] for c in self.db.list_clients("all")}
        return [{"id": r["id"], "preset": r["preset_name"], "login": r["login"],
                 "client": clients.get(r["login"]) or r["login"],
                 "campaign_id": r["campaign_id"], "campaign": r["campaign"],
                 "who": names.get(r["by_user"], "—"), "at": r["at"],
                 "ok": bool(r["ok"]), "error": r["error"]} for r in rows]

    @safe
    def preset_undo(self, run_id):
        """Удалить кампанию, созданную по шаблону. Директ разрешает удалять только те,
        по которым не было открутки, — то есть ровно наш случай промаха."""
        self._require_write()
        rows = [r for r in self.db.preset_runs(200) if r["id"] == int(run_id)]
        if not rows:
            raise RuntimeError("Запись не найдена")
        run = rows[0]
        if not run.get("campaign_id"):
            raise RuntimeError("В этой записи кампания не создавалась")
        self._require_owned(run["login"])
        from . import yandex
        token = load_secrets()["yandex_oauth_token"]
        res = yandex.campaigns_delete(token, run["login"], [run["campaign_id"]])
        first = (res or [{}])[0]
        if first.get("Errors"):
            msg = "; ".join(e.get("Message", "") for e in first["Errors"])
            raise RuntimeError("Директ не дал удалить: " + msg[:200])
        self.db.preset_run_log(run.get("preset_id"), run.get("preset_name"), run["login"],
                               run["campaign_id"], "удалена: " + (run.get("campaign") or ""),
                               (self.user or {}).get("id"), True, None)
        self._audit("preset_undo", run["login"],
                    "удалена кампания {} ({})".format(run.get("campaign"), run["campaign_id"]))
        return {"deleted": run["campaign_id"]}

    # ═══════════ система: состояние машины и обслуживание (только админ) ═══════════
    # Всё это раньше делалось по SSH: посмотреть, жив ли процесс, цела ли база,
    # какой код сейчас на сервере, откуда откатываться. Теперь — из кабинета.

    def _public_url(self):
        """Адрес, по которому кабинет виден снаружи. Записывается при первом заходе."""
        return (self.db.get_kv("public_url") or "https://reports.iig.ru").rstrip("/")

    def _personal_chat(self):
        """Личный чат текущего пользователя с ботом — туда уходят копии базы."""
        from . import budgets as B
        by_id, by_un, by_title = B._priv_index(self.db)
        u = self.db.get_user((self.user or {}).get("id")) if self.user else None
        chat = B._resolve_chat(u, by_id, by_un, by_title)
        if not chat:
            raise RuntimeError("Личный чат с ботом не привязан. Настройки → «Мой чат для тревог» → "
                               "«Привязать», и повторите.")
        return chat["chat_id"]

    @safe
    def sys_status(self):
        """Одним запросом всё о машине: версия, аптайм, база, место, задачи, проверки."""
        self._require_admin()
        from . import sysinfo
        db = self.db

        def kv_json(key):
            try:
                raw = db.get_kv(key)
                return json.loads(raw) if raw else None
            except Exception:  # noqa: BLE001 — сохранённого результата может не быть
                return None

        jobs = [{"key": j["key"], "title": j.get("title"), "state": j.get("state"),
                 "note": j.get("note"), "error": (j.get("error") or "")[:200],
                 "started": j.get("started"), "finished": j.get("finished")}
                for j in db.jobs_recent(8)]
        p = sysinfo.paths()
        return {"version": sysinfo.version(), "cpu": sysinfo.cpu_now(),
                "db": sysinfo.db_stats(db), "disk": sysinfo.storage_stats(),
                "cache": db.cache_stats(), "jobs": jobs,
                "backup_last": db.get_kv("backup_last"),
                "backup_sent": db.get_kv("backup_sent_last"),
                "integrity": kv_json("integrity_last"),
                "watch": kv_json("watch_last"),
                "public_url": self._public_url(),
                "python": p["python_version"], "paths": p,
                "users": len(db.list_users()),
                "clients": len(db.list_clients("all")),
                "cron": {
                    "autosync": "0 5 * * *  {} -m iigbot autosync".format(p["python"]),
                    "weekly": "0 10 * * 1 {} -m iigbot weekly".format(p["python"]),
                    "watch": "*/5 * * * * {} -m iigbot watch".format(p["python"]),
                }}

    @safe
    def sys_integrity(self):
        """Полная проверка базы. Читает файл целиком — поэтому в фоне."""
        self._require_admin()
        return self._job_start("integrity", "Проверяю целостность базы",
                               lambda say: self._integrity_run(say))

    def _integrity_run(self, say=None):
        from . import sysinfo
        if say:
            say("читаю базу…")
        res = sysinfo.integrity(self.db)
        self.db.set_kv("integrity_last", json.dumps(res, ensure_ascii=False))
        if not res.get("ok"):
            log_error("integrity", "проверка базы не прошла: {}".format(res.get("full"))[:400])
        return res

    @safe
    def sys_maintenance(self):
        """ANALYZE + VACUUM: пересчитать статистику и сжать файл базы."""
        self._require_admin()
        res = self.db.maintenance()
        self._audit("maintenance", "db", "было {} КБ, стало {} КБ".format(
            res.get("before_kb"), res.get("after_kb")))
        return res

    @safe
    def sys_cpu(self):
        """Расход процессора: по дням и по методам. На виртуальном хостинге это тот ресурс,
        из-за которого гасят процесс, — полезно знать, кто его ест."""
        self._require_admin()
        from . import sysinfo
        import datetime as _d
        today = _d.date.today().isoformat()
        return {"days": self.db.cpu_days(14), "top_today": self.db.cpu_top(today, 12),
                "top_all": self.db.cpu_top(None, 12), "today": today,
                "process": sysinfo.cpu_now()}

    @safe
    def sys_secrets(self):
        """Какие ключи есть, когда менялись, не пора ли обновить. Значения не отдаём."""
        self._require_admin()
        from . import sysinfo
        return sysinfo.secrets_report(self.db)

    @safe
    def sys_secret_rotated(self, name):
        """Отметить, что ключ обновлён вручную: возраст считается от этой даты."""
        self._require_admin()
        import time as _t
        self.db.set_kv("secret_rotated_" + str(name)[:40], str(_t.time()))
        self._audit("secret_rotated", str(name)[:40], "отмечено обновление ключа")
        return {"ok": True}

    @safe
    def sys_backup_send(self):
        """Копия базы во внешнее хранилище: файлом в личный чат с ботом.

        Копия рядом с базой не спасает, если у хостинга проблема с диском или аккаунтом,
        — нужна копия вне сервера."""
        self._require_admin()
        chat_id = self._personal_chat()      # проверяем до фона: ошибку видно сразу
        return self._job_start("backup_send", "Отправляю копию базы в Telegram",
                               lambda say: self._backup_send_run(chat_id, say))

    def _backup_send_run(self, chat_id, say=None):
        import os
        import time as _t
        import datetime as _d
        if say:
            say("делаю копию…")
        name = self._make_backup()
        path = os.path.join(self._backup_dir(), name)
        size = os.path.getsize(path)
        if say:
            say("отправляю {} КБ…".format(round(size / 1024)))
        with open(path, "rb") as f:
            data = f.read()
        self._tg_client().send_document(
            chat_id, name, data,
            caption="Копия базы IIG Reporter\n{} КБ, {}".format(
                round(size / 1024), _d.datetime.now().strftime("%d.%m.%Y %H:%M")))
        self.db.set_kv("backup_sent_last", str(_t.time()))
        self._audit("backup_send", name,
                    "копия базы отправлена в Telegram, {} КБ".format(round(size / 1024)))
        return {"file": name, "kb": round(size / 1024)}

    @safe
    def sys_restore(self, file):
        """Восстановить базу из копии. Текущее состояние сначала сохраняем отдельным бэкапом:
        если восстановились не туда, будет куда вернуться."""
        self._require_admin()
        import os
        from . import sysinfo
        fn = os.path.basename(str(file))
        if fn != str(file) or not (fn.startswith("iigbot_") and fn.endswith(".sqlite3")):
            raise RuntimeError("Неверное имя файла копии")
        path = os.path.join(self._backup_dir(), fn)
        if not os.path.isfile(path):
            raise RuntimeError("Копия не найдена")
        # Имя с секундами и пометкой: копия по минутам могла бы совпасть с той,
        # из которой восстанавливаемся, — и затереть её до чтения.
        import datetime as _d
        safety = self._make_backup(name="iigbot_{}_before_restore.sqlite3".format(
            _d.datetime.now().strftime("%Y%m%d_%H%M%S")))
        res = sysinfo.restore(self.db, path)
        self.db.cache_clear()                 # накопленный кэш относится к прежним данным
        report.cache_clear()
        self._audit("restore", fn,
                    "база восстановлена из копии; прежняя сохранена как {}".format(safety))
        res["safety"] = safety
        return res

    @safe
    def sys_deploys(self):
        """Копии файлов приложения, которые кладёт выкат: откуда можно откатиться."""
        self._require_admin()
        from . import sysinfo
        return sysinfo.deploy_backups()

    @safe
    def sys_rollback(self, stamp):
        """Вернуть файлы приложения из выбранной копии и перезапустить приложение."""
        self._require_admin()
        from . import sysinfo
        res = sysinfo.rollback(str(stamp)[:40])
        self._audit("rollback", res["stamp"], "возвращены файлы: {}".format(", ".join(res["files"])))
        res["restart"] = sysinfo.touch_restart()
        return res

    @safe
    def sys_restart(self):
        """Перезапустить приложение (Passenger следит за tmp/restart.txt)."""
        self._require_admin()
        from . import sysinfo
        res = sysinfo.touch_restart()
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "не удалось перезапустить")
        self._audit("restart", "app", "перезапуск из кабинета")
        return res

    @safe
    def sys_ping(self):
        """Проверка снаружи: запрос уходит отдельным потоком.

        Синхронно так нельзя. Процесс приложения на хостинге один, и запрос к самому себе
        изнутри обработчика ждал бы освобождения этого же процесса — то есть себя. Поток
        отпускает текущий запрос, и проверка доходит уже до свободного процесса.
        """
        self._require_admin()
        import threading
        db = self.db

        def run():
            from . import sysinfo
            try:
                sysinfo.watch_once(db, tg=None)   # тревоги в Telegram шлёт cron, не кнопка
            except Exception as e:  # noqa: BLE001
                log_error("watch", str(e))

        threading.Thread(target=run, daemon=True).start()
        return {"started": True}

    @safe
    def sys_watch(self):
        """Последний результат проверки живости — и ручной, и ночной из cron."""
        self._require_admin()
        raw = self.db.get_kv("watch_last")
        try:
            return json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001
            return {}

    @safe
    def sys_runbook(self):
        """Справка для преемника с подставленными путями этой машины."""
        self._require_admin()
        from . import sysinfo
        return {"text": sysinfo.runbook(self.db)}

    # ---------- переотправка отчёта ----------
    @safe
    def resend_report(self, login):
        """Отправить недельный отчёт по клиенту заново (кнопка в Истории)."""
        self._require_write()
        self._require_owned(login)
        token = load_secrets()["yandex_oauth_token"]
        intro, note, attr = self._report_ctx()
        res = report.send_for_login(token, self._tg_client(), self.db, login, intro, note, attr)
        self._audit("resend", login, "статус: {}".format(res.get("status")))
        return res

    # ---------- аудит действий (кто что сделал) ----------
    @safe
    def audit_log(self, limit=300, action=None, user_id=None):
        """Журнал действий для работодателя/админа: привязки, назначения, долги, сотрудники."""
        self._require_supervisor()
        rows = [dict(r) for r in self.db.list_audit(limit=limit, action=action, user_id=user_id)]
        users = [{"id": u["id"], "name": u["name"] or u["email"]} for u in self.db.list_users()]
        return {"entries": rows, "users": users}

    # ---------- журнал ошибок (админ) ----------
    @safe
    def error_log(self, lines=300):
        """Хвост файлового журнала iig_errors.log для раздела «Журнал»: новые сверху,
        каждая запись {ts, where, msg}. Только админ."""
        self._require_admin()
        import os
        from .settings import ERROR_LOG_PATH
        if not os.path.isfile(ERROR_LOG_PATH):
            return {"entries": []}
        with open(ERROR_LOG_PATH, encoding="utf-8", errors="replace") as f:
            raw = f.readlines()
        out = []
        for ln in raw[-int(lines or 300):]:
            parts = ln.rstrip("\n").split("\t", 2)
            if len(parts) == 3:
                out.append({"ts": parts[0], "where": parts[1], "msg": parts[2]})
            elif ln.strip():
                out.append({"ts": "", "where": "", "msg": ln.strip()})
        out.reverse()   # свежие сверху
        return {"entries": out}

    @safe
    def error_log_clear(self):
        """Очищает журнал ошибок (админ)."""
        self._require_admin()
        import os
        from .settings import ERROR_LOG_PATH
        if os.path.isfile(ERROR_LOG_PATH):
            open(ERROR_LOG_PATH, "w", encoding="utf-8").close()
        return {"cleared": True}

    # ---------- своя приписка к отчётам ----------
    @safe
    def my_note(self):
        """Своя приписка пользователя: note=null — общая (из Настроек), '' — без приписки,
        текст — своя. global_note — что сейчас в общих Настройках (для подсказки в UI)."""
        g_note = load_report_config().get("specialist_note") or ""
        if not self.user:
            return {"note": None, "global_note": g_note}
        u = self.db.get_user(self.user["id"])
        n = (u["note"] if (u is not None and "note" in u.keys()) else None)
        return {"note": n, "global_note": g_note}

    @safe
    def set_my_note(self, note):
        """Сохранить свою приписку (null=общая, ''=без, текст=своя). Подставляется во все отчёты
        по клиентам, которыми владеет пользователь — включая недельный cron."""
        if not self.user:
            raise RuntimeError("Доступно только в веб-кабинете")
        self._require_write()
        if note is not None:
            note = str(note).strip() or ""
        self.db.set_user_note(self.user["id"], note)
        return {"saved": True, "note": note}

    @safe
    def my_alert(self):
        """Куда мне идут бюджет-алерты: linked=True — привязана личка по deep-link (надёжно, работает
        и без публичного @username). scope='all' — получаю по ВСЕМУ агентству (для работодателя/админа),
        иначе только по своим клиентам. can_all — доступен ли режим «все» этой роли."""
        if not self.user:
            return {"alert_username": None, "linked": False, "scope": "mine", "can_all": False}
        u = self.db.get_user(self.user["id"])
        gk = lambda k: (u[k] if (u is not None and k in u.keys()) else None)  # noqa: E731
        return {"alert_username": gk("alert_username"),
                "linked": bool(gk("alert_chat_id")),
                "scope": ("all" if gk("alert_scope") == "all" else "mine"),
                "can_all": (self.user.get("role") in ("admin", "observer"))}

    @safe
    def set_my_alert(self, username):
        """Задать свой @username для алертов (пусто = не получать персонально). Наблюдателю можно."""
        if not self.user:
            raise RuntimeError("Доступно только в кабинете")
        username = (username or "").strip().lstrip("@").lower() or None
        self.db.set_user_alert(self.user["id"], username)
        return {"saved": True, "alert_username": username}

    @safe
    def set_my_alert_scope(self, scope):
        """Охват алертов: 'all' — по всем клиентам агентства (админ/наблюдатель), иначе только свои."""
        if not self.user:
            raise RuntimeError("Доступно только в кабинете")
        if scope == "all" and self.user.get("role") not in ("admin", "observer"):
            raise RuntimeError("Режим «по всему агентству» доступен админу и наблюдателю")
        self.db.set_user_alert_scope(self.user["id"], scope)
        return {"scope": ("all" if scope == "all" else "mine")}

    @safe
    def alert_link(self):
        """Одноразовая deep-link для НАДЁЖНОЙ привязки лички к бюджет-алертам: пользователь
        открывает ссылку и жмёт Start — бот сохраняет его chat_id напрямую (без @username).
        Доступно всем ролям, включая наблюдателя (он тоже хочет алерты)."""
        if not self.user:
            raise RuntimeError("Доступно только в кабинете")
        import os
        token = os.urandom(6).hex()
        self.db.set_kv("alerttok_" + token, str(self.user["id"]))
        return {"link": "https://t.me/{}?start=alert_{}".format(self._bot_name(), token)}

    @safe
    def alert_unlink(self):
        """Отвязать личку от алертов."""
        if not self.user:
            raise RuntimeError("Доступно только в кабинете")
        self.db.set_user_alert_chat(self.user["id"], None)
        return {"linked": False}

    # ---------- бюджеты ----------
    @safe
    def budgets(self, scope=None):
        """Вкладка «Бюджеты». scope='all' — по всему агентству (админ/наблюдатель: у админа может
        не быть своих проектов, но деньги агентства видеть нужно), иначе — свои клиенты."""
        can_all = (not self.user) or self.user.get("role") in ("admin", "observer")
        all_mode = (scope == "all" and can_all) or (self._owner() == "all")
        if all_mode:
            rows = [dict(r) for r in self.db.list_budgets()]
        else:
            visible = {c["login"] for c in self.db.list_clients(self._owner())}
            rows = [dict(r) for r in self.db.list_budgets() if r["login"] in visible]
        return {"rows": rows, "updated": self.db.get_kv("budgets_updated"),
                "scope": ("all" if all_mode else "mine"), "can_all": can_all,
                "run": {k: _BUDGET_RUN[k] for k in ("running", "done", "total", "error")}}

    @safe
    def budgets_refresh(self):
        """Принудительный сбор бюджетов в фоне по МОИМ клиентам (специалист/админ — свои,
        наблюдатель — все). Полный агентский сбор делает 12-часовой авто-планировщик."""
        self._require_write()
        if _BUDGET_RUN["running"]:
            return {"already_running": True}
        # скоуп ручного сбора: видимые мне клиенты (не всё агентство)
        scope = None
        if self._owner() != "all":
            scope = sorted({c["login"] for c in self.db.list_clients(self._owner())})
            if not scope:
                return {"started": False, "reason": "У вас нет клиентов для сбора."}
        _BUDGET_RUN.update({"running": True, "done": 0, "total": 0, "error": None, "summary": None})
        import threading
        threading.Thread(target=self._budgets_worker, args=(scope,), daemon=True).start()
        return {"started": True}

    def _budgets_worker(self, logins=None):
        from . import budgets as B
        try:
            token = load_secrets()["yandex_oauth_token"]
            tg = None
            try:
                tg = self._tg_client()
            except Exception:  # noqa: BLE001 — нет бота: собираем без алертов
                tg = None

            def prog(done, total, detail):
                _BUDGET_RUN["done"], _BUDGET_RUN["total"] = done, total

            res = B.collect_and_alert(self.db, token, tg=tg, on_progress=prog, logins=logins)
            _BUDGET_RUN["summary"] = res
            self.db.set_kv("budgets_last", str(__import__("time").time()))
        except Exception as e:  # noqa: BLE001
            _BUDGET_RUN["error"] = str(e)
            log_error("budgets", e)
        finally:
            _BUDGET_RUN["running"] = False

    # ---------- НАБЛЮДАТЕЛЬ: контроль обязательств + сообщения ----------
    @safe
    def supervision(self):
        """Контроль: по каждому сотруднику — покрытие недельной рассылки (все ли его привязанные
        клиенты получили отчёт на этой неделе, с понедельника). Наблюдатель/админ."""
        self._require_supervisor()
        from datetime import date, timedelta
        today = date.today()
        mon = today - timedelta(days=today.weekday())
        prev_mon = mon - timedelta(days=7)
        def iso(d):
            return d.isoformat() + "T00:00:00"
        sent_this = self.db.sent_logins_between(iso(mon), iso(today + timedelta(days=1)))
        sent_mon = self.db.sent_logins_between(iso(mon), iso(mon + timedelta(days=1)))  # именно в Пн
        sent_prev = self.db.sent_logins_between(iso(prev_mon), iso(mon))
        skip_this = self.db.status_logins_between("skipped", iso(mon), iso(today + timedelta(days=1)))
        excused = self.db.excused_logins(mon.isoformat())   # {login: {kind,reason,ongoing,id}}
        clients = self.db.list_clients("all")
        owner_of = {c["login"]: (c["owner"] if "owner" in c.keys() else None) for c in clients}
        names = {c["login"]: c["name"] for c in clients}
        bound_by_owner = {}
        for b in self.db.list_bindings("all"):
            bound_by_owner.setdefault(owner_of.get(b["login"]), set()).add(b["login"])
        # сторонние (копипаст) — тоже недельное обязательство: их доставляют вручную, зачёт
        # ставится при сборе отчёта во вкладке «Сторонние» (log_send 'sent').
        for c in clients:
            if ("delivery" in c.keys()) and c["delivery"] == "external":
                bound_by_owner.setdefault(owner_of.get(c["login"]), set()).add(c["login"])
        rows = []
        # Ничьи проекты (owner=NULL) тоже висят обязательством на агентстве, но раньше
        # выпадали из Контроля: строки строились только по пользователям, а корзина None
        # молча терялась. Работодатель видел «долг 1», а кто именно — нигде.
        buckets = [(u, bound_by_owner.get(u["id"], set()))
                   for u in self.db.list_users() if u["role"] != "observer"]
        orphan = bound_by_owner.get(None, set())
        if orphan:
            buckets.append((None, orphan))
        for u, bset in buckets:
            total = len(bset)
            done = bset & sent_this
            exc_items = []   # уважительные: отдельно (авто-скип + закрытые долги)
            for lg in sorted(bset - done):
                if lg in excused:
                    e = excused[lg]
                    exc_items.append({"login": lg, "name": names.get(lg, lg),
                                      "reason": e.get("reason") or ("проект отвалился" if e.get("kind") == "churned" else "уважительно"),
                                      "excuse_id": e.get("id"), "ongoing": e.get("ongoing")})
                elif lg in skip_this:
                    exc_items.append({"login": lg, "name": names.get(lg, lg),
                                      "reason": "нет открута (авто)", "excuse_id": None, "ongoing": False})
            exc_logins = {x["login"] for x in exc_items}
            debt = sorted(bset - done - exc_logins)   # реальные долги
            covered = len(done) + len(exc_logins)      # обязательство выполнено или закрыто
            last = None
            for lg in bset:
                ls = self.db.last_send_at(lg)
                if ls and (last is None or ls > last):
                    last = ls
            status = ("none" if total == 0 else "ok" if not debt
                      else "partial" if (done or exc_logins) else "miss")
            rows.append({
                "user_id": (u["id"] if u else None),
                "name": (u["name"] or u["email"]) if u else "Без владельца",
                "email": (u["email"] if u else ""),
                "role": (u["role"] if u else "unassigned"),
                "active": (bool(u["active"]) if u else True),
                "unassigned": u is None,          # строка «ничьи проекты» — её нужно раздать
                "bound": total, "sent": len(done), "on_monday": len(bset & sent_mon),
                "excused": exc_items, "debt": len(debt),
                "missing": [{"login": m, "name": names.get(m, m)} for m in debt],
                "coverage": (round(100 * covered / total) if total else None),
                "prev_coverage": (round(100 * len(bset & sent_prev) / total) if total else None),
                "last_activity": last, "status": status,
            })
        order = {"miss": 0, "partial": 1, "none": 2, "ok": 3}
        # ничьи проекты — наверх: это не чья-то недоработка, а дыра в раздаче
        rows.sort(key=lambda r: (0 if (r.get("unassigned") and r["debt"]) else 1,
                                 order.get(r["status"], 9), -(r["bound"] or 0)))
        bt = sum(r["bound"] for r in rows)
        st = sum(r["sent"] for r in rows)
        ex = sum(len(r["excused"]) for r in rows)
        dt = sum(r["debt"] for r in rows)
        agency = {"week_from": mon.isoformat(), "week_to": today.isoformat(),
                  "specialists": sum(1 for r in rows if not r.get("unassigned") and (r["bound"] or r["role"] == "admin")),
                  "bound_total": bt, "sent_total": st, "excused_total": ex, "debt_total": dt,
                  "coverage": (round(100 * (st + ex) / bt) if bt else None),
                  "at_risk": sum(1 for r in rows if r["status"] in ("miss", "partial"))}
        return {"agency": agency, "rows": rows}

    # ---------- обязательства: помощники по неделям ----------
    @staticmethod
    def _week_bounds(weeks_back=0):
        """(понедельник, следующий понедельник) недели N назад."""
        from datetime import date, timedelta
        today = date.today()
        mon = today - timedelta(days=today.weekday()) - timedelta(days=7 * weeks_back)
        return mon, mon + timedelta(days=7)

    def _obligations_by_owner(self):
        """{owner_id: set(логинов)} — недельные обязательства: привязанные ∪ сторонние."""
        clients = self.db.list_clients("all")
        owner_of = {c["login"]: (c["owner"] if "owner" in c.keys() else None) for c in clients}
        by_owner = {}
        for b in self.db.list_bindings("all"):
            by_owner.setdefault(owner_of.get(b["login"]), set()).add(b["login"])
        for c in clients:
            if ("delivery" in c.keys()) and c["delivery"] == "external":
                by_owner.setdefault(owner_of.get(c["login"]), set()).add(c["login"])
        return by_owner

    def _coverage_for(self, logins, mon, nxt):
        """Покрытие набора клиентов за неделю [mon, nxt): (covered, total, sent)."""
        def iso(d):
            return d.isoformat() + "T00:00:00"
        if not logins:
            return 0, 0, 0
        sent = self.db.sent_logins_between(iso(mon), iso(nxt)) & logins
        skipped = self.db.status_logins_between("skipped", iso(mon), iso(nxt)) & logins
        excused = set(self.db.excused_logins(mon.isoformat()).keys()) & logins
        covered = sent | skipped | excused
        return len(covered), len(logins), len(sent)

    @safe
    def coverage_history(self, weeks=8):
        """Тренд сдачи по неделям: агентство и каждый специалист (для графиков в Контроле)."""
        self._require_supervisor()
        weeks = max(2, min(int(weeks or 8), 26))
        by_owner = self._obligations_by_owner()
        users = [u for u in self.db.list_users() if u["role"] != "observer"]
        all_logins = set()
        for s in by_owner.values():
            all_logins |= s
        out_weeks, agency, per_user = [], [], {u["id"]: [] for u in users}
        for back in range(weeks - 1, -1, -1):
            mon, nxt = self._week_bounds(back)
            out_weeks.append(mon.isoformat())
            cov, tot, _ = self._coverage_for(all_logins, mon, nxt)
            agency.append(round(100 * cov / tot) if tot else None)
            for u in users:
                lg = by_owner.get(u["id"], set())
                c2, t2, _ = self._coverage_for(lg, mon, nxt)
                per_user[u["id"]].append(round(100 * c2 / t2) if t2 else None)
        return {"weeks": out_weeks, "agency": agency,
                "users": [{"id": u["id"], "name": u["name"] or u["email"],
                           "series": per_user[u["id"]]} for u in users]}

    @safe
    def workload(self):
        """Баланс нагрузки: сколько проектов у кого, сколько ничьих, кто перегружен."""
        self._require_supervisor()
        clients = self.db.list_clients("all")
        bound = {b["login"] for b in self.db.list_bindings("all")}
        by_owner = self._obligations_by_owner()
        users = [u for u in self.db.list_users() if u["role"] in ("user", "admin") and u["active"]]
        rows = []
        for u in users:
            own = [c for c in clients if (("owner" in c.keys()) and c["owner"] == u["id"])]
            ext = [c for c in own if ("delivery" in c.keys()) and c["delivery"] == "external"]
            rows.append({"user_id": u["id"], "name": u["name"] or u["email"], "role": u["role"],
                         "clients": len(own), "obligations": len(by_owner.get(u["id"], set())),
                         "external": len(ext),
                         "bound": len([c for c in own if c["login"] in bound])})
        rows.sort(key=lambda r: -r["obligations"])
        unassigned = len([c for c in clients if not (("owner" in c.keys()) and c["owner"])])
        unassigned_bound = len([c for c in clients
                                if not (("owner" in c.keys()) and c["owner"]) and c["login"] in bound])
        total_obl = sum(r["obligations"] for r in rows)
        avg = round(total_obl / len(rows), 1) if rows else 0
        return {"rows": rows, "unassigned": unassigned, "unassigned_bound": unassigned_bound,
                "avg": avg, "total_obligations": total_obl}

    @safe
    def specialist_card(self, user_id):
        """Досье специалиста: проекты (покрытие/бюджет/доставка/заметка), тренд сдачи,
        деньги его клиентов, сообщения и ответы. Для работодателя/админа."""
        self._require_supervisor()
        uid = int(user_id)
        u = self.db.get_user(uid)
        if not u:
            raise RuntimeError("Сотрудник не найден")
        mon, nxt = self._week_bounds(0)

        def iso(d):
            return d.isoformat() + "T00:00:00"
        obl = self._obligations_by_owner().get(uid, set())
        sent = self.db.sent_logins_between(iso(mon), iso(nxt))
        skipped = self.db.status_logins_between("skipped", iso(mon), iso(nxt))
        excused = self.db.excused_logins(mon.isoformat())
        budgets = {b["login"]: dict(b) for b in self.db.list_budgets()}
        bound = {b["login"] for b in self.db.list_bindings("all")}
        projects = []
        for c in self.db.list_clients("all"):
            if not (("owner" in c.keys()) and c["owner"] == uid):
                continue
            lg = c["login"]
            b = budgets.get(lg) or {}
            st = ("sent" if lg in sent else "skipped" if lg in skipped
                  else "excused" if lg in excused else ("debt" if lg in obl else "—"))
            projects.append({
                "login": lg, "name": c["name"] or lg,
                "delivery": (c["delivery"] if "delivery" in c.keys() else None) or "telegram",
                "note": (c["note"] if "note" in c.keys() else None),
                "bound": lg in bound, "obligation": lg in obl, "week_status": st,
                "balance": b.get("balance"), "days_left": b.get("days_left"),
                "budget_status": b.get("status"), "rate": b.get("rate"),
                "last_send": self.db.last_send_at(lg),
            })
        projects.sort(key=lambda p: (p["week_status"] != "debt", -(p["rate"] or 0)))
        # тренд по неделям
        hist = []
        for back in range(7, -1, -1):
            m2, n2 = self._week_bounds(back)
            cov, tot, _ = self._coverage_for(obl, m2, n2)
            hist.append({"week": m2.isoformat(), "coverage": (round(100 * cov / tot) if tot else None),
                         "total": tot})
        # сообщения и ответы
        replies = self.db.all_note_replies()
        msgs = []
        for n in self.db.list_notes(limit=50):
            if n["to_user"] in (uid, None):
                msgs.append({"id": n["id"], "text": n["text"], "kind": n["kind"],
                             "created_at": n["created_at"], "acks": n["acks"],
                             "replies": replies.get(n["id"], [])})
        cov, tot, snt = self._coverage_for(obl, mon, nxt)
        return {
            "user": {"id": u["id"], "name": u["name"] or u["email"], "email": u["email"],
                     "role": u["role"], "active": bool(u["active"])},
            "week": {"coverage": (round(100 * cov / tot) if tot else None),
                     "obligations": tot, "sent": snt, "debt": tot - cov},
            "projects": projects, "history": hist, "messages": msgs[:15],
            "money": {"total_rate": round(sum((p["rate"] or 0) for p in projects), 2),
                      "critical": len([p for p in projects if p["budget_status"] == "critical"])},
        }

    @safe
    def excuse_add(self, login, kind="nospend", reason=None):
        """Закрыть «долг» по клиенту: уважительная причина, что отчёт не отправлен.
        kind: 'churned' (проект отвалился — бессрочно) | 'nospend'/'other' (на эту неделю).
        Наблюдатель/админ — по любому; специалист — по своему клиенту."""
        c = self.db.get_client(login)
        if not c:
            raise RuntimeError("Клиент не найден")
        owner = c["owner"] if "owner" in c.keys() else None
        if not (self._is_admin() or self._is_observer() or (self.user and owner == self.user["id"])):
            raise RuntimeError("Можно закрывать долг только по своему клиенту")
        if kind not in ("churned", "nospend", "other"):
            kind = "other"
        from datetime import date, timedelta
        today = date.today()
        week = None if kind == "churned" else (today - timedelta(days=today.weekday())).isoformat()
        if not reason:
            reason = {"churned": "проект отвалился", "nospend": "нет открута (деньги не крутятся)"}.get(kind, "уважительно")
        eid = self.db.add_excuse(login, week, kind, reason, (self.user or {}).get("id"))
        self._audit("excuse", login, "долг закрыт: {}".format(reason))
        return {"id": eid, "login": login, "kind": kind, "reason": reason, "ongoing": week is None}

    @safe
    def excuse_add_bulk(self, logins, kind="nospend", reason=None):
        """Закрыть сразу пачку долгов одной причиной (кнопка «закрыть все» / выбранные).
        Права проверяются по каждому клиенту как в excuse_add."""
        out = {"closed": 0, "errors": [], "ids": []}
        for lg in (logins or []):
            r = self.excuse_add(lg, kind, reason)   # @safe → {"ok":..,"data"/"error"}
            if r.get("ok"):
                out["closed"] += 1
                out["ids"].append((r.get("data") or {}).get("id"))
            else:
                out["errors"].append({"login": lg, "error": r.get("error")})
        return out

    @safe
    def excuse_remove(self, excuse_id):
        """Вернуть долг (снять уважительную). Наблюдатель/админ, или владелец клиента."""
        login = self.db.excuse_owner_login(int(excuse_id))
        if login is None:
            return {"removed": int(excuse_id)}
        c = self.db.get_client(login)
        owner = (c["owner"] if (c and "owner" in c.keys()) else None)
        if not (self._is_admin() or self._is_observer() or (self.user and owner == self.user["id"])):
            raise RuntimeError("Недостаточно прав")
        self.db.remove_excuse(int(excuse_id))
        return {"removed": int(excuse_id)}

    @safe
    def excuses_list(self):
        """Список уважительных (для наблюдателя/админа) — что и почему закрыто."""
        self._require_supervisor()
        return [{"id": e["id"], "login": e["login"], "client_name": e["client_name"] or e["login"],
                 "kind": e["kind"], "reason": e["reason"], "ongoing": e["week"] is None,
                 "by_name": e["by_name"], "created_at": e["created_at"]}
                for e in self.db.list_excuses()]

    @safe
    def note_create(self, to_user, text, kind="info"):
        """Оставить сообщение сотруднику (to_user=None/'all' → всем специалистам). Наблюдатель/админ.
        Сотрудник увидит его ярким баннером в кабинете, пока не нажмёт «прочитано»."""
        self._require_supervisor()
        text = (text or "").strip()
        if not text:
            raise RuntimeError("Пустое сообщение")
        if kind not in ("info", "warn", "urgent"):
            kind = "info"
        tu = None
        if to_user not in (None, "", 0, "0", "all"):
            tu = int(to_user)
            if not self.db.get_user(tu):
                raise RuntimeError("Получатель не найден")
        nid = self.db.create_note(tu, (self.user or {}).get("id"), text, kind)
        self._notify(tu, "message", "Сообщение от руководителя", text[:140], "dashboard")
        return {"id": nid, "to_user": tu, "kind": kind}

    @safe
    def notes_list(self):
        """Отправленные сообщения с числом прочтений и ответами специалистов (наблюдатель/админ)."""
        self._require_supervisor()
        replies = self.db.all_note_replies()
        return [{"id": n["id"], "to_user": n["to_user"],
                 "to_name": (n["to_name"] if n["to_user"] is not None else "всем специалистам") or "?",
                 "from_name": n["from_name"], "text": n["text"], "kind": n["kind"],
                 "created_at": n["created_at"], "acks": n["acks"],
                 "replies": replies.get(n["id"], [])}
                for n in self.db.list_notes()]

    @safe
    def note_delete(self, note_id):
        self._require_supervisor()
        self.db.delete_note(int(note_id))
        return {"deleted": int(note_id)}

    @safe
    def my_notes(self):
        """Неподтверждённые сообщения текущему пользователю (яркий баннер). Любой вошедший."""
        if not self.user:
            return []
        return [{"id": n["id"], "text": n["text"], "kind": n["kind"],
                 "from_name": n["from_name"], "created_at": n["created_at"]}
                for n in self.db.notes_for_user(self.user["id"])]

    @safe
    def note_ack(self, note_id):
        """«Прочитано» — убрать баннер у текущего пользователя."""
        if self.user:
            self.db.ack_note(int(note_id), self.user["id"])
        return {"acked": int(note_id)}

    @safe
    def note_reply(self, note_id, text):
        """Специалист отвечает на сообщение работодателя. Ответ виден наблюдателю в Контроле.
        Отправка ответа = «прочитано» (баннер уходит)."""
        if not self.user:
            raise RuntimeError("Доступно только в кабинете")
        text = (text or "").strip()
        if not text:
            raise RuntimeError("Пустой ответ")
        self.db.add_note_reply(int(note_id), self.user["id"], text)
        self.db.ack_note(int(note_id), self.user["id"])   # ответил → баннер убираем
        return {"replied": int(note_id)}

    # ---------- settings ----------
    @safe
    def settings(self):
        rep = load_report_config()
        app = load_app_config()
        # наличие токенов (сами значения не раскрываем)
        tg_has = ya_has = False
        try:
            secrets = load_secrets()
            tgv = secrets.get("telegram_bot_token") or ""
            yav = secrets.get("yandex_oauth_token") or ""
            tg_has = bool(tgv) and "ВСТАВЬ" not in tgv
            ya_has = bool(yav) and "ВСТАВЬ" not in yav
        except Exception:  # noqa: BLE001
            pass
        ya_status = "ок" if ya_has else "нет"
        tg_status, tg_name = "нет", None
        if tg_has:
            try:
                tg_name = self._bot_name()
                tg_status = "ок"
            except Exception as e:  # noqa: BLE001
                tg_status = "ошибка: {}".format(e)
        return {
            "intro": rep.get("intro", ""),
            "specialist_note": rep.get("specialist_note", ""),
            "attribution_model": rep.get("attribution_model") or default_attribution(),
            "admin_user_ids": app.get("admin_user_ids", []),
            "report_day": app.get("report_day", "Понедельник"),
            "report_time": app.get("report_time", "09:00"),
            "telegram": {"status": tg_status, "username": tg_name, "has_token": tg_has},
            "yandex": {"status": ya_status, "has_token": ya_has},
        }

    @safe
    def save_settings(self, intro=None, specialist_note=None, attribution_model=None,
                      admin_user_ids=None, report_day=None, report_time=None):
        rep_patch = {}
        if intro is not None:
            rep_patch["intro"] = intro
        if specialist_note is not None:
            rep_patch["specialist_note"] = specialist_note
        if attribution_model is not None:
            rep_patch["attribution_model"] = attribution_model
        if rep_patch:
            save_report_config(rep_patch)
        app_patch = {}
        if admin_user_ids is not None:
            ids = []
            for x in admin_user_ids:
                try:
                    ids.append(int(x))
                except (TypeError, ValueError):
                    pass
            app_patch["admin_user_ids"] = ids
        if report_day is not None:
            app_patch["report_day"] = report_day
        if report_time is not None:
            app_patch["report_time"] = report_time
        if app_patch:
            self.cfg = save_app_config(app_patch)
        return True

    @safe
    def save_secrets(self, telegram_bot_token=None, yandex_oauth_token=None):
        """Сохраняет токены в secrets.json прямо из интерфейса (без правки файла руками)."""
        patch = {}
        if telegram_bot_token and telegram_bot_token.strip():
            patch["telegram_bot_token"] = telegram_bot_token.strip()
        if yandex_oauth_token and yandex_oauth_token.strip():
            patch["yandex_oauth_token"] = yandex_oauth_token.strip()
        if not patch:
            raise RuntimeError("Введите хотя бы один токен")
        _save_secrets(patch)
        self._tg = None            # сброс кэша — статус/бот перечитают новый токен
        self._bot_username = None
        try:
            listener.start(load_secrets(), self.cfg)   # поднять слушатель с новым токеном
        except Exception:  # noqa: BLE001
            pass
        return True

    @safe
    def connect_link(self, login):
        return {"link": "https://t.me/{}?startgroup={}".format(self._bot_name(), login)}
