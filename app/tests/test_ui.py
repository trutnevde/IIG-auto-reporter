# -*- coding: utf-8 -*-
"""Целостность интерфейса: кнопка не должна звать несуществующую функцию.

Интерфейс — один файл на девять тысяч строк, собранный из шаблонных строк. Опечатка
в имени функции внутри onclick не видна ни редактору, ни Python: она проявляется
только когда человек нажмёт кнопку и ничего не произойдёт. Такое дважды доезжало
до боевой, поэтому проверка жила в скрипте выката. Теперь она тест и работает на
каждый push, а не только когда кто-то решил выкатывать.
"""
import io
import os
import re

import pytest

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "iigbot", "ui.html")


@pytest.fixture(scope="module")
def страница():
    return io.open(UI, encoding="utf-8").read()


@pytest.fixture(scope="module")
def скрипты(страница):
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", страница, re.S))


def объявленные(скрипты):
    d = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", скрипты))
    d |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
                        r"(?:function|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)", скрипты))
    return d


def test_все_обработчики_объявлены(страница, скрипты):
    """onclick="чтоТо()" на функцию, которой нет, — мёртвая кнопка."""
    d = объявленные(скрипты)
    вызовы = set(re.findall(
        r'on(?:click|change|input|submit|keydown|keyup)\s*=\s*["\']?\s*([A-Za-z_$][\w$]*)\s*\(',
        страница))
    нет = sorted(n for n in вызовы if n not in d and n != "if")
    assert not нет, "кнопки зовут несуществующие функции: " + ", ".join(нет)


def test_ключевые_функции_на_месте(скрипты):
    """Их пропажа означала неработающий кабинет — проверено дважды."""
    d = объявленные(скрипты)
    for имя in ("bindSmart", "doPreview", "doSendTest", "runWeekly", "api", "renderLab",
                "renderBoard", "labRun", "renderDocs", "mdToHtml"):
        assert имя in d, "пропала функция " + имя


def test_ссылки_на_значки_не_битые(страница):
    """<use href="#i-чего-нет"> рисует пустоту — молча."""
    есть = set(re.findall(r'id="(i-[a-z0-9-]+)"', страница))
    нужны = set(re.findall(r'href="#(i-[a-z0-9-]+)"', страница))
    битые = sorted(нужны - есть)
    assert not битые, "ссылки на несуществующие значки: " + ", ".join(битые)


def test_нет_повторов_в_меню(страница):
    """Два раздела с одним значком путают: так было у «Гайда» с «Документацией»."""
    пункты = re.findall(r'data-go="([a-z]+)"[^>]*>\s*<svg[^>]*>\s*<use href="#(i-[a-z0-9-]+)"',
                        страница)
    if len(пункты) < 5:
        pytest.skip("разметка меню изменилась — проверку надо переписать")
    по_значку = {}
    for ключ, значок in пункты:
        по_значку.setdefault(значок, []).append(ключ)
    повторы = {з: k for з, k in по_значку.items() if len(k) > 1}
    assert not повторы, "один значок у разных разделов: {}".format(повторы)


def test_страница_не_обрезана(страница):
    assert страница.rstrip().endswith("</html>"), "ui.html обрывается — файл залит не целиком"
    assert страница.count("<script") == страница.count("</script>")
    assert страница.count("<style") == страница.count("</style>")


def test_патчноут_разбирается(скрипты):
    """Одна лишняя кавычка в записи ломает весь массив, а с ним и весь скрипт."""
    m = re.search(r"const CHANGELOG=\[", скрипты)
    assert m, "не нашёл CHANGELOG"
    хвост = скрипты[m.end() - 1:]
    глубина, конец = 0, None
    for i, ch in enumerate(хвост):
        if ch == "[":
            глубина += 1
        elif ch == "]":
            глубина -= 1
            if глубина == 0:
                конец = i
                break
    assert конец, "массив патчноутов не закрыт"
    кусок = хвост[:конец + 1]
    assert кусок.count("`") % 2 == 0, "непарная обратная кавычка в патчноуте"
    assert re.search(r"\{date:'20\d\d-\d\d-\d\d'", кусок), "в патчноуте нет ни одной записи с датой"
