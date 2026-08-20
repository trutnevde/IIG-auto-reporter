# -*- coding: utf-8 -*-
"""Ночная копия базы наружу: Telegram, Яндекс.Диск или Google.

Зачем отдельно от суточной копии. Та кладёт файл рядом с базой, на тот же диск
и в тот же аккаунт хостинга: она спасает от нашей ошибки (снесли данные, откатили),
но не от потери сервера или блокировки аккаунта. Здесь копия уходит наружу.

Как снимается. Через встроенное копирование SQLite (`Connection.backup`), а не
`cp`: база работает в режиме WAL, и простое копирование файла даёт неконсистентный
снимок, если в этот момент кто-то пишет. Дальше файл сжимается — 2,46 МБ базы
превращаются в 0,30 МБ.

Куда. Выбирается ключом backup_target в app_config:

  telegram  Документом в личный чат. Работает без единого нового доступа: бот и
            его токен уже есть. Ротации нет — бот умеет удалять только свои свежие
            сообщения, поэтому копии просто копятся в чате. При 0,3 МБ в сутки это
            около 110 МБ в год, чат столько держит.
  yandex    На Яндекс.Диск по WebDAV. Нужен пароль приложения: yandex_disk_login и
            yandex_disk_password в secrets.json. Ротация полноценная.
  gdrive    На «Общий диск» организации. Именно общий, а не папка на личном диске:
            своего места на Диске сервисным аккаунтам Google не выделяет вовсе, и
            загрузка в обычную папку отбивается ошибкой про квоту (проверено).
            Общий диск требует Google Workspace.

Нет настройки — задача не выполняется и говорит об этом, а не падает.
"""
import datetime as _dt
import gzip
import io
import os
import re
import shutil
import sqlite3
import tempfile

import requests

from . import gsheets, settings

# drive.file — только свои файлы. Полный drive тут не нужен и опасен: тем же
# ключом раскладываются отчёты клиентов, и задача копирования не должна их видеть.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
# supportsAllDrives обязателен: копия кладётся на общий диск, туда без флага не пускают.
UPLOAD_URL = ("https://www.googleapis.com/upload/drive/v3/files"
              "?uploadType=multipart&supportsAllDrives=true")
FILES_URL = "https://www.googleapis.com/drive/v3/files"
WEBDAV = "https://webdav.yandex.ru"
PREFIX = "iigbot_"
TIMEOUT = 120


# ─────────────────────────── снимок ───────────────────────────
def snapshot(db_path, dest):
    """Согласованная сжатая копия базы. Работает и когда в базу пишут."""
    raw = dest + ".tmp"
    src = sqlite3.connect("file:{}?mode=ro".format(db_path), uri=True)
    try:
        dst = sqlite3.connect(raw)
        try:
            src.backup(dst)          # штатное копирование SQLite: снимок целостный
        finally:
            dst.close()
    finally:
        src.close()
    with open(raw, "rb") as fi, gzip.open(dest, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo)
    os.remove(raw)
    return os.path.getsize(dest)


def _short(t):
    return " ".join(str(t or "").split())[:180]


def _json_bytes(obj):
    import json
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


# ─────────────────────────── Telegram ───────────────────────────
def _send_telegram(path, chat_id, token, note):
    with open(path, "rb") as f:
        r = requests.post("https://api.telegram.org/bot{}/sendDocument".format(token),
                          data={"chat_id": str(chat_id), "caption": note[:1000],
                                "disable_notification": "true"},
                          files={"document": (os.path.basename(path), f, "application/gzip")},
                          timeout=TIMEOUT)
    try:
        j = r.json()
    except ValueError:
        j = {}
    if not j.get("ok"):
        raise RuntimeError("Telegram не принял файл: {}"
                           .format(_short(j.get("description") or r.text)))
    return str((j.get("result") or {}).get("message_id") or "")


