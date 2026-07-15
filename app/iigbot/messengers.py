# -*- coding: utf-8 -*-
"""Маршрутизатор доставки: единый интерфейс send(chat, text), но мессенджер выбирается по
полю chat.channel. Так рассылка/отчёты не знают про каналы — отдают чат и текст, а роутер
шлёт в Telegram или Яндекс Мессенджер. Отсутствующий канал (нет токена) — понятная ошибка."""


def _get(row, key, default=None):
    try:
        return row[key]
    except Exception:  # noqa: BLE001 — sqlite Row без колонки / dict без ключа
        return default


class Messengers:
    def __init__(self, tg=None, ym=None):
        self.tg = tg   # telegram_api.Telegram | None
        self.ym = ym   # yandex_messenger.YMessenger | None

    @staticmethod
    def channel_of(chat):
        return (_get(chat, "channel") or "telegram")

    def has(self, channel):
        return bool(self.ym) if channel == "ymessenger" else bool(self.tg)

    def send(self, chat, text):
        """Отправить текст в чат нужным мессенджером (по chat.channel)."""
        ch = self.channel_of(chat)
        if ch == "ymessenger":
            if not self.ym:
                raise RuntimeError("Яндекс Мессенджер не подключён — нет токена бота в secrets.json")
            return self.ym.send_message(_get(chat, "ext_id"), text)
        if not self.tg:
            raise RuntimeError("Telegram не подключён — нет токена бота")
        return self.tg.send_message(_get(chat, "chat_id"), text)
