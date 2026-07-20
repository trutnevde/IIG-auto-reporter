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
    load_secrets, load_app_config, load_report_config,
    save_app_config, save_report_config, save_secrets as _save_secrets,
    log_error,
)
from .import_config import normalize_goals


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
        attr = rep.get("attribution_model") or "LSC"
        return intro, note, attr

    def _chat_title(self, chat_id):
        c = self.db.get_chat(chat_id)
        return c["title"] if c else str(chat_id)

    # ---------- dashboard ----------
    @safe
    def dashboard(self):
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
        covered = delivered | (obligations & (skip_this | excused))
        debt = obligations - covered
        week = {"obligations": len(obligations), "sent": len(delivered),
                "debt": len(debt), "covered": len(covered),
                "coverage": (round(100 * len(covered) / len(obligations)) if obligations else None)}
        # сторонние, по которым на этой неделе ещё не собирали отчёт (нет ни sent, ни skipped)
        ext_pending = sorted(external_logins - sent_this - skip_this)

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

        return {
            "role": (self.user.get("role") if self.user else "admin"),
            "clients": len(clients),
            "chats": len(chats),
            "bound": len(bound_chat_ids),
            "unbound_chats": len(unbound_chats),
            "clients_no_chat": len(clients_no_chat),
            "errors": by_status.get("error", 0),
            "week": week,
            "external_pending": len(ext_pending),
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
            "attribution": c["attribution"] or "LSC", "goals": goals,
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
        self._require_admin()
        clients = yandex.get_agency_clients(load_secrets()["yandex_oauth_token"])
        n = 0
        for c in clients:
            if c.get("Login"):
                self.db.upsert_client(login=c["Login"], name=c.get("ClientInfo") or c["Login"], source="yandex")
                n += 1
        return {"synced": n}

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
        return {"bound": True}

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
    def bind_bulk(self, min_confidence=75):
        """Массовая привязка: привязывает все непривязанные чаты, где уверенность подсказки
        >= порога. Возвращает {bound, min_confidence, details}. Остальное правишь вручную."""
        self._require_write()
        thr = int(min_confidence)
        bound, details = 0, []
        for x in self._suggest_matches()["matches"]:
            if x.get("suggest_login") and x.get("confidence", 0) >= thr:
                try:
                    self._require_bindable(x["suggest_login"])
                    self.db.set_binding(int(x["chat_id"]), x["suggest_login"])
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
                     "details": [], "summary": None, "error": None,
                     "only_failed": only_failed, "dry": bool(dry_run)}
        import threading
        threading.Thread(target=self._run_weekly_worker, args=(logins, bool(dry_run)),
                         daemon=True).start()
        return {"started": True, "only_failed": only_failed, "dry": bool(dry_run)}

    @safe
    def run_weekly_progress(self):
        r = getattr(self, "_run", None) or {"running": False, "done": 0, "total": 0, "details": []}
        return {"running": r.get("running", False), "done": r.get("done", 0),
                "total": r.get("total", 0), "details": r.get("details", []),
                "summary": r.get("summary"), "error": r.get("error"),
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
                       attribution or "LSC", goal_defs, segments, date_grain or "day", campaign, limit or 100)
        res["client_name"] = c["name"] or login
        res["text"] = RC.to_text(login, c["name"] or login, res)
        return res

    @safe
    def report_query(self, login, level="campaign", date_from=None, date_to=None, attribution="LSC",
                     limit=100, segments=None, date_grain="day", campaign=None, goal_ids=None):
        res = self._report_build(login, level, date_from, date_to, attribution, limit, segments, date_grain, campaign, goal_ids)
        res["chats"] = [{"chat_id": b["chat_id"], "title": self._chat_title(b["chat_id"])}
                        for b in self.db.bindings_for_login(login)]
        return res

    @safe
    def report_send(self, login, level="campaign", date_from=None, date_to=None, attribution="LSC",
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
    def report_export_xlsx(self, login, level="campaign", date_from=None, date_to=None, attribution="LSC",
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
                    out.append({"login": c["login"], "name": c["name"] or c["login"],
                                "sheet": sheets[key]["title"]})
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
        self._require_admin()
        out = []
        for u in self.db.list_users():
            out.append({"id": u["id"], "email": u["email"], "name": u["name"] or "",
                        "role": u["role"], "active": bool(u["active"]),
                        "clients": len(self.db.owned_logins(u["id"]))})
        return out

    @safe
    def user_create(self, email, password, name=None, role="user"):
        self._require_admin()
        from . import auth
        email = (email or "").strip().lower()
        if not email or not password:
            raise RuntimeError("Нужны email и пароль")
        if role not in ("user", "admin", "observer"):
            role = "user"
        if self.db.get_user_by_email(email):
            raise RuntimeError("Пользователь с таким email уже есть")
        uid = self.db.create_user(email, auth.hash_password(password), name, role)
        return {"id": uid, "email": email, "role": role}

    @safe
    def user_set_role(self, user_id, role):
        """Сменить роль пользователя (в т.ч. выдать «Наблюдатель»). Админ."""
        self._require_admin()
        if role not in ("user", "admin", "observer"):
            raise RuntimeError("Неизвестная роль")
        self.db.set_user_role(int(user_id), role)
        return {"id": int(user_id), "role": role}

    @safe
    def user_set_active(self, user_id, active):
        self._require_admin()
        self.db.set_user_active(int(user_id), bool(active))
        return {"id": int(user_id), "active": bool(active)}

    @safe
    def user_set_password(self, user_id, password):
        self._require_admin()
        from . import auth
        if not password:
            raise RuntimeError("Пустой пароль")
        self.db.set_user_password(int(user_id), auth.hash_password(password))
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
        return {"login": login, "owner": owner,
                "delivery": ("external" if delivery == "external" else "telegram") if delivery is not None else None}

    @safe
    def set_delivery_super(self, login, mode):
        """Сменить доставку клиента при раздаче (админ/наблюдатель), НЕ трогая владельца."""
        self._require_supervisor()
        if not self.db.get_client(login):
            raise RuntimeError("Клиент не найден")
        self.db.set_client_delivery(login, "external" if mode == "external" else "telegram")
        return {"login": login, "delivery": "external" if mode == "external" else "telegram"}

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
        """Куда мне идут бюджет-алерты по СВОИМ клиентам: linked=True — привязана личка по deep-link
        (надёжно, работает и без публичного @username). alert_username — запасной способ (по @username)."""
        if not self.user:
            return {"alert_username": None, "linked": False}
        u = self.db.get_user(self.user["id"])
        return {"alert_username": (u["alert_username"] if (u is not None and "alert_username" in u.keys()) else None),
                "linked": bool((u["alert_chat_id"] if (u is not None and "alert_chat_id" in u.keys()) else None))}

    @safe
    def set_my_alert(self, username):
        """Задать свой @username для алертов по своим клиентам (пусто = не получать персонально)."""
        if not self.user:
            raise RuntimeError("Доступно только в кабинете")
        self._require_write()
        username = (username or "").strip().lstrip("@").lower() or None
        self.db.set_user_alert(self.user["id"], username)
        return {"saved": True, "alert_username": username}

    @safe
    def alert_link(self):
        """Одноразовая deep-link для НАДЁЖНОЙ привязки лички к бюджет-алертам: пользователь
        открывает ссылку и жмёт Start — бот сохраняет его chat_id напрямую (без @username)."""
        if not self.user:
            raise RuntimeError("Доступно только в кабинете")
        self._require_write()
        import os
        token = os.urandom(6).hex()
        self.db.set_kv("alerttok_" + token, str(self.user["id"]))
        return {"link": "https://t.me/{}?start=alert_{}".format(self._bot_name(), token)}

    @safe
    def alert_unlink(self):
        """Отвязать личку от алертов."""
        if not self.user:
            raise RuntimeError("Доступно только в кабинете")
        self._require_write()
        self.db.set_user_alert_chat(self.user["id"], None)
        return {"linked": False}

    # ---------- бюджеты ----------
    @safe
    def budgets(self):
        """Вкладка «Бюджеты»: строки по видимости (наблюдатель — все, специалист/админ — свои),
        когда собирали последний раз и статус фонового обновления."""
        visible = {c["login"] for c in self.db.list_clients(self._owner())}
        rows = [dict(r) for r in self.db.list_budgets() if r["login"] in visible]
        return {"rows": rows, "updated": self.db.get_kv("budgets_updated"),
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
        for u in self.db.list_users():
            if u["role"] == "observer":
                continue
            bset = bound_by_owner.get(u["id"], set())
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
                "user_id": u["id"], "name": u["name"] or u["email"], "email": u["email"],
                "role": u["role"], "active": bool(u["active"]),
                "bound": total, "sent": len(done), "on_monday": len(bset & sent_mon),
                "excused": exc_items, "debt": len(debt),
                "missing": [{"login": m, "name": names.get(m, m)} for m in debt],
                "coverage": (round(100 * covered / total) if total else None),
                "prev_coverage": (round(100 * len(bset & sent_prev) / total) if total else None),
                "last_activity": last, "status": status,
            })
        order = {"miss": 0, "partial": 1, "none": 2, "ok": 3}
        rows.sort(key=lambda r: (order.get(r["status"], 9), -(r["bound"] or 0)))
        bt = sum(r["bound"] for r in rows)
        st = sum(r["sent"] for r in rows)
        ex = sum(len(r["excused"]) for r in rows)
        dt = sum(r["debt"] for r in rows)
        agency = {"week_from": mon.isoformat(), "week_to": today.isoformat(),
                  "specialists": sum(1 for r in rows if r["bound"] or r["role"] == "admin"),
                  "bound_total": bt, "sent_total": st, "excused_total": ex, "debt_total": dt,
                  "coverage": (round(100 * (st + ex) / bt) if bt else None),
                  "at_risk": sum(1 for r in rows if r["status"] in ("miss", "partial"))}
        return {"agency": agency, "rows": rows}

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
        return {"id": eid, "login": login, "kind": kind, "reason": reason, "ongoing": week is None}

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
            "attribution_model": rep.get("attribution_model", "LSC"),
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
