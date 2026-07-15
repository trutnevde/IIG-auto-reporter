# -*- coding: utf-8 -*-
"""Загрузка/сохранение секретов и настроек.

Работает в двух режимах:
  * обычный запуск (python -m iigbot ...): secrets.json/config.json берём из корня репозитория,
    app_config.json и база — из папки app/;
  * собранный .exe (PyInstaller, sys.frozen): все файлы (secrets.json, config.json,
    app_config.json, база) лежат РЯДОМ с .exe — это удобно для пользователя без Python.
"""
import os
import sys
import json

FROZEN = getattr(sys, "frozen", False)

PKG_DIR = os.path.dirname(os.path.abspath(__file__))      # .../iigbot
_APP_DIR_DEV = os.path.dirname(PKG_DIR)                   # .../app
_REPO_ROOT_DEV = os.path.dirname(_APP_DIR_DEV)            # корень репозитория

# Папка, где приложение ищет секреты/конфиги/базу.
BASE_DIR = os.path.dirname(sys.executable) if FROZEN else _APP_DIR_DEV


def _load(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _first_existing(*paths):
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


def _secrets_candidates():
    if FROZEN:
        return [os.path.join(BASE_DIR, "secrets.json")]
    return [os.path.join(_REPO_ROOT_DEV, "secrets.json"), os.path.join(_APP_DIR_DEV, "secrets.json")]


def _report_candidates():
    if FROZEN:
        return [os.path.join(BASE_DIR, "config.json")]
    return [os.path.join(_REPO_ROOT_DEV, "config.json")]


def _app_config_path():
    return os.path.join(BASE_DIR, "app_config.json")


def package_file(name):
    """Путь к файлу, поставляемому ВНУТРИ приложения (например, ui.html).

    В onefile-сборке данные распаковываются в sys._MEIPASS/iigbot/.
    """
    if FROZEN:
        base = getattr(sys, "_MEIPASS", PKG_DIR)
        for p in (os.path.join(base, "iigbot", name), os.path.join(base, name), os.path.join(PKG_DIR, name)):
            if os.path.isfile(p):
                return p
    return os.path.join(PKG_DIR, name)


def ensure_ca_bundle():
    """Делает CA-сертификат для HTTPS независимым от временной _MEI-папки PyInstaller.

    В onefile-сборке requests/certifi берёт cacert.pem из sys._MEIPASS (…\\Temp\\_MEI…).
    Эту папку удаляет родительский/прежний процесс при закрытии окна — и тогда HTTPS падает
    с OSError («Could not find a suitable TLS CA certificate bundle»), а фоновый слушатель
    тихо умирает. Копируем бандл ОДИН раз рядом с программой (BASE_DIR) и указываем на
    стабильную копию через переменные окружения — их наследует и дочерний процесс-слушатель.
    Копия рядом с exe не удаляется никогда. В обычном Python ничего не трогаем.
    """
    if not FROZEN:
        return None
    stable = os.path.join(BASE_DIR, "cacert.pem")
    src = None
    try:
        import certifi
        src = certifi.where()
    except Exception:  # noqa: BLE001
        src = None
    if not (src and os.path.isfile(src)):
        meipass = getattr(sys, "_MEIPASS", None)
        cand = os.path.join(meipass, "certifi", "cacert.pem") if meipass else None
        src = cand if (cand and os.path.isfile(cand)) else None
    if src:  # обновляем стабильную копию, пока _MEI ещё жив
        try:
            with open(src, "rb") as fi, open(stable, "wb") as fo:
                fo.write(fi.read())
        except Exception:  # noqa: BLE001
            pass
    if os.path.isfile(stable):
        os.environ["REQUESTS_CA_BUNDLE"] = stable   # requests
        os.environ["CURL_CA_BUNDLE"] = stable        # requests (резерв)
        os.environ["SSL_CERT_FILE"] = stable         # ssl/urllib
        return stable
    return None


def load_secrets():
    path = _first_existing(*_secrets_candidates())
    if not path:
        where = BASE_DIR if FROZEN else "рядом с weekly_report.ps1"
        raise FileNotFoundError(
            "secrets.json не найден ({}). Скопируй secrets.example.json в secrets.json и впиши токены.".format(where)
        )
    return _load(path)


SECRETS_TEMPLATE = {
    "telegram_bot_token": "ВСТАВЬ_ТОКЕН_БОТА_от_BotFather",
    "yandex_oauth_token": "ВСТАВЬ_OAUTH_ТОКЕН_ЯНДЕКС_ДИРЕКТА",
}


def ensure_secrets_template():
    """Если secrets.json рядом с программой нет — создаём шаблон с подсказками.
    Так пользователь сразу видит, какой файл заполнить, а не получает падение «не найден»."""
    path = _secrets_candidates()[0]
    if not os.path.isfile(path):
        try:
            _save(path, SECRETS_TEMPLATE)
        except Exception:
            pass
    return path


def load_app_config():
    path = _first_existing(_app_config_path())
    cfg = _load(path) if path else {}
    cfg.setdefault("admin_user_ids", [])
    cfg.setdefault("poll_timeout", 25)
    cfg.setdefault("announce_on_join", True)
    cfg.setdefault("web_port", 8077)
    cfg.setdefault("report_day", "Понедельник")
    cfg.setdefault("report_time", "09:00")
    cfg.setdefault("db_path", "iigbot.sqlite3")
    if not os.path.isabs(cfg["db_path"]):
        cfg["db_path"] = os.path.join(BASE_DIR, cfg["db_path"])
    return cfg


def load_report_config():
    path = _first_existing(*_report_candidates())
    return _load(path) if path else {}


def _save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_app_config(patch):
    path = _app_config_path()
    cur = _load(path) if os.path.isfile(path) else {}
    cur.update(patch)
    _save(path, cur)
    return cur


def save_report_config(patch):
    path = _report_candidates()[0]
    cur = _load(path) if os.path.isfile(path) else {}
    cur.update(patch)
    _save(path, cur)
    return cur


def save_secrets(patch):
    """Записывает/обновляет токены в secrets.json рядом с программой (мержит с существующими)."""
    path = _secrets_candidates()[0]
    cur = {}
    if os.path.isfile(path):
        try:
            cur = _load(path)
        except Exception:  # noqa: BLE001
            cur = {}
    for k, v in (patch or {}).items():
        if v is not None:
            cur[k] = v
    _save(path, cur)
    return path


ERROR_LOG_PATH = os.path.join(BASE_DIR, "iig_errors.log")


def log_error(where, message):
    """Дописывает ошибку в iig_errors.log рядом с базой (BASE_DIR). Нужно, чтобы сбои —
    особенно в авто-рассылке на сервере, где консоли не видно, — можно было потом разобрать
    по SFTP. Файл под _app/ (Require all denied), по вебу недоступен. Сбои самой записи глотаем."""
    try:
        import datetime
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = "{}\t{}\t{}\n".format(stamp, where, str(message).replace("\n", " ").replace("\t", " "))
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        pass
