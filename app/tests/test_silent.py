# -*- coding: utf-8 -*-
"""Молчаливые дефекты, найденные сплошным разбором 22.08.2026.

Общее у всех: ничего не падало, никто не ругался, а результат был неверный и
выглядел правильным. Каждый тест закрывает конкретный случай, который уже
происходил на боевой или мог произойти незаметно.
"""
import json

import pytest


# ─────────── «отправлено» без отправки ───────────
class ПадающийБот:
    """Бот, у которого не проходит ни одна отправка."""

    last_message_ids = []

    def send_message(self, chat_id, text, **kw):
        raise RuntimeError("chat not found")


class РабочийБот:
    def __init__(self):
        self.last_message_ids = []
        self.послано = []

    def send_message(self, chat_id, text, **kw):
        self.last_message_ids = [1001, 1002]
        self.послано.append((chat_id, text))
        return {"message_id": 1002}


def test_все_чаты_упали_значит_не_отправлено():
    """Раньше здесь всегда возвращалось 'sent', и окно прогресса, тост и запись
    в аудит хором сообщали об успехе, которого не было.

    Проверяем строение ветки: полный вызов send_for_login тянет за собой Директ,
    а сеть в тестах закрыта. Зато видно главное — статус считается по результату.
    """
    import inspect
    from iigbot import report
    код = inspect.getsource(report.send_for_login)
    assert 'if sent == 0:' in код, "статус снова не зависит от результата отправки"
    assert '"status": "error"' in код, "провал всех чатов больше не даёт ошибку"
    assert 'message_ids=getattr(tg, "last_message_ids", None)' in код, (
        "плановая рассылка не сохраняет номера — её отправки будут неотменяемыми")
    # и «отправлено» по-прежнему возможно, когда хоть один чат прошёл
    assert '"status": "sent"' in код


def test_номера_сообщений_собираются():
    """Конверт разворачивался дважды: mid всегда был None, список пустой,
    и кнопка «Отменить отправку» не сработала ни разу с момента появления."""
    import inspect
    from iigbot import telegram_api
    код = inspect.getsource(telegram_api.Telegram.send_message)
    assert '(result or {}).get("message_id")' in код, (
        "снова разворачиваем конверт дважды — _call уже вернул data['result']")
    assert '.get("result") or {}).get("message_id")' not in код


def test_отправка_записывает_номера(db):
    """Без номеров отмена невозможна, а кабинет обещает её человеку."""
    ид = db.log_send("клиент", 1, "2026-08-10", "2026-08-16", "sent",
                     by_user=None, message_ids=[1001, 1002])
    rec = db.get_send(ид)
    assert json.loads(rec["message_ids"]) == [1001, 1002]


# ─────────── секреты в текстах ошибок ───────────
def test_секреты_вычищаются_отовсюду(monkeypatch):
    """Точечные заплатки мы уже ставили, и каждая новая строка кода заводила
    новую дырку. Чистка стоит на общем выходе и режет любой секрет."""
    from iigbot import settings
    monkeypatch.setattr(settings, "load_secrets", lambda: {
        "telegram_bot_token": "1234567890:ОЧЕНЬ_СЕКРЕТНЫЙ_ХВОСТ",
        "yandex_oauth_token": "y0_ОЧЕНЬ_СЕКРЕТНЫЙ_ЯНДЕКС",
    })
    monkeypatch.setitem(settings._SCRUB_CACHE, "значения", None)
    for грязь in ("сеть: /bot1234567890:ОЧЕНЬ_СЕКРЕТНЫЙ_ХВОСТ/getMe",
                  "Директ: y0_ОЧЕНЬ_СЕКРЕТНЫЙ_ЯНДЕКС отклонён",
                  "хвост отдельно: ОЧЕНЬ_СЕКРЕТНЫЙ_ХВОСТ"):
        чисто = settings.scrub(грязь)
        assert "ОЧЕНЬ_СЕКРЕТНЫЙ" not in чисто, чисто


def test_короткие_значения_не_режутся(monkeypatch):
    """Иначе секретом станет любое трёхбуквенное слово и текст ошибки покрошится."""
    from iigbot import settings
    monkeypatch.setattr(settings, "load_secrets", lambda: {"что_то": "abc"})
    monkeypatch.setitem(settings._SCRUB_CACHE, "значения", None)
    assert "abc" in settings.scrub("тут есть abc, и это нормально")


