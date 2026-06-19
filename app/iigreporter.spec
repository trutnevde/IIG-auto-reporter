# -*- mode: python ; coding: utf-8 -*-
"""Сборка одного исполняемого файла IIGReporter.exe (PyInstaller, onefile).

Локально/в CI:  pyinstaller --noconfirm --clean iigreporter.spec
Результат:      dist/IIGReporter.exe
"""
from PyInstaller.utils.hooks import collect_all

datas = [('iigbot/ui.html', 'iigbot')]          # интерфейс кладём внутрь exe
binaries = []
hiddenimports = ['iigbot.' + m for m in (
    'cli', 'desktop', 'web', 'report', 'api', 'bot', 'run_weekly',
    'sync_clients', 'import_config', 'storage', 'telegram_api', 'yandex',
    'settings', 'listener',
)]

# pywebview/flask тянут необязательные подмодули — собираем их целиком, без падения,
# если пакета нет.
for pkg in ('pywebview', 'flask', 'requests', 'bottle', 'proxy_tools'):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='IIGReporter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,          # GUI-приложение, без чёрного окна консоли
    disable_windowed_traceback=False,
)
