# -*- coding: utf-8 -*-
"""Управление веб-аккаунтами из консоли (сид первого админа, приглашения, сброс пароля).

  python -m iigbot useradd <email> <пароль> [Имя] [--admin]   создать пользователя
  python -m iigbot users                                       список пользователей
  python -m iigbot passwd <email> <новый-пароль>               сменить пароль
  python -m iigbot useroff <email> | useron <email>            заблокировать/разблокировать
  python -m iigbot assign <email> <login> [<login> ...]        назначить клиентов владельцу

Публичной регистрации в вебе нет — учётки заводит админ (этими командами или разделом
«Пользователи» в интерфейсе).
"""
from .storage import Storage
from .settings import load_app_config
from . import auth


def _db():
    return Storage(load_app_config()["db_path"])


def useradd(argv):
    admin = "--admin" in argv
    rest = [a for a in argv if not a.startswith("-")]
    if len(rest) < 2:
        print("Использование: useradd <email> <пароль> [Имя] [--admin]")
        return 2
    email, password = rest[0], rest[1]
    name = rest[2] if len(rest) > 2 else None
    db = _db()
    if db.get_user_by_email(email):
        print("Пользователь уже существует: {}".format(email))
        return 1
    uid = db.create_user(email, auth.hash_password(password), name, "admin" if admin else "user")
    print("Создан {} #{} ({})".format("АДМИН" if admin else "пользователь", uid, email))
    return 0


def users(_argv):
    db = _db()
    rows = db.list_users()
    if not rows:
        print("Пользователей нет. Заведите первого: useradd <email> <пароль> --admin")
        return 0
    for r in rows:
        n = len(db.owned_logins(r["id"]))
        flag = "" if r["active"] else "  [заблокирован]"
        print("#{:<3} {:<6} {:<28} {}  клиентов: {}{}".format(
            r["id"], r["role"], r["email"], r["name"] or "", n, flag))
    return 0


def passwd(argv):
    if len(argv) < 2:
        print("Использование: passwd <email> <новый-пароль>")
        return 2
    db = _db()
    u = db.get_user_by_email(argv[0])
    if not u:
        print("Нет такого пользователя: {}".format(argv[0]))
        return 1
    db.set_user_password(u["id"], auth.hash_password(argv[1]))
    print("Пароль обновлён: {}".format(argv[0]))
    return 0


def set_active(argv, active):
    if not argv:
        print("Использование: {} <email>".format("useron" if active else "useroff"))
        return 2
    db = _db()
    u = db.get_user_by_email(argv[0])
    if not u:
        print("Нет такого пользователя: {}".format(argv[0]))
        return 1
    db.set_user_active(u["id"], active)
    print("{}: {}".format("разблокирован" if active else "заблокирован", argv[0]))
    return 0


def assign(argv):
    if len(argv) < 2:
        print("Использование: assign <email> <login> [<login> ...]")
        return 2
    db = _db()
    u = db.get_user_by_email(argv[0])
    if not u:
        print("Нет такого пользователя: {}".format(argv[0]))
        return 1
    n = 0
    for login in argv[1:]:
        if not db.get_client(login):
            print("  пропуск (нет клиента): {}".format(login))
            continue
        db.set_client_owner(login, u["id"])
        n += 1
    print("Назначено клиентов {} → {}: {}".format(u["email"], n, ", ".join(argv[1:])))
    return 0
