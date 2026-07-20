# -*- coding: utf-8 -*-
"""Бот-слушатель: определяет, в каких чатах он находится, и привязывает их к клиентам.

Главная задача первого этапа: добавляешь бота в чат — он сразу понимает,
ГДЕ он (название, id, тип), запоминает это в базе и сообщает в чат.

Запуск (Windows):  run_bot.bat
Или из app/:       python -m iigbot.bot
"""
import socket
import sys
import time
import traceback

from .settings import load_secrets, load_app_config
from .storage import Storage
from .telegram_api import Telegram, TelegramError

# Без явного allowed_updates Telegram НЕ присылает my_chat_member (события добавления/удаления).
ALLOWED_UPDATES = ["message", "edited_message", "my_chat_member", "chat_member", "callback_query"]
JOINED = ("member", "administrator", "creator", "restricted")
LEFT = ("left", "kicked")


def _title(chat):
    return chat.get("title") or chat.get("username") or chat.get("first_name") or str(chat.get("id"))


def is_admin(msg, tg, cfg):
    """Кому разрешён /bind. Если admin_user_ids не задан — разрешаем всем (с предупреждением)."""
    uid = (msg.get("from") or {}).get("id")
    admins = cfg.get("admin_user_ids") or []
    if not admins:
        return True
    if uid in admins:
        return True
    chat = msg["chat"]
    if chat.get("type") in ("group", "supergroup"):
        try:
            admin_ids = [(m.get("user") or {}).get("id") for m in tg.get_chat_administrators(chat["id"])]
            return uid in admin_ids
        except TelegramError:
            return False
    return False


def _binding_line(db, chat_id):
    b = db.get_binding(chat_id)
    if not b:
        return None
    c = db.get_client(b["login"])
    return "• Привязан к клиенту: {}{}".format(b["login"], " — " + c["name"] if c and c["name"] else "")


def announce_join(tg, db, chat):
    lines = [
        "✅ Я на связи и определил, где нахожусь.",
        "• Чат: «{}»".format(_title(chat)),
        "• ID чата: {}".format(chat["id"]),
        "• Тип: {}".format(chat.get("type")),
    ]
    bl = _binding_line(db, chat["id"])
    if bl:
        lines.append(bl)
    else:
        lines.append("• Пока НЕ привязан к клиенту Директа.")
        lines.append("  Привязать: /bind <логин> (или через веб-админку).")
    try:
        tg.send_message(chat["id"], "\n".join(lines))
    except TelegramError as e:
        print("  (не смог написать в чат {}: {})".format(chat["id"], e))


def handle_my_chat_member(ev, tg, db, cfg):
    chat = ev["chat"]
    status = (ev.get("new_chat_member") or {}).get("status")
    if status in JOINED:
        db.upsert_chat(chat, my_status=status, status="active")
        print("➕ Добавлен/обновлён: «{}» | id={} | type={} | роль={}".format(
            _title(chat), chat["id"], chat.get("type"), status))
        if cfg.get("announce_on_join", True) and chat.get("type") in ("group", "supergroup", "channel"):
            announce_join(tg, db, chat)
    elif status in LEFT:
        db.upsert_chat(chat, my_status=status, status="removed")
        print("➖ Удалён: «{}» | id={}".format(_title(chat), chat["id"]))


def cmd_whereami(msg, tg, db):
    chat = msg["chat"]
    lines = [
        "Я нахожусь здесь:",
        "• Чат: «{}»".format(_title(chat)),
        "• ID: {}".format(chat["id"]),
        "• Тип: {}".format(chat.get("type")),
    ]
    bl = _binding_line(db, chat["id"])
    lines.append(bl if bl else "• Не привязан. /bind <логин> — привязать этот чат к клиенту.")
    tg.send_message(chat["id"], "\n".join(lines))


def cmd_bind(msg, tg, db, cfg, arg):
    chat = msg["chat"]
    if not arg:
        tg.send_message(chat["id"], "Использование: /bind <логин клиента в Директе>")
        return
    if not is_admin(msg, tg, cfg):
        tg.send_message(chat["id"], "⛔ Привязывать чат может только администратор/владелец бота.")
        return
    login = arg.split()[0]
    db.set_binding(chat["id"], login, bound_by=(msg.get("from") or {}).get("id"))
    c = db.get_client(login)
    note = " — " + c["name"] if c and c["name"] else " (клиента пока нет в базе, появится после синхронизации)"
    tg.send_message(chat["id"], "✅ Чат привязан к клиенту: {}{}\nОтчёты по нему будут приходить сюда.".format(login, note))
    print("🔗 bind chat {} -> {} (by {})".format(chat["id"], login, (msg.get("from") or {}).get("id")))