# ─────────────────────── Яндекс.Диск (WebDAV) ───────────────────────
def _yandex_auth(sec):
    login = (sec.get("yandex_disk_login") or "").strip()
    pw = (sec.get("yandex_disk_password") or "").strip()
    if not (login and pw):
        raise RuntimeError("Нет доступа к Яндекс.Диску: заведи пароль приложения и положи "
                           "yandex_disk_login и yandex_disk_password в secrets.json")
    return (login, pw)


def _yandex_upload(path, folder, auth):
    # MKCOL на уже существующую папку отвечает 405 — это не ошибка, просто она есть
    requests.request("MKCOL", "{}/{}".format(WEBDAV, folder.strip("/")),
                     auth=auth, timeout=TIMEOUT)
    url = "{}/{}/{}".format(WEBDAV, folder.strip("/"), os.path.basename(path))
    with open(path, "rb") as f:
        r = requests.put(url, data=f, auth=auth, timeout=TIMEOUT)
    if r.status_code >= 300:
        raise RuntimeError("Яндекс.Диск не принял файл ({}): {}"
                           .format(r.status_code, _short(r.text)))
    return os.path.basename(path)


def _yandex_rotate(folder, auth, keep):
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:"><d:prop><d:displayname/></d:prop></d:propfind>')
    r = requests.request("PROPFIND", "{}/{}".format(WEBDAV, folder.strip("/")), auth=auth,
                         data=body.encode("utf-8"), timeout=TIMEOUT,
                         headers={"Depth": "1", "Content-Type": "application/xml"})
    if r.status_code >= 300:
        raise RuntimeError("Не прочитался список копий ({})".format(r.status_code))
    names = sorted({n for n in re.findall(r"<d:displayname>([^<]+)</d:displayname>", r.text)
                    if n.startswith(PREFIX)}, reverse=True)
    dropped = []
    for n in names[int(keep):]:
        d = requests.delete("{}/{}/{}".format(WEBDAV, folder.strip("/"), n),
                            auth=auth, timeout=TIMEOUT)
        if d.status_code < 300:
            dropped.append(n)
    return len(names), dropped


# ─────────────────────────── Google ───────────────────────────
def _token():
    from google.auth.transport.requests import Request
    from google.oauth2.service_account import Credentials
    p = gsheets.key_path()
    if not p:
        raise RuntimeError("sa_key.json не найден — выгрузка на Google невозможна")
    creds = Credentials.from_service_account_file(p, scopes=SCOPES)
    creds.refresh(Request())
    return creds.token


def _gdrive_upload(path, folder_id, token):
    meta = {"name": os.path.basename(path), "parents": [folder_id]}
    with open(path, "rb") as f:
        blob = f.read()
    files = {
        "metadata": ("metadata", io.BytesIO(_json_bytes(meta)), "application/json; charset=UTF-8"),
        "file": (os.path.basename(path), io.BytesIO(blob), "application/gzip"),
    }
    r = requests.post(UPLOAD_URL, headers={"Authorization": "Bearer " + token},
                      files=files, timeout=TIMEOUT)
    if r.status_code >= 300:
        t = r.text or ""
        if "storage quota" in t:
            raise RuntimeError(
                "У сервисного аккаунта нет своего места на Google-диске — это правило Google, "
                "обойти его нельзя. Нужен «Общий диск» организации (только с Google Workspace): "
                "создать общий диск, добавить туда сервисный аккаунт как «Организатор контента» "
                "и указать папку оттуда. Обычная папка на личном диске не подойдёт.")
        raise RuntimeError("Google-диск не принял файл ({}): {}".format(r.status_code, _short(t)))
    return (r.json() or {}).get("id")


