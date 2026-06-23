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
)
from .import_config import normalize_goals


def safe(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return {"ok": True, "data": fn(self, *args, **kwargs)}
        except Exception as e:  # noqa: BLE001
            try:                       # в exe stdout перенаправлен в iig.log — ошибки будут видны
                print("[api] {}: {}".format(fn.__name__, e))
            except Exception:          # noqa: BLE001
                pass
            return {"ok": False, "error": str(e)}
    return wrapper


class Api:
    def __init__(self):
        self.cfg = load_app_config()
        self.db = Storage(self.cfg["db_path"])
        self._tg = None
        self._bot_username = None
        self._mk_counters = None       # кэш списка счётчиков Метрики

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
        note = rep.get("specialist_note") or "Через некоторое время специалист даст комментарий по этому отчёту."
        attr = rep.get("attribution_model") or "LSC"
        return intro, note, attr

    def _chat_title(self, chat_id):
        c = self.db.get_chat(chat_id)
        return c["title"] if c else str(chat_id)

    # ---------- dashboard ----------
    @safe
    def dashboard(self):
        chats = [c for c in self.db.list_chats() if c["status"] == "active"]
        clients = self.db.list_clients()
        bindings = self.db.list_bindings()
        bound_chat_ids = {b["chat_id"] for b in bindings}
        bound_logins = {b["login"] for b in bindings}
        unbound_chats = [c for c in chats if c["chat_id"] not in bound_chat_ids]
        clients_no_chat = [c for c in clients if c["login"] not in bound_logins]
        history = self.db.list_bindings  # placeholder, real history below
        rows = self.db.conn.execute(
            "SELECT status, COUNT(*) n FROM send_log GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        return {
            "clients": len(clients),
            "chats": len(chats),
            "bound": len(bound_chat_ids),
            "unbound_chats": len(unbound_chats),
            "clients_no_chat": len(clients_no_chat),
            "errors": by_status.get("error", 0),
            "alerts": {
                "unbound_chats": len(unbound_chats),
                "clients_no_chat": len(clients_no_chat),
            },
        }

    # ---------- clients ----------
    @safe
    def clients(self):
        binds = {b["login"]: b for b in self.db.list_bindings()}
        out = []
        for c in self.db.list_clients():
            b = binds.get(c["login"])
            try:
                goals = json.loads(c["goals"] or "[]")
            except (ValueError, TypeError):
                goals = []
            out.append({
                "login": c["login"], "name": c["name"], "source": c["source"],
                "attribution": c["attribution"] or "",
                "goals": goals,
                "chat_id": b["chat_id"] if b else None,
                "chat_title": self._chat_title(b["chat_id"]) if b else None,
            })
        return out

    @safe
    def client(self, login):
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
            "chats": [{"chat_id": b["chat_id"], "title": self._chat_title(b["chat_id"])} for b in binds],
        }

    @safe
    def save_client(self, login, name=None, goals=None, attribution=None):
        self.db.upsert_client(
            login=login, name=name,
            goals=normalize_goals(goals) if goals is not None else None,
            attribution=attribution,
        )
        return True

    @safe
    def metrika_goals(self, login):
        """Находит счётчик(и) клиента (по настройкам кампаний Директа и/или домену)
        среди доступных токену в Метрике и возвращает ВСЕ их цели. Ничего не сохраняет."""
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
        # сначала счётчики из кампаний (доступные), затем совпавшие по домену
        candidates = []
        for cid in camp_ids + dom_ids:
            if cid in acc_ids and cid not in candidates:
                candidates.append(cid)
        if not candidates:                 # вдруг список доступных неполон — пробуем кампанийные напрямую
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
                    goals.append(g)
        note = "" if goals else "Не нашёл доступного счётчика Метрики для этого клиента (нет доступа к его счётчику)."
        return {"goals": goals, "counters": used, "note": note,
                "from_campaigns": camp_ids, "from_domain": dom_ids}

    @safe
    def sync_clients(self):
        clients = yandex.get_agency_clients(load_secrets()["yandex_oauth_token"])
        n = 0
        for c in clients:
            if c.get("Login"):
                self.db.upsert_client(login=c["Login"], name=c.get("ClientInfo") or c["Login"], source="yandex")
                n += 1
        return {"synced": n}

    @safe
    def import_config(self):
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
        binds = {b["chat_id"]: b for b in self.db.list_bindings()}
        names = {c["login"]: c["name"] for c in self.db.list_clients()}
        out = []
        for c in self.db.list_chats():
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
        if not login:
            self.db.remove_binding(int(chat_id))
            return {"bound": False}
        self.db.set_binding(int(chat_id), login)
        return {"bound": True}

    @safe
    def unbind(self, chat_id):
        self.db.remove_binding(int(chat_id))
        return {"bound": False}

    @safe
    def delete_chat(self, chat_id):
        """Удаляет чат из базы (для «висяков» — когда бота уже выгнали, а строка осталась)."""
        self.db.delete_chat(int(chat_id))
        return {"deleted": True}

    # ---------- matcher (подсказки привязок) ----------
    @safe
    def suggestions(self):
        binds = self.db.list_bindings()
        bound_chat_ids = {b["chat_id"] for b in binds}
        bound_logins = {b["login"] for b in binds}
        chats = [c for c in self.db.list_chats()
                 if c["status"] == "active" and c["chat_id"] not in bound_chat_ids]
        clients = [c for c in self.db.list_clients() if c["login"] not in bound_logins]
        free_clients = [{"login": c["login"], "name": c["name"]} for c in clients]
        out = []
        for ch in chats:
            best, best_score = None, 0.0
            title = (ch["title"] or "").lower()
            for c in clients:
                score = max(
                    difflib.SequenceMatcher(None, title, (c["name"] or "").lower()).ratio(),
                    difflib.SequenceMatcher(None, title, (c["login"] or "").lower()).ratio(),
                )
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

    # ---------- reports ----------
    @safe
    def preview(self, login):
        token = load_secrets()["yandex_oauth_token"]
        intro, note, attr = self._report_ctx()
        text, camps, per = report.build_for_login(token, self.db, login, intro, note, attr)
        if text is None:
            return {"text": None, "reason": "Нет активных кампаний за последние 4 недели — клиент пропускается."}
        return {"text": text, "campaigns": len(camps), "period": per}

    @safe
    def send_test(self, login):
        token = load_secrets()["yandex_oauth_token"]
        intro, note, attr = self._report_ctx()
        return report.send_for_login(token, self._tg_client(), self.db, login, intro, note, attr)

    @safe
    def run_weekly(self):
        token = load_secrets()["yandex_oauth_token"]
        intro, note, attr = self._report_ctx()
        return report.run_weekly(token, self._tg_client(), self.db, intro, note, attr)

    @safe
    def history(self):
        rows = self.db.conn.execute(
            "SELECT * FROM send_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
        names = {c["login"]: c["name"] for c in self.db.list_clients()}
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
        """Список кампаний клиента для фильтра конструктора (только чтение)."""
        from . import yandex
        token = load_secrets()["yandex_oauth_token"]
        camps = yandex.get_campaigns(token, login)
        return [{"id": str(c.get("Id")), "name": c.get("Name") or str(c.get("Id"))} for c in camps]

    def _report_build(self, login, level, date_from, date_to, attribution, limit,
                      segments=None, date_grain="day", campaign=None):
        from . import report_custom as RC
        token = load_secrets()["yandex_oauth_token"]
        c = self.db.get_client(login)
        if not c:
            raise RuntimeError("Клиент {} не найден".format(login))
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
                     limit=100, segments=None, date_grain="day", campaign=None):
        res = self._report_build(login, level, date_from, date_to, attribution, limit, segments, date_grain, campaign)
        res["chats"] = [{"chat_id": b["chat_id"], "title": self._chat_title(b["chat_id"])}
                        for b in self.db.bindings_for_login(login)]
        return res

    @safe
    def report_send(self, login, level="campaign", date_from=None, date_to=None, attribution="LSC",
                    limit=100, segments=None, date_grain="day", campaign=None):
        res = self._report_build(login, level, date_from, date_to, attribution, limit, segments, date_grain, campaign)
        chats = self.db.bindings_for_login(login)
        if not chats:
            raise RuntimeError("Клиент не привязан ни к одному чату")
        tg = self._tg_client()
        sent = 0
        for b in chats:
            tg.send_message(b["chat_id"], res["text"])
            sent += 1
        return {"sent": sent}

    @safe
    def report_export_xlsx(self, login, level="campaign", date_from=None, date_to=None, attribution="LSC",
                           limit=1000, segments=None, date_grain="day", campaign=None):
        """Строит отчёт и сохраняет .xlsx в подпапку reports/ рядом с программой. Ничего не отправляет."""
        import os
        import re
        from . import report_custom as RC
        from .settings import BASE_DIR
        res = self._report_build(login, level, date_from, date_to, attribution, limit, segments, date_grain, campaign)
        folder = os.path.join(BASE_DIR, "reports")
        os.makedirs(folder, exist_ok=True)
        safe_login = re.sub(r"[^A-Za-z0-9_.-]", "_", str(login))
        fn = "report_{}_{}_{}_{}.xlsx".format(safe_login, level or "campaign", res["date_from"], res["date_to"])
        path = os.path.join(folder, fn)
        RC.to_xlsx(res, path)
        return {"path": path, "filename": fn, "n_rows": res["n_shown"]}

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