def cmd_bind_alert(msg, tg, db, token):
    """Привязка этой лички к бюджет-алертам по одноразовому токену из кабинета (Настройки)."""
    chat = msg["chat"]
    uid = db.get_kv("alerttok_" + token) if token else None
    if not uid:
        tg.send_message(chat["id"], "Ссылка привязки алертов устарела или неверна. Открой в кабинете "
                        "Настройки → «Алерты по бюджету» и нажми «Привязать Telegram» заново.")
        return
    try:
        db.set_user_alert_chat(int(uid), chat["id"])
        db.set_kv("alerttok_" + token, "")   # гасим токен
    except Exception as e:  # noqa: BLE001
        tg.send_message(chat["id"], "Не получилось привязать: {}".format(e))
        return
    tg.send_message(chat["id"], "✅ Готово! Сюда будут приходить алерты по бюджету твоих клиентов "
                    "(когда деньги на исходе). Отключить — в кабинете, Настройки.")
    print("🔔 alert chat bound: user {} -> chat {}".format(uid, chat["id"]))


def cmd_unbind(msg, tg, db, cfg):
    chat = msg["chat"]
    if not is_admin(msg, tg, cfg):
        tg.send_message(chat["id"], "⛔ Только администратор/владелец бота.")
        return
    db.remove_binding(chat["id"])
    tg.send_message(chat["id"], "Привязка этого чата удалена.")
    print("🔓 unbind chat {}".format(chat["id"]))


def cmd_chats(msg, tg, db):
    rows = db.list_chats()
    lines = ["Известные чаты ({}):".format(len(rows))]
    for r in rows:
        b = db.get_binding(r["chat_id"])
        tag = "→ {}".format(b["login"]) if b else "— не привязан"
        flag = "" if r["status"] == "active" else " [удалён]"
        lines.append("• «{}» id={} {}{}".format(r["title"], r["chat_id"], tag, flag))
    tg.send_message(msg["chat"]["id"], "\n".join(lines))


HELP = (
    "Команды:\n"
    "/whereami — где я сейчас (чат и привязка)\n"
    "/bind <логин> — привязать этот чат к клиенту Директа (админ)\n"
    "/unbind — снять привязку (админ)\n"
    "/status — показать привязку этого чата\n"
    "/chats — список всех известных чатов\n"
    "/help — это сообщение"
)


def handle_message(msg, tg, db, cfg, bot_username):
    chat = msg["chat"]
    db.upsert_chat(chat, my_status="member", status="active")
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.lower()
    if "@" in cmd:  # /bind@MyBot -> снимаем упоминание, чужие команды игнорим
        cmd, _, mention = cmd.partition("@")
        if bot_username and mention and mention.lower() != bot_username.lower():
            return
    arg = arg.strip()

    if cmd == "/start":
        if arg.startswith("alert_"):   # deep-link привязки лички для бюджет-алертов
            cmd_bind_alert(msg, tg, db, arg[len("alert_"):])
        elif arg:                       # deep-link: t.me/Bot?start=<логин>
            cmd_bind(msg, tg, db, cfg, arg)
        else:
            cmd_whereami(msg, tg, db)
    elif cmd in ("/whereami", "/where", "/here"):
        cmd_whereami(msg, tg, db)
    elif cmd == "/bind":
        cmd_bind(msg, tg, db, cfg, arg)
    elif cmd == "/unbind":
        cmd_unbind(msg, tg, db, cfg)
    elif cmd == "/status":
        cmd_whereami(msg, tg, db)
    elif cmd == "/chats":
        cmd_chats(msg, tg, db)
    elif cmd == "/help":
        tg.send_message(chat["id"], HELP)


def handle_update(u, tg, db, cfg, bot_username):
    if "my_chat_member" in u:
        handle_my_chat_member(u["my_chat_member"], tg, db, cfg)
    elif "message" in u:
        handle_message(u["message"], tg, db, cfg, bot_username)


