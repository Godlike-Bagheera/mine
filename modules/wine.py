#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
modules/wine.py — легаси-Wine путь (Linux/РЕД ОС), сохранён для совместимости.

Модуль подключают wine-ветки launcher.py / commands.py / ui.py и тесты
(tests/test_mock_download_wine_prefix.py). На Windows 10 эти функции не
вызываются из основного потока (см. modules/ui.py::_linux_wine_boot) либо
возвращают успех сразу (download_wine в legacy-меню).

Исторический контракт (его проверяют mock-тесты):
  * download_wine()            -> bool, качает WINE_MIRRORS в WINE_BIN, chmod 755;
  * download_dxvk(silent=...)  -> bool, tar.gz с зеркал make_dxvk_mirrors(),
                                  распаковка net.safe_extract_tar в DXVK_DIR;
  * create_runexe()            -> bash-обёртка RUNEXE (WINEPREFIX + DXVK_OVERRIDES);
  * install_dxvk_to_wine(exe=None) -> bool, dll-копии в system32/syswow64 + маркер;
  * cmd_dxvk() / cmd_update()  -> интерактивные команды меню 'dxvk'/'update'.

Все пути читаются из модуля КАК АТРИБУТЫ (wine.WINE_DIR и т.д.), поэтому
тесты могут подменять их monkeypatch-ом без перезапуска процесса.
"""
import shutil
from pathlib import Path

from modules import debug, net, prefix
from modules.colors import ok, info, err, hint
from modules.config import (
    WINE_DIR, WINE_BIN, RUNEXE, DXVK_DIR, DEFAULT_PREFIX,
    WINE_MIRRORS, DXVK_VERSIONS, DXVK_OVERRIDES, make_dxvk_mirrors,
)
from modules.download import download_file

# Пути-атрибуты модуля (тесты подменяют их monkeypatch-ом).
WINE_PREFIX = DEFAULT_PREFIX

__all__ = [
    "download_wine", "download_dxvk", "create_runexe",
    "install_dxvk_to_wine", "cmd_dxvk", "cmd_update",
]


def _prefix_path(exe_path=None):
    """Текущий WINEPREFIX (атрибут модуля важен для тестов)."""
    if isinstance(WINE_PREFIX, Path):
        return WINE_PREFIX
    try:
        p = prefix.get_prefix_path(exe_path=exe_path)
    except Exception as e:
        debug.dbg_exc(e, "wine._prefix_path")
        p = None
    return p or DEFAULT_PREFIX


# ---------- ЗАКАЧКА КОМПОНЕНТОВ ----------

def download_wine():
    """Скачать Wine AppImage по списку зеркал. True при успехе."""
    dest = WINE_BIN
    if download_file(WINE_MIRRORS, dest, "Wine", min_size_mb=50):
        try:
            dest.chmod(0o755)
        except Exception as e:
            debug.dbg_exc(e, "wine.download_wine/chmod")
        return True
    return False


def download_dxvk(silent=False):
    """Скачать и распаковать DXVK в DXVK_DIR. True при успехе/уже установлен."""
    if (DXVK_DIR / "x64").exists():
        return True
    for ver in DXVK_VERSIONS:
        mirrors = make_dxvk_mirrors(ver)
        archive = WINE_DIR / "dxvk.tar.gz"
        if not download_file(mirrors, archive, f"DXVK {ver}",
                             min_size_mb=5, silent=silent):
            continue
        tmp = WINE_DIR / "dxvk-extract"
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True, exist_ok=True)
            net.safe_extract_tar(archive, tmp)
            extracted = sorted(p for p in tmp.iterdir() if p.is_dir())
            if not extracted:
                raise RuntimeError("пустой dxvk-архив")
            shutil.rmtree(DXVK_DIR, ignore_errors=True)
            DXVK_DIR.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(extracted[0]), str(DXVK_DIR))
            archive.unlink(missing_ok=True)
            return True
        except Exception as e:
            debug.dbg_exc(e, f"wine.download_dxvk/{ver}")
            archive.unlink(missing_ok=True)
            shutil.rmtree(tmp, ignore_errors=True)
    return False


def create_runexe():
    """bash-обёртка runexe: экспортирует WINEPREFIX/DXVK_HUD и exec'ит Wine."""
    try:
        WINE_DIR.mkdir(parents=True, exist_ok=True)
        pf = _prefix_path()
        script = (
            "#!/bin/bash\n"
            f'export WINEPREFIX="{pf}"\n'
            'export DXVK_HUD="${DXVK_HUD:-0}"\n'
            f'export WINEDLLOVERRIDES="${{WINEDLLOVERRIDES:-{DXVK_OVERRIDES}}}"\n'
            f'exec "{WINE_BIN}" "$@"\n'
        )
        RUNEXE.write_text(script, encoding="utf-8")
        RUNEXE.chmod(0o755)
    except Exception as e:
        debug.dbg_exc(e, "wine.create_runexe")


