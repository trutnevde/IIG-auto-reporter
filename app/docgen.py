# -*- coding: utf-8 -*-
"""Сборщик документации: разделы, которые извлекаются из кода и не пишутся руками.

Запуск:  python docgen.py            — собрать всё в app/docs/generated/
         python docgen.py --check    — проверить, что собранное совпадает с лежащим на диске
                                       (для сторожа выката: разошлось — код ушёл вперёд документации)

Смысл: эти файлы никто не редактирует. Если раздел кабинета переименовали, метод переименовали
или таблицу добавили — документация меняется сама на следующем прогоне. Всё остальное
(объяснения, сценарии, «почему так») пишут люди в app/docs/*.md, и это соседние файлы.
"""
import ast
import io
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

APP = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(APP, "iigbot")
OUT = os.path.join(APP, "docs", "generated")
UI = os.path.join(PKG, "ui.html")


def read(p):
    return io.open(p, encoding="utf-8").read()


def ui_scripts():
    return "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", read(UI), re.S))


# ─────────────────────────── 1. разделы кабинета ───────────────────────────
def gen_sections():
    h = read(UI)
    js = ui_scripts()
    # подпись пункта: выкидываем значок и все спаны с классом (бейджи «демо», счётчики),
    # из остатка берём текст. Спаны бывают вложенными, поэтому чистим, а не выбираем.
    nav = []
    for m in re.finditer(r'data-go="([a-z_]+)"[^>]*>(.*?)</a>', h, re.S):
        inner = re.sub(r"<svg.*?</svg>", "", m.group(2), flags=re.S)
        inner = re.sub(r'<span[^>]*class="[^"]*"[^>]*>.*?</span>', "", inner, flags=re.S)
        label = " ".join(re.sub(r"<[^>]+>", "", inner).split())
        nav.append((m.group(1), label or "—"))
    views = re.findall(r'data-view="([a-z_]+)"', h)
    routes = dict(re.findall(r"([a-z_]+)\s*:\s*(render[A-Za-z]+)", js))
    seen, rows = set(), []
    for key, label in nav:
        if key in seen:
            continue
        seen.add(key)
        rows.append((key, " ".join(label.split()), routes.get(key, "—"),
                     "да" if key in views else "нет"))
    for v in views:
        if v not in seen:
            seen.add(v)
            rows.append((v, "(без пункта меню)", routes.get(v, "—"), "да"))
    body = ["# Разделы кабинета", "",
            "Собрано из `iigbot/ui.html`: пункты навигации, секции и таблица маршрутов.", "",
            "| Ключ | Пункт меню | Отрисовка | Есть секция |", "|---|---|---|---|"]
    for r in rows:
        body.append("| `%s` | %s | `%s` | %s |" % r)
    body += ["", "Всего разделов: **%d**." % len(rows)]
    return "\n".join(body)


# ─────────────────────── 2. методы API и права по ролям ───────────────────────
GUARD = {
    "_require_admin": "только админ",
    "_require_supervisor": "админ или наблюдатель",
    "_require_write": "запрещено наблюдателю",
    "_require_owned": "только свой клиент",
    "_owned_set": "ограничено своими клиентами",
    "_exp_scope": "ограничено своими клиентами",
}


def gen_api():
    src = read(os.path.join(PKG, "api.py"))
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    rows = []
    for n in cls.body:
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(getattr(d, "id", getattr(d, "attr", "")) == "safe" for d in n.decorator_list):
            continue
        seg = ast.get_source_segment(src, n) or ""
        guards = sorted({v for k, v in GUARD.items() if k + "(" in seg})
        doc = (ast.get_docstring(n) or "").strip().split("\n")[0]
        args = [a.arg for a in n.args.args if a.arg != "self"]
        rows.append((n.name, ", ".join(args) or "—",
                     "; ".join(guards) or "любой вошедший", doc[:110]))
    rows.sort()
    body = ["# Методы кабинета и права", "",
            "Собрано из `iigbot/api.py`: все методы с декоратором `@safe` доступны с фронта",
            "как `POST /api/<имя>`. Колонка «Кому» выведена из вызовов проверок прав внутри метода.",
            "", "| Метод | Аргументы | Кому | Что делает |", "|---|---|---|---|"]
    for r in rows:
        body.append("| `%s` | `%s` | %s | %s |" % r)
    body += ["", "Всего методов: **%d**." % len(rows), "",
             "## Роли", "",
             "| Роль | Видит клиентов | Может менять |", "|---|---|---|",
             "| `admin` | только своих | да |",
             "| `user` (специалист) | только своих | да |",
             "| `observer` (работодатель) | всех | нет, режим просмотра |",
             "| без пользователя (десктоп) | всех | да |"]
    return "\n".join(body)


