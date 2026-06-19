# -*- coding: utf-8 -*-
"""IIG Telegram-бот: обнаружение чатов и привязка их к клиентам Яндекс.Директа.

Пакет — основа для постепенного перехода с PowerShell на Python:
  * iigbot.bot            — бот-слушатель (long-polling), определяет, где он находится;
  * iigbot.storage        — локальная база (SQLite): чаты, клиенты, привязки;
  * iigbot.yandex         — чтение списка клиентов из агентского аккаунта Директа;
  * iigbot.sync_clients   — CLI: подтянуть клиентов из Директа;
  * iigbot.import_config  — CLI: перенести текущий config.json в базу;
  * iigbot.web            — мини веб-админка для привязок.
"""

__version__ = "0.1.0"
