# -*- coding: utf-8 -*-
"""Выкат на reports.iig.ru.

Раньше этот скрипт жил только на одной машине и нигде не был записан: сменился бы
человек — и выкатывать стало бы нечем, а как оно устроено, знал бы только он.
Теперь он в репозитории.

Порядок намеренно такой и менять его нельзя:

  1. Тесты. Единственный источник правды про целостность кода и интерфейса. Раньше
     здесь лежала своя копия сторожа кнопок — она разъезжалась с той, что в CI.
  2. Справочники не отстали от кода.
  3. Копия того, что сейчас на сервере, — до первой записи.
  4. Заливка с поштучной сверкой размера. Не сошёлся — откат.
  5. Разбор и настоящий импорт пакета на сервере. Ловит рассинхрон, который
     локальный синтаксис не видит: например, метод, которого нет в залитом модуле.
  6. Живость и наличие ключевых кусков на боевой странице. Нет — откат.

Пароль берётся только из переменной окружения BEGET_TRY. В репозитории его нет
и быть не должно.

Запуск из корня репозитория:  python app/tools/deploy.py
"""
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APPDIR = os.path.join(REPO, "app")
LOCAL = os.path.join(APPDIR, "iigbot")

UPLOAD = ["report.py", "report_custom.py", "dossier.py", "api.py", "storage.py",
          "gsheets.py", "gsheets_sync.py", "web.py", "import_config.py",
          "backup_cloud.py", "ui.html"]

# Куски, без которых боевая страница считается сломанной. Список пополняется,
# когда выкатывается что-то, чью пропажу нельзя заметить глазами.
LIVE_MARKERS = [
    "function bindSmart", "function doPreview", "function labRun",
    'id="labBench"', "К эталону",
    'data-go="docs"', "function renderDocs", "function mdToHtml", "/download/docs.md",
    "function renderBoard", "function bdCard", "function labTabs", 'id="labBody"',
]

HOST, USER = "adolmax0.beget.tech", "adolmax0"
BASE = "/home/a/adolmax0/reports.iig.ru/public_html"
PKG = BASE + "/_app/app/iigbot"
APPREMOTE = BASE + "/_app/app"
BAK = "/home/a/adolmax0/reports.iig.ru/deploy_backup"
KEEP_BACKUPS = 15


def шаг(n, текст):
    print("\n{}. {}".format(n, текст))


def прогнать(команда, зачем):
    print("   {}…".format(зачем), end=" ", flush=True)
    r = subprocess.run(команда, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=REPO)
    if r.returncode != 0:
        print("НЕ ПРОШЛО")
        print((r.stdout or "") + (r.stderr or ""))
        sys.exit(3)
    хвост = [l for l in (r.stdout or "").strip().split("\n") if l.strip()]
    print(хвост[-1].strip() if хвост else "ок")


