# -*- coding: utf-8 -*-
"""Тонкий клиент Telegram Bot API на requests. Long-polling, без вебхуков —
работает на обычном ПК без белого IP и хостинга.
"""
import time
import requests

TG_HARD_LIMIT = 4096


class TelegramError(Exception):
    pass


def split_text(text, limit=4000):
    """Бьём длинный текст под лимит Telegram (4096) по строкам. Логика — как в weekly_report.ps1."""
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


class Telegram:
    def __init__(self, token, timeout=25, session=None):
        self.token = token
        self.base = "https://api.telegram.org/bot{}/".format(token)
        self.timeout = timeout
        self.s = session or requests.Session()

    def _call(self, method, params=None, http_timeout=None, retries=4):
        url = self.base + method
        last = None
        for attempt in range(retries):
            try:
                r = self.s.post(url, json=params or {}, timeout=http_timeout or (self.timeout + 15))
            except (requests.RequestException, OSError) as e:
                # OSError ловим тоже: при битом пути к CA-бандлу requests кидает именно его —
                # без этого слушатель падал насмерть. Здесь же он станет повторяемой ошибкой.
                last = "сеть: {}".format(e)
                time.sleep(min(2 ** attempt, 10))
                continue
            try:
                data = r.json()
            except ValueError:
                last = "не-JSON ответ (HTTP {})".format(r.status_code)
                time.sleep(min(2 ** attempt, 10))
                continue
            if data.get("ok"):
                return data.get("result")
            if r.status_code == 429:
                retry_after = (data.get("parameters") or {}).get("retry_after", 1)
                last = "429 (retry_after={})".format(retry_after)
                time.sleep(retry_after + 0.5)
                continue
            raise TelegramError("{}: {} {}".format(method, data.get("error_code"), data.get("description")))
        raise TelegramError("{}: не удалось ({})".format(method, last))

    def get_me(self):
        return self._call("getMe", retries=2)

    def get_updates(self, offset=None, allowed_updates=None, timeout=None):
        t = self.timeout if timeout is None else timeout
        params = {"timeout": t}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates is not None:
            params["allowed_updates"] = allowed_updates
        # http_timeout должен превышать серверный long-poll timeout; retries=1 —
        # обычные таймауты опроса разруливает цикл бота.
        return self._call("getUpdates", params, http_timeout=t + 15, retries=1)

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        result = None
        parts = split_text(text)
        for i, part in enumerate(parts):
            params = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
            if parse_mode:
                params["parse_mode"] = parse_mode
            if reply_markup and i == len(parts) - 1:
                params["reply_markup"] = reply_markup
            result = self._call("sendMessage", params)
            if len(parts) > 1:
                time.sleep(0.4)
        return result

    def get_chat_administrators(self, chat_id):
        return self._call("getChatAdministrators", {"chat_id": chat_id}, retries=2)