def run_loop(db, tg, cfg, bot_username, stop_event=None):
    """Цикл опроса Telegram. Используется и ботом (main), и десктоп-приложением (в фоне).

    stop_event — необязательный threading.Event для аккуратной остановки из другого потока.
    """
    stored = db.get_kv("update_offset")
    if stored is None:
        try:
            pending = tg.get_updates(offset=None, allowed_updates=ALLOWED_UPDATES, timeout=0) or []
        except TelegramError:
            pending = []
        if pending:
            last = pending[-1]["update_id"]
            db.set_kv("update_offset", last)
            offset = last + 1
            print("   (пропущено старых событий: {})".format(len(pending)))
        else:
            offset = None
    else:
        offset = int(stored) + 1

    while True:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            updates = tg.get_updates(offset=offset, allowed_updates=ALLOWED_UPDATES) or []
        except KeyboardInterrupt:
            print("\nВыход.")
            break
        except TelegramError as e:
            print("⚠️ getUpdates: {} — пауза 3с".format(e))
            time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                handle_update(u, tg, db, cfg, bot_username)
            except Exception as e:  # noqa: BLE001 — один кривой апдейт не должен ронять бота
                print("⚠️ ошибка обработки update {}: {}".format(u.get("update_id"), e))
                traceback.print_exc()
            db.set_kv("update_offset", u["update_id"])


def webhook_command(args):
    """Управление вебхуком: python -m iigbot webhook set <https-url> | delete | info

    На хостинге бот работает через вебхук, а не long-polling. `set` генерирует секрет,
    сохраняет его в БД (kv.tg_webhook_secret — его же проверяет /tg/webhook) и регистрирует
    URL в Telegram. ВАЖНО: у агентства один токен — вебхук и long-polling взаимоисключающи
    (Telegram отдаёт 409 на getUpdates при активном вебхуке). Т.е. на сервере — вебхук,
    десктоп-слушатель тогда не запускать."""
    import os
    secrets = load_secrets()
    cfg = load_app_config()
    token = (secrets.get("telegram_bot_token") or "").strip()
    if not token or "ВСТАВЬ" in token:
        print("❌ Не задан telegram_bot_token в secrets.json")
        return 1
    tg = Telegram(token, timeout=20)
    db = Storage(cfg["db_path"])
    sub = (args[0].lower() if args else "info")

    if sub == "set":
        if len(args) < 2:
            print("Использование: webhook set https://<адрес>/tg/webhook")
            return 2
        url = args[1]
        secret = db.get_kv("tg_webhook_secret")
        if not secret:
            secret = os.urandom(24).hex()
            db.set_kv("tg_webhook_secret", secret)
        tg.set_webhook(url, secret_token=secret, allowed_updates=ALLOWED_UPDATES, drop_pending=True)
        print("✅ Вебхук установлен: {}".format(url))
        print("   Секрет сохранён в БД (kv.tg_webhook_secret) — его проверяет эндпоинт /tg/webhook.")
        print("   Слушатель по long-polling теперь НЕ запускать (конфликт 409).")
        return 0
    if sub in ("delete", "del", "off", "remove"):
        tg.delete_webhook(drop_pending=False)
        print("✅ Вебхук удалён. Бот снова может работать по long-polling (десктоп/консоль).")
        return 0
    info = tg.get_webhook_info() or {}
    print("Состояние вебхука:")
    print("  URL: {}".format(info.get("url") or "(не задан — работает long-polling)"))
    for k in ("pending_update_count", "ip_address", "last_error_date", "last_error_message"):
        if info.get(k):
            print("  {}: {}".format(k, info[k]))
    return 0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # корректный вывод эмодзи/кириллицы в консоли Windows
    except Exception:
        pass

    # анти-дубликат: только один слушатель на машину (иначе конфликт getUpdates и двойные ответы).
    # Лок держим до конца процесса — освобождается ОС при выходе.
    _singleton_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _singleton_lock.bind(("127.0.0.1", 49517))
    except OSError:
        print("Слушатель уже запущен — выходим, чтобы не дублировать ответы.")
        return

    secrets = load_secrets()
    cfg = load_app_config()
    token = (secrets.get("telegram_bot_token") or "").strip()
    if not token or "ВСТАВЬ" in token:
        print("❌ Не задан telegram_bot_token в secrets.json")
        return

    db = Storage(cfg["db_path"])
    tg = Telegram(token, timeout=cfg["poll_timeout"])

    try:
        me = tg.get_me()
    except TelegramError as e:
        print("❌ Не удалось подключиться к Telegram: {}".format(e))
        return
    bot_username = me.get("username")
    print("✅ Бот подключён: @{} (id {})".format(bot_username, me.get("id")))
    print("   База: {}".format(cfg["db_path"]))
    if cfg.get("admin_user_ids"):
        print("   Админы (/bind): {}".format(cfg["admin_user_ids"]))
    else:
        print("   ⚠️ admin_user_ids не заданы — /bind разрешён всем. Впиши свой Telegram id в app_config.json.")
    print("   Жду события. Добавь бота в чат — он определит, где находится. Ctrl+C для выхода.")
    run_loop(db, tg, cfg, bot_username)


if __name__ == "__main__":
    main()