def main():
    шаг(1, "Проверки перед выкатом")
    прогнать([sys.executable, "-m", "pytest", "app/tests", "-q"], "тесты")
    прогнать([sys.executable, os.path.join("app", "docgen.py"), "--check"], "справочники")

    pw = os.environ.get("BEGET_TRY", "")
    if not pw:
        print("\nНЕТ ПАРОЛЯ: положи его в переменную окружения BEGET_TRY")
        sys.exit(2)

    import paramiko
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, 22, USER, pw, timeout=30, look_for_keys=False, allow_agent=False)
    sftp = cli.open_sftp()

    def run(c):
        return cli.exec_command(c)[1].read().decode("utf-8", "replace").strip()

    def откат():
        for m in UPLOAD:
            run("cp -p {}/{}/{} {}/{} 2>/dev/null || true".format(BAK, stamp, m, PKG, m))
        run("touch {}/tmp/restart.txt".format(BASE))

    шаг(2, "Копия того, что сейчас на сервере -> {}/{}".format(BAK, stamp))
    run("mkdir -p {}/{}".format(BAK, stamp))
    for n in UPLOAD:
        run("cp -p {}/{} {}/{}/{} 2>/dev/null || true".format(PKG, n, BAK, stamp, n))
    run("ls -1d {}/*/ 2>/dev/null | head -n -{} | xargs -r rm -rf".format(BAK, KEEP_BACKUPS))

    шаг(3, "Заливка кода")
    for n in UPLOAD:
        lp = os.path.join(LOCAL, n)
        sz = os.path.getsize(lp)
        sftp.put(lp, PKG + "/" + n)
        got = sftp.stat(PKG + "/" + n).st_size
        print("   {:<18} {:>7} -> {:>7} {}".format(n, sz, got, "ок" if got == sz else "НЕ СОШЛОСЬ"))
        if got != sz:
            откат()
            print("   ОТКАЧЕНО")
            sys.exit(4)

    шаг(4, "Заливка документации (api.py читает эти файлы с диска)")
    run("mkdir -p {}/docs/generated".format(APPREMOTE))
    sftp.put(os.path.join(APPDIR, "docgen.py"), APPREMOTE + "/docgen.py")
    залито = 0
    for sub in ("", "generated"):
        d = os.path.join(APPDIR, "docs", sub) if sub else os.path.join(APPDIR, "docs")
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".md"):
                continue
            lp = os.path.join(d, fn)
            rp = "{}/docs/{}{}".format(APPREMOTE, (sub + "/") if sub else "", fn)
            sftp.put(lp, rp)
            if sftp.stat(rp).st_size != os.path.getsize(lp):
                откат()
                print("   {} НЕ СОШЁЛСЯ, ОТКАЧЕНО".format(fn))
                sys.exit(4)
            залито += 1
    print("   документов: {}".format(залито))

    шаг(5, "Разбор и импорт на сервере")
    mods = ",".join("'iigbot/{}'".format(n) for n in UPLOAD if n.endswith(".py"))
    out = run("cd {}/.. && /usr/bin/python3 -c \"import ast;"
              " [ast.parse(open(p,encoding='utf-8').read()) for p in [{}]];"
              " import iigbot.gsheets, iigbot.storage, iigbot.report, iigbot.dossier,"
              " iigbot.api, iigbot.backup_cloud; print('импорт ок')\" 2>&1".format(PKG, mods))
    print("   {}".format(out))
    if "импорт ок" not in out:
        откат()
        print("   ОТКАЧЕНО")
        sys.exit(5)

    коммит = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                            text=True, cwd=REPO).stdout.strip() or "?"
    with sftp.open(PKG + "/VERSION", "w") as f:
        f.write(json.dumps({"commit": коммит,
                            "at": datetime.datetime.now().isoformat(timespec="seconds"),
                            "files": UPLOAD}, ensure_ascii=False))
    with sftp.open(BASE + "/tmp/restart.txt", "w") as f:
        f.write("deploy {}\n".format(int(time.time())))
    sftp.close()

    шаг(6, "Живость")
    time.sleep(8)
    живо = False
    for i in range(5):
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                "https://reports.iig.ru/healthz",
                headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"}), timeout=45)
            print("   /healthz: {} {}".format(r.status, r.read().decode("utf-8")))
            живо = True
            break
        except Exception as e:                                          # noqa: BLE001
            print("   попытка {}: {}".format(i + 1, str(e)[:80]))
            time.sleep(8)
    if not живо:
        откат()
        print("   НЕ ОТВЕТИЛ — ОТКАЧЕНО")
        sys.exit(6)

    страница = urllib.request.urlopen(urllib.request.Request(
        "https://reports.iig.ru/",
        headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"}),
        timeout=45).read().decode("utf-8", "replace")
    print("   страница {} байт".format(len(страница)))
    нет = [m for m in LIVE_MARKERS if m not in страница]
    if нет:
        откат()
        print("   НЕТ НА БОЕВОЙ: {} — ОТКАЧЕНО".format(", ".join(нет)))
        sys.exit(7)
    print("   все {} ключевых кусков на месте".format(len(LIVE_MARKERS)))

    шаг(7, "Свежие записи журнала")
    print(run("tail -6 $(find /home/a/adolmax0/reports.iig.ru -name 'iig_errors.log' | head -1)"))
    cli.close()
    print("\nГОТОВО. Откат: {}/{}".format(BAK, stamp))


if __name__ == "__main__":
    main()