# ---------- УСТАНОВКА DXVK В ПРЕФИКС ----------

def install_dxvk_to_wine(exe_path=None):
    """Копировать dll из DXVK_DIR в префикс + маркер .dxvk_installed."""
    pf = _prefix_path(exe_path)
    win = Path(pf) / "drive_c" / "windows"
    s32 = win / "system32"
    wow64 = win / "syswow64"
    marker = Path(pf) / ".dxvk_installed"
    if marker.exists():
        try:
            if marker.read_text(encoding="utf-8").strip() == str(DXVK_DIR):
                return True
        except Exception as e:
            debug.dbg_exc(e, "wine.install_dxvk/marker")
    if not s32.is_dir():
        return False
    copied = 0
    for sub in ("x64", "x32"):
        src_dir = DXVK_DIR / sub
        if not src_dir.is_dir():
            continue
        dst = s32 if sub == "x64" else (wow64 if wow64.is_dir() else s32)
        for dll in sorted(src_dir.glob("*.dll")):
            try:
                shutil.copy2(dll, dst / dll.name)
                copied += 1
            except Exception as e:
                debug.dbg_exc(e, f"wine.install_dxvk/{dll.name}")
    if copied == 0:
        return False
    try:
        marker.write_text(str(DXVK_DIR), encoding="utf-8")
    except Exception as e:
        debug.dbg_exc(e, "wine.install_dxvk/mark_write")
    return True


# ---------- ИНТЕРАКТИВНЫЕ КОМАНДЫ ----------

def cmd_dxvk():
    """Команда 'dxvk': переустановка DXVK с подтверждением."""
    if DXVK_DIR.exists():
        try:
            ans = input("DXVK уже установлен. Переустановить? (y/n): ").strip().lower()
        except EOFError:
            return
        if ans not in ("y", "yes", "д", "да"):
            info("Пропущено.")
            return
        shutil.rmtree(DXVK_DIR, ignore_errors=True)
        pf = _prefix_path()
        try:
            (Path(pf) / ".dxvk_installed").unlink(missing_ok=True)
        except Exception as e:
            debug.dbg_exc(e, "wine.cmd_dxvk/marker_unlink")
    if not download_dxvk():
        err("DXVK не скачан")
        return
    prefix.ensure_prefix()
    if install_dxvk_to_wine():
        ok("DXVK установлен")
    else:
        err("Не удалось установить DXVK в префикс (нет Wine-префикса?)")


def cmd_update():
    """Команда 'update': полная перепроверка Wine + DXVK с подтверждением."""
    from modules.platform_utils import IS_WINDOWS
    if IS_WINDOWS:
        # Wine/DXVK на Windows 10 не используются (всё нативное) —
        # бессмысленное скачивание Wine-AppImage убрано. Обновляем только
        # сам скрипт из репозитория.
        try:
            ans = input("Обновить скрипт из GitHub? (да/нет): ").strip().lower()
        except EOFError:
            return
        if ans not in ("y", "yes", "д", "да"):
            info("Пропущено.")
            return
        ok("Wine/DXVK на Windows не нужны — обновление их пропущено.")
        hint("Для обновления кода: git pull в папке установки, либо скачай "
             "свежий install.cmd")
        return
    try:
        ans = input("Обновить Wine и DXVK? (да/нет): ").strip().lower()
    except EOFError:
        return
    if ans not in ("y", "yes", "д", "да"):
        info("Пропущено.")
        return
    if not download_wine():
        err("Wine не обновился")
        return
    if download_dxvk():
        pf = _prefix_path()
        try:
            (Path(pf) / ".dxvk_installed").unlink(missing_ok=True)
        except Exception as e:
            debug.dbg_exc(e, "wine.cmd_update/marker_unlink")
        install_dxvk_to_wine()
    create_runexe()
    ok("Обновление завершено")