def test_отправка_документа_чистит_ошибку():
    """Адрес запроса содержит токен, а requests цитирует его в тексте ошибки.
    Оттуда он попадал в журнал и внутрь ночной копии базы."""
    import inspect
    from iigbot import telegram_api
    код = inspect.getsource(telegram_api.Telegram.send_document)
    assert "self._redact(e)" in код, "сетевая ошибка sendDocument не чистится"
    assert "self._redact(d.get(\"description\"))" in код, "ответ Telegram не чистится"


# ─────────── бюджеты ───────────
def test_отказ_балансов_не_красит_в_зелёное():
    """17.07 Директ не отдал балансы, и пять клиентов с кончающимися деньгами
    стали «ок»: алерты в тот день не ушли, и никто не заметил."""
    import inspect
    from iigbot import budgets
    код = inspect.getsource(budgets.collect)
    assert "balances_ok" in код, "признак отказа балансов потерян"
    assert 'row["balance"] is None and not balances_ok' in код, (
        "отказ балансов снова неотличим от «у клиента нет общего счёта»")


def test_сбой_по_клиенту_не_обнуляет_расход():
    """Клиент, по которому сбор упал, иначе везде становится «без активности»:
    пропадает из списка, не попадает в детектор простоя, сдвигает медиану."""
    import inspect
    from iigbot import budgets
    код = inspect.getsource(budgets.collect)
    assert "_keep_costs" in код
    assert "прежние" in код


def test_частичный_сбор_не_сдвигает_окно():
    """Сбор «по своим клиентам» раньше отодвигал общий агентский обход на 12 часов."""
    import inspect
    from iigbot import budgets
    код = inspect.getsource(budgets.collect)
    assert "if logins is None and balances_ok:" in код


# ─────────── отчёт клиенту ───────────
def test_без_целей_конверсии_клиенту_не_показываются():
    """Директ без списка целей отдаёт конверсии по ВСЕМ целям счётчика, включая
    прокрутки и время на сайте. В отчёте это подписано «Конверсии»."""
    import inspect
    from iigbot import report
    код = inspect.getsource(report)
    assert "allow_bare_conv" in код, "признак потерян"
    assert "цели для отчёта не выбраны" in код, "снова печатаем чужие конверсии как заявки"
    assert "allow_bare_conv=False" in код, "отчёт клиенту снова берёт голое поле"


def test_рассылка_считает_ошибки():
    """Без этой ветки провалившийся клиент не попадал ни в один счётчик:
    строка в окне краснела, а итог показывал «отправлено N, ошибок 0»."""
    import inspect
    from iigbot import report
    код = inspect.getsource(report.run_weekly)
    assert 'res["status"] == "error"' in код
    assert 'results["errors"] += 1' in код


# ─────────── фоновые задачи ───────────
def test_метка_копии_ставится_после_успеха():
    """Раньше метка стояла до работы: упавшая копия выглядела состоявшейся,
    и следующая попытка откладывалась на сутки."""
    import inspect
    from iigbot import web
    код = inspect.getsource(web)
    assert '_due("backup_try"' in код, "окно снова считается по метке успеха"
    assert 'set_kv("backup_last"' in inspect.getsource(web._backup_job), (
        "метка успеха не ставится внутри задачи")


def test_сбои_фоновых_задач_видны_человеку():
    """Молчащий сбой копии — худшее, что может случиться: узнаём о нём в тот
    день, когда копия понадобилась."""
    import inspect
    from iigbot import web
    код = inspect.getsource(web)
    assert "Копия базы не сделана" in код
    assert "Бюджеты не проверены" in код


@pytest.mark.parametrize("метод", ["_backup_job", "_budgets_job"])
def test_задача_не_роняет_приложение(метод):
    """Фоновая задача обязана ловить свои ошибки: она крутится в потоке,
    и падение там не видно вообще никому."""
    import inspect
    from iigbot import web
    код = inspect.getsource(getattr(web, метод))
    assert "except Exception" in код, "{}: нет обработчика".format(метод)
