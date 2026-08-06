# -*- coding: utf-8 -*-
"""Общий HTTP-клиент для внешних сервисов: таймауты, ретраи, понятные ошибки.

Раньше каждый модуль ходил в сеть по-своему: у Яндекса и Метрики не было ни
одного повтора (разовый обрыв сети ронял операцию целиком), у Google-таблиц —
ни одного таймаута (зависший вызов держал единственный процесс кабинета
бесконечно), а сообщения об ошибках у всех были разные.

Здесь всё это в одном месте:
  * таймаут по умолчанию на каждый запрос;
  * повторы с растущей паузой на сетевых сбоях и кодах 429/500/502/503/504;
  * уважение заголовка Retry-After, если сервис его прислал;
  * кодировка ответа: если сервер не назвал её, читаем как UTF-8, иначе
    русский текст ошибки превращается в кракозябры;
  * единый текст исключения, по которому понятно, кто именно отказал.
"""
import random
import time

import requests

DEFAULT_TIMEOUT = 60
DEFAULT_RETRIES = 3
# коды, при которых повтор осмыслен: перегрузка или временная неисправность
RETRY_CODES = (429, 500, 502, 503, 504)


class ServiceError(RuntimeError):
    """Внешний сервис отказал. status=None — до сервиса вообще не достучались."""

    def __init__(self, service, message, status=None, body=""):
        super(ServiceError, self).__init__(message)
        self.service = service
        self.status = status
        self.body = body


def _fix_encoding(resp):
    """requests при отсутствии charset читает текст как latin-1 — чиним."""
    try:
        enc = (resp.encoding or "").lower()
        if not enc or enc in ("iso-8859-1", "latin-1"):
            resp.encoding = "utf-8"
    except Exception:  # noqa: BLE001
        pass
    return resp


def _sleep_for(attempt, resp, base):
    """Пауза перед повтором: сервис попросил — слушаемся, иначе растим сами."""
    if resp is not None:
        hdr = resp.headers.get("Retry-After") or resp.headers.get("retryIn")
        try:
            if hdr:
                return max(1.0, min(30.0, float(hdr)))
        except (TypeError, ValueError):
            pass
    # 1, 2, 4 секунды плюс разброс, чтобы параллельные вызовы не били в такт
    return min(30.0, base * (2 ** attempt)) + random.random() * 0.4


def request(service, method, url, retries=DEFAULT_RETRIES, timeout=DEFAULT_TIMEOUT,
            backoff=1.0, retry_codes=RETRY_CODES, session=None, _sleep=None, **kw):
    """Запрос с повторами. Возвращает Response; на исчерпании попыток — ServiceError."""
    sleep = _sleep or time.sleep
    send = (session or requests).request
    kw.setdefault("timeout", timeout)
    last_err = None
    for attempt in range(max(1, retries)):
        try:
            resp = _fix_encoding(send(method, url, **kw))
        except requests.RequestException as e:      # сеть, DNS, таймаут
            last_err = e
            if attempt == retries - 1:
                raise ServiceError(service, "{}: нет связи ({})".format(service, e))
            sleep(_sleep_for(attempt, None, backoff))
            continue
        if resp.status_code in retry_codes and attempt < retries - 1:
            sleep(_sleep_for(attempt, resp, backoff))
            continue
        return resp
    raise ServiceError(service, "{}: не удалось получить ответ ({})".format(service, last_err))


def get(service, url, **kw):
    return request(service, "GET", url, **kw)


def post(service, url, **kw):
    return request(service, "POST", url, **kw)


def json_or_error(service, resp, what=""):
    """Разобрать JSON или объяснить, что пришло вместо него."""
    try:
        return resp.json()
    except ValueError:
        raise ServiceError(service, "{} вернул не-JSON (HTTP {}){}".format(
            service, resp.status_code, (": " + what) if what else ""),
            status=resp.status_code, body=(resp.text or "")[:300])
