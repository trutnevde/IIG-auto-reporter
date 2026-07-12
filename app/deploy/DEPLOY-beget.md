# Развёртывание веб-кабинета на Beget (виртуальный хостинг)

Боевой инстанс: **https://reports.iig.ru** (аккаунт `adolmax0`, сервер geist).
Мультиагентство: один Яндекс-токен/бот/Google-ключ, аккаунты по приглашению, каждый видит своих клиентов.

## Раскладка на сервере
```
~/reports.iig.ru/public_html/
├── passenger_wsgi.py        # точка входа Passenger (импортирует iigbot.web.create_app)
├── .htaccess                # конфиг Passenger (см. ниже)
├── tmp/restart.txt          # touch — перезапуск приложения
└── _app/                    # ПРИВАТНО (.htaccess = Require all denied, по вебу 403)
    ├── .htaccess            # "Require all denied"
    ├── venv/                # virtualenv (см. про --copies ниже)
    ├── secrets.json         # токены Яндекс+бот  (chmod 600)
    ├── sa_key.json          # ключ Google
    └── app/
        ├── app_config.json  # admin_user_ids, db_path и пр.
        ├── iigbot.sqlite3   # база (клиенты/привязки/пользователи)
        └── iigbot/          # код пакета
```
Всё чувствительное — под `public_html/_app/`, закрыто `.htaccess`. Приложение читает `secrets.json`/
`sa_key.json` из `_app/`, а базу/`app_config.json` — из `_app/app/` (см. `settings.py`).

## public_html/.htaccess
```
PassengerEnabled on
PassengerAppRoot /home/a/adolmax0/reports.iig.ru/public_html
PassengerBaseURI /
PassengerStartupFile passenger_wsgi.py
PassengerAppType wsgi
PassengerPython /usr/bin/python3
```

## Грабли Beget (важно)
- `python3 -m venv` НЕ работает (нет ensurepip). Ставить venv так:
  `python3 -m pip install --user virtualenv && python3 -m virtualenv --copies ~/…/_app/venv`
  (`--copies` обязателен: симлинк на `/usr/bin/python3` в джейле запрещён).
- Passenger НЕ заводит venv-python. Поэтому `PassengerPython /usr/bin/python3` (системный),
  а пакеты venv подключаются в `passenger_wsgi.py` через `sys.path`/`site.addsitedir`.
- Приложение исполняется под ОТДЕЛЬНЫМ пользователем сайта (не ssh-юзер). Доступ к файлам даёт
  default-ACL, который Beget вешает на `public_html` → всё держать ВНУТРИ `public_html`
  (`setfacl` вне дерева сайта запрещён).

## Обновление кода
Залить файлы в `_app/app/iigbot/`, затем `touch ~/reports.iig.ru/public_html/tmp/restart.txt`.

## Бот — webhook (не long-polling)
```
python -m iigbot webhook set https://reports.iig.ru/tg/webhook
```
Один токен на агентство → webhook и long-polling взаимоисключающи. На сервере — webhook,
десктоп-слушатель НЕ запускать. Откат: `python -m iigbot webhook delete`.

## Еженедельная рассылка (cron в панели Beget)
`crontab` в консоли отсутствует — задача заводится в панели Beget → Cron.
Команда (Пн 09:00):
```
cd ~/reports.iig.ru/public_html/_app/app && PYTHONPATH=/home/a/adolmax0/reports.iig.ru/public_html/_app/venv/lib/python3.10/site-packages /usr/bin/python3 -m iigbot weekly
```
Безопасная проверка без отправки клиентам — добавить `--dry` в конец команды.
