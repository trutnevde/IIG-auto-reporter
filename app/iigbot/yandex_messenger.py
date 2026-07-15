# -*- coding: utf-8 -*-
"""Тонкий клиент Bot API Яндекс Мессенджера (Яндекс 360 для бизнеса) на requests.

Зеркало telegram_api.Telegram: тот же контракт send_message(chat_id, text), чтобы рассылка
слала в оба канала одинаково. Токен бота создаёт админ организации в admin.yandex.ru/bot-platform
(показывается один раз). Россия-нативно, без VPN.

Docs: https://yandex.ru/dev/messenger/doc/ru/
Отправка: POST botapi.messenger.yandex.net/bot/v1/messages/sendText/  (Authorization: OAuth <token>)
Лимит текста — 6000 символов (у Telegram 4096).
"""
import time
import requests

YA_HARD_LIMIT = 6000
BASE = "https://botapi.messenger.yandex.net/bot/v1/"


class YMessengerError(Exception):
    pass


def split_text(text, limit=5800):
    """Бьём длинный текст под лимит Яндекс Мессенджера (6000) по строкам — как в telegram_api."""
    text = (text or "").replace("\r\n", "\n")
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            if cur:
                parts.append(cur)
            cur = line
        else:
            cur = line if not cur else cur + "\n" + line
    if cur:
        parts.append(cur)
    return parts


class YMessenger:
    """Клиент бота Яндекс Мессенджера. Интерфейс send_message(chat_id, text) совместим с
    telegram_api.Telegram — рассылка выбирает канал по чату, не меняя логику отчётов."""

    def __init__(self, token, timeout=25, session=None):
        self.token = token
        self.timeout = timeout
        self.s = session or requests.Session()
        self.s.headers.update({"Authorization": "OAuth " + (token or ""),
                               "Content-Type": "application/json"})

    def _call(self, method, params=None, retries=4):
        url = BASE + method
        last = None
        for attempt in range(retries):
            try:
                r = self.s.post(url, json=params or {}, timeout=self.timeout + 15)
            except (requests.RequestException, OSError) as e:
                last = "сеть: {}".format(e)
                time.sleep(min(2 ** attempt, 10))
                continue
            try:
                data = r.json()
            except ValueError:
                last = "не-JSON ответ (HTTP {})".format(r.status_code)
                time.sleep(min(2 ** attempt, 10))
                continue
            if isinstance(data, dict) and data.get("ok"):
                return data
            if r.status_code == 429:
                retry_after = (data.get("parameters") or {}).get("retry_after", 1) if isinstance(data, dict) else 1
                last = "429 (retry_after={})".format(retry_after)
                time.sleep(retry_after + 0.5)
                continue
            desc = data.get("description") or data if isinstance(data, dict) else data
            raise YMessengerError("{}: HTTP {} {}".format(method, r.status_code, desc))
        raise YMessengerError("{}: не удалось ({})".format(method, last))

    def send_message(self, chat_id, text, login=None, parse_mode=None, reply_markup=None):
        """Отправить текст в групповой чат (chat_id) ИЛИ в личку (login). Первым аргументом
        chat_id — как у telegram_api. Длинный текст бьётся под лимит 6000."""
        result = None
        parts = split_text(text)
        for i, part in enumerate(parts):
            params = {"text": part}
            if login:
                params["login"] = login
            else:
                params["chat_id"] = str(chat_id)
            result = self._call("messages/sendText/", params)
            if len(parts) > 1:
                time.sleep(0.4)
        return result

    def send_to_login(self, login, text):
        """Отправить в приватный чат по логину пользователя (без известного chat_id)."""
        return self.send_message(None, text, login=login)