def _gdrive_rotate(folder_id, token, keep):
    r = requests.get(FILES_URL, timeout=TIMEOUT,
                     headers={"Authorization": "Bearer " + token},
                     params={"q": "'{}' in parents and name contains '{}' and trashed=false"
                                  .format(folder_id, PREFIX),
                             "fields": "files(id,name,createdTime)",
                             "orderBy": "createdTime desc", "pageSize": 200,
                             "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
                             "corpora": "allDrives"})
    if r.status_code >= 300:
        raise RuntimeError("Не прочитался список копий ({}): {}"
                           .format(r.status_code, _short(r.text)))
    files = (r.json() or {}).get("files") or []
    dropped = []
    for f in files[int(keep):]:
        d = requests.delete("{}/{}".format(FILES_URL, f["id"]), timeout=TIMEOUT,
                            params={"supportsAllDrives": "true"},
                            headers={"Authorization": "Bearer " + token})
        if d.status_code < 300:
            dropped.append(f["name"])
    return len(files), dropped


# ─────────────────────────── запуск ───────────────────────────
def run(cfg=None, keep=None):
    """Снять копию и увезти наружу. Возвращает словарь для журнала."""
    from .settings import load_secrets
    cfg = cfg or settings.load_app_config()
    target = (cfg.get("backup_target") or "").strip().lower()
    if not target:
        return {"ok": False, "skipped": True,
                "note": "адресат копии не выбран (app_config.backup_target: "
                        "telegram, yandex или gdrive)"}
    keep = int(keep if keep is not None
               else cfg.get("backup_keep") or cfg.get("gdrive_backup_keep") or 30)

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M")
    tmp = os.path.join(tempfile.gettempdir(), "{}{}.sqlite3.gz".format(PREFIX, stamp))
    try:
        size = snapshot(cfg["db_path"], tmp)
        name = os.path.basename(tmp)
        mb = size / 1048576.0

        if target == "telegram":
            chat = str(cfg.get("backup_telegram_chat") or "").strip()
            if not chat:
                ids = cfg.get("admin_user_ids") or []
                chat = str(ids[0]) if ids else ""
            if not chat:
                raise RuntimeError("Не указан чат для копии (app_config.backup_telegram_chat)")
            note = "Копия базы {} за {}, {:.2f} МБ".format(
                name, _dt.datetime.now().strftime("%d.%m.%Y %H:%M"), mb)
            mid = _send_telegram(tmp, chat, load_secrets()["telegram_bot_token"], note)
            return {"ok": True, "target": target, "file": name, "id": mid, "size": size,
                    "note": "копия {} ({:.2f} МБ) отправлена в Telegram".format(name, mb)}

        if target == "yandex":
            auth = _yandex_auth(load_secrets())
            folder = (cfg.get("backup_folder") or "IIG-Reporter-backups").strip()
            _yandex_upload(tmp, folder, auth)
            total, dropped = _yandex_rotate(folder, auth, keep)
            return {"ok": True, "target": target, "file": name, "size": size,
                    "kept": min(total, keep), "dropped": len(dropped),
                    "note": "копия {} ({:.2f} МБ) на Яндекс.Диске, всего копий {}, "
                            "удалено лишних {}".format(name, mb, min(total, keep), len(dropped))}

        if target == "gdrive":
            folder = (cfg.get("gdrive_backup_folder") or "").strip()
            if not folder:
                raise RuntimeError("Не указана папка общего диска "
                                   "(app_config.gdrive_backup_folder)")
            token = _token()
            file_id = _gdrive_upload(tmp, folder, token)
            total, dropped = _gdrive_rotate(folder, token, keep)
            return {"ok": True, "target": target, "file": name, "id": file_id, "size": size,
                    "kept": min(total, keep), "dropped": len(dropped),
                    "note": "копия {} ({:.2f} МБ) на Google-диске, всего копий {}, "
                            "удалено лишних {}".format(name, mb, min(total, keep), len(dropped))}

        raise RuntimeError("Неизвестный адресат копии: {}".format(target))
    finally:
        for p in (tmp, tmp + ".tmp"):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
