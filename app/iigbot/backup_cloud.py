# -*- coding: utf-8 -*-
"""Ночная копия базы на Google-диск.

Зачем отдельно от суточной копии. Та кладёт файл рядом с базой, на тот же диск
и в тот же аккаунт хостинга: она спасает от нашей ошибки (снесли данные, откатили),
но не от потери сервера или блокировки аккаунта. Здесь копия уходит наружу.

Как снимается. Через встроенное копирование SQLite (`Connection.backup`), а не
`cp`: база работает в режиме WAL, и простое копирование файла даёт неконсистентный
снимок, если в этот момент кто-то пишет. Дальше файл сжимается — база из мегабайтов
текста жмётся примерно вчетверо.

Куда кладётся. В папку Google-диска, которую расшарили на сервисный аккаунт
(тот же ключ, что раскладывает отчёты по таблицам клиентов). Идентификатор папки
лежит в app_config под ключом gdrive_backup_folder. Нет ключа — задача просто
не выполняется и говорит об этом, а не падает.

Права. Берём узкую область drive.file: она даёт доступ только к тем файлам,
которые создало само приложение. Таблицы клиентов этой задаче недоступны.
"""
import datetime as _dt
import gzip
import io
import os
import shutil
import sqlite3
import tempfile

import requests

from . import gsheets, settings

# drive.file — только свои файлы. Полный drive тут не нужен и опасен: тем же
# ключом раскладываются отчёты клиентов, и задача копирования не должна их видеть.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
PREFIX = "iigbot_"
TIMEOUT = 120


def _token():
    """Токен сервисного аккаунта. Ключ тот же, что у таблиц."""
    from google.auth.transport.requests import Request
    from google.oauth2.service_account import Credentials
    p = gsheets.key_path()
    if not p:
        raise RuntimeError("sa_key.json не найден — выгрузка наружу невозможна")
    creds = Credentials.from_service_account_file(p, scopes=SCOPES)
    creds.refresh(Request())
    return creds.token


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


def _upload(path, folder_id, token):
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
        raise RuntimeError("Google-диск не принял файл ({}): {}"
                           .format(r.status_code, _short(r.text)))
    return (r.json() or {}).get("id")


def _rotate(folder_id, token, keep):
    """Лишние копии удаляем сами: место у сервисного аккаунта не бесконечное."""
    r = requests.get(FILES_URL, timeout=TIMEOUT,
                     headers={"Authorization": "Bearer " + token},
                     params={"q": "'{}' in parents and name contains '{}' and trashed=false"
                                  .format(folder_id, PREFIX),
                             "fields": "files(id,name,createdTime)",
                             "orderBy": "createdTime desc", "pageSize": 200})
    if r.status_code >= 300:
        raise RuntimeError("Не прочитался список копий ({}): {}"
                           .format(r.status_code, _short(r.text)))
    files = (r.json() or {}).get("files") or []
    dropped = []
    for f in files[int(keep):]:
        d = requests.delete("{}/{}".format(FILES_URL, f["id"]), timeout=TIMEOUT,
                            headers={"Authorization": "Bearer " + token})
        if d.status_code < 300:
            dropped.append(f["name"])
    return len(files), dropped


def _json_bytes(obj):
    import json
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _short(t):
    t = " ".join(str(t or "").split())
    return t[:180]


def run(cfg=None, keep=None):
    """Снять копию и увезти на Google-диск. Возвращает словарь для журнала."""
    cfg = cfg or settings.load_app_config()
    folder = (cfg.get("gdrive_backup_folder") or "").strip()
    if not folder:
        return {"ok": False, "skipped": True,
                "note": "папка на Google-диске не задана (app_config.gdrive_backup_folder)"}
    keep = int(keep if keep is not None else cfg.get("gdrive_backup_keep") or 30)

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M")
    tmp = os.path.join(tempfile.gettempdir(), "{}{}.sqlite3.gz".format(PREFIX, stamp))
    try:
        size = snapshot(cfg["db_path"], tmp)
        token = _token()
        file_id = _upload(tmp, folder, token)
        total, dropped = _rotate(folder, token, keep)
        return {"ok": True, "file": os.path.basename(tmp), "id": file_id,
                "size": size, "kept": min(total, keep), "dropped": len(dropped),
                "note": "копия {} ({:.2f} МБ) на Google-диске, всего копий {}, удалено лишних {}"
                        .format(os.path.basename(tmp), size / 1048576.0,
                                min(total, keep), len(dropped))}
    finally:
        for p in (tmp, tmp + ".tmp"):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
