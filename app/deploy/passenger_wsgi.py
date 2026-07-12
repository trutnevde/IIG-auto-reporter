# -*- coding: utf-8 -*-
# Точка входа Passenger (кладётся в ~/reports.iig.ru/public_html/passenger_wsgi.py).
# Passenger запускается системным python3, пакеты venv и код приложения подключаем в sys.path.
# Пути подставлены под боевой аккаунт adolmax0 — при другом аккаунте/домене поправить BASE.
import sys, site
BASE = "/home/a/adolmax0/reports.iig.ru/public_html/_app"
VENV_SP = BASE + "/venv/lib/python3.10/site-packages"
sys.path.insert(0, VENV_SP)
site.addsitedir(VENV_SP)
APP_DIR = BASE + "/app"
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
from iigbot.web import create_app
application = create_app()