# ─────────────────────────── 3. схема базы ───────────────────────────
def gen_schema():
    src = read(os.path.join(PKG, "storage.py"))
    tabs = re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*[\"']", src, re.S)
    if not tabs:
        tabs = re.findall(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\n\s*\)", src, re.S)
    body = ["# Модель данных", "",
            "Собрано из `iigbot/storage.py` (операторы CREATE TABLE).", ""]
    for name, cols in sorted(tabs):
        fields = []
        for line in cols.split("\n"):
            line = line.strip().rstrip(",")
            if not line or line.upper().startswith(("PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "--")):
                continue
            parts = line.split()
            if parts:
                fields.append("`%s` %s" % (parts[0], " ".join(parts[1:])[:40]))
        body += ["## `%s`" % name, "", ", ".join(fields) or "—", ""]
    body += ["Всего таблиц: **%d**." % len(tabs)]
    return "\n".join(body)


# ─────────────────────────── 4. расписания ───────────────────────────
def gen_schedule():
    web = read(os.path.join(PKG, "web.py"))
    body = ["# Расписание фоновых задач", "",
            "**Три независимых контура.** Они не знают друг о друге, и это важно помнить",
            "при разборе: задача может не отработать в одном и отработать в другом.", "",
            "## Контур 1: ленивый планировщик внутри приложения", "",
            "Тикает из `before_request`, то есть **на любом запросе к кабинету**, но не чаще",
            "раза в 10 минут. Если в кабинет никто не заходит, задачи не идут.", "",
            "| Задача | Периодичность | Метка в таблице `kv` |", "|---|---|---|"]
    for key, hours in re.findall(r'_due\("(\w+)",\s*([\d\s*]+)\)', web):
        h = eval(hours.strip()) if re.fullmatch(r"[\d\s*]+", hours.strip()) else hours
        body.append("| %s | раз в %s ч | `%s` |" % (key.replace("_last", ""), h, key))
    body += ["", "## Контур 2: cron хостинга Beget", "",
             "Настраивается в панели, в репозитории не хранится. Полные строки задач —",
             "в эксплуатационном разделе технического описания.", "",
             "## Контур 3: GitHub Actions", ""]
    wf = os.path.join(os.path.dirname(APP), ".github", "workflows")
    if os.path.isdir(wf):
        body += ["| Файл | Расписание | Что запускает |", "|---|---|---|"]
        for f in sorted(os.listdir(wf)):
            t = read(os.path.join(wf, f))
            cron = ", ".join(re.findall(r"cron:\s*[\"']([^\"']+)", t)) or "по событию"
            runs = ", ".join(re.findall(r"python -m (\S+[^\n]*)", t))[:60] or "—"
            body.append("| `%s` | `%s` | %s |" % (f, cron, runs))
        body += ["", "**Важно:** Actions делает `checkout` и запускает код **из репозитория**,",
                 "а не тот, что залит на Beget. Правка без коммита сюда не доедет."]
    return "\n".join(body)


# ─────────────────────────── 5. команды консоли ───────────────────────────
def gen_cli():
    src = read(os.path.join(PKG, "cli.py"))
    lines = src.split("\n")
    rows = []
    for i, line in enumerate(lines):
        m = (re.search(r'cmd\s+in\s+\(([^)]+)\)', line) or
             re.search(r'cmd\s*==\s*("[a-z0-9\-_]+")', line))
        if not m:
            continue
        names = re.findall(r'"([a-z0-9\-_]+)"', m.group(1))
        if not names:
            continue
        # что вызывается: первый вызов функции в теле ветки
        call = "—"
        SKIP = ("print", "sys.exit", "SystemExit", "len", "int", "str", "raise", "open")
        for nxt in lines[i + 1:i + 12]:
            s = nxt.strip()
            if not s or s.startswith(("#", "elif", "else")):
                continue
            for c in re.findall(r"([A-Za-z_][\w.]*)\s*\(", nxt):
                if c not in SKIP:
                    call = c
                    break
            if call != "—":
                break
        rows.append((names[0], ", ".join("`%s`" % a for a in names[1:]) or "—", call))
    body = ["# Команды консоли", "",
            "Собрано из `iigbot/cli.py`.", "",
            "> **Как выполнять на боевом сервере.** Не `python -m iigbot`, а с явным путём:",
            "> `cd ~/reports.iig.ru/public_html/_app/app && /usr/bin/python3 -m iigbot <команда>`",
            "", "| Команда | Синонимы | Обработчик |", "|---|---|---|"]
    for name, alias, call in rows:
        body.append("| `%s` | %s | `%s` |" % (name, alias, call))
    body += ["", "Всего команд: **%d**." % len(rows)]
    return "\n".join(body)


# ─────────────────────────── 6. карта репозитория ───────────────────────────
def gen_modules():
    rows = []
    for f in sorted(os.listdir(PKG)):
        if not f.endswith(".py"):
            continue
        p = os.path.join(PKG, f)
        src = read(p)
        doc = ""
        try:
            doc = (ast.get_docstring(ast.parse(src)) or "").strip().split("\n")[0]
        except SyntaxError:
            pass
        rows.append((f, len(src) // 1024, src.count("\ndef ") + src.count("\n    def "), doc[:110]))
    body = ["# Карта репозитория", "",
            "Собрано из `iigbot/`: размер, число функций и первая строка описания модуля.", "",
            "| Модуль | КБ | Функций | Назначение |", "|---|---|---|---|"]
    for r in rows:
        body.append("| `%s` | %d | %d | %s |" % r)
    body += ["", "Всего модулей: **%d**." % len(rows)]
    return "\n".join(body)


# ─────────────────────────── 7. цифры для паспорта ───────────────────────────
def gen_numbers():
    import sqlite3
    sys.path.insert(0, APP)
    from iigbot.settings import load_app_config
    p = load_app_config()["db_path"]
    body = ["# Объём: цифры для паспорта продукта", ""]
    if not os.path.isfile(p):
        return "\n".join(body + ["База не найдена: `%s`" % p])
    c = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
    q = lambda s: c.execute(s).fetchone()[0]
    try:
        rows = [
            ("Клиентских аккаунтов подтягивается из Директа", q("SELECT COUNT(*) FROM clients")),
            ("Проектов на активном ведении", q("SELECT COUNT(*) FROM clients WHERE owner IS NOT NULL")),
            ("Из них с настроенными целями", q(
                "SELECT COUNT(*) FROM clients WHERE goals IS NOT NULL AND TRIM(goals) NOT IN ('','[]')")),
            ("С подключённым чатом клиента", q("SELECT COUNT(DISTINCT login) FROM bindings")),
            ("Бюджетов под ежедневным присмотром", q("SELECT COUNT(*) FROM budgets")),
            ("Отчётов доставлено клиентам", q("SELECT COUNT(*) FROM send_log WHERE status='sent'")),
            ("Отчётных недель обслужено", q(
                "SELECT COUNT(DISTINCT period_from) FROM send_log WHERE status='sent'")),
        ]
        if not rows[0][1]:
            # локальная база пустая: нули в паспорте хуже, чем честная пометка
            return "\n".join(body + [
                "> **Цифры не собраны.** База по пути `%s` пуста — значит генератор" % p,
                "> запущен не на боевом сервере. Соберите там: цифры паспорта берутся",
                "> только с боевой базы, иначе в документ уедут нули."])
        body += ["| Показатель | Значение |", "|---|---|"]
        for k, v in rows:
            body.append("| %s | **%s** |" % (k, v))
        sent = rows[5][1]
        body += ["", "Высвобождено времени: **около %d часов** — %d доставленных отчётов "
                     "по 25 минут ручной сборки. Двадцать пять минут это оценка, а не замер."
                 % (round(sent * 25 / 60), sent), "",
                 "> Считается строго по `status='sent'`. Строки `skipped` (не было открута) и",
                 "> `error` доставками не считаются."]
    finally:
        c.close()
    return "\n".join(body)


BLOCKS = [
    ("01-разделы-кабинета.md", gen_sections),
    ("02-методы-и-права.md", gen_api),
    ("03-модель-данных.md", gen_schema),
    ("04-расписание.md", gen_schedule),
    ("05-команды-консоли.md", gen_cli),
    ("06-карта-репозитория.md", gen_modules),
    ("07-объём.md", gen_numbers),
]

HEAD = ("<!-- СОБРАНО АВТОМАТИЧЕСКИ. Не редактировать руками: правки затрутся.\n"
        "     Пересобрать: python app/docgen.py -->\n\n")


def main(check=False):
    os.makedirs(OUT, exist_ok=True)
    diff = []
    for name, fn in BLOCKS:
        try:
            text = HEAD + fn() + "\n"
        except Exception as e:                                          # noqa: BLE001
            print("  [СБОЙ] %-28s %s" % (name, str(e)[:90]))
            diff.append(name)
            continue
        path = os.path.join(OUT, name)
        old = read(path) if os.path.isfile(path) else None
        if check:
            mark = "ок" if old == text else "РАЗОШЛОСЬ"
            if old != text:
                diff.append(name)
            print("  [%s] %-28s %d строк" % (mark, name, text.count("\n")))
        else:
            io.open(path, "w", encoding="utf-8").write(text)
            print("  [%s] %-28s %d строк" % ("обновлён" if old != text else "без изменений",
                                             name, text.count("\n")))
    if check and diff:
        print("\nДокументация отстала от кода: %s" % ", ".join(diff))
        print("Пересобери: python app/docgen.py")
        return 1
    print("\n%s -> %s" % ("Проверка пройдена" if check else "Собрано", OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main(check="--check" in sys.argv))
