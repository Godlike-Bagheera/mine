# -*- coding: utf-8 -*-
"""Кросс-платформенные утилиты: Windows 10 (основная цель) + Linux (редко).

Раньше проект был чисто под РЕД ОС (Wine); теперь целевая платформа —
Windows 10, а Wine/DXVK/префиксы не нужны вовсе. Всё, что зависит от ОС,
собрано здесь, чтобы остальные модули писали `IS_WINDOWS` и не дублировали
проверки.
"""
import os
import sys
import shutil
import subprocess
from pathlib import Path

IS_WINDOWS = (os.name == "nt")
IS_LINUX = (sys.platform.startswith("linux"))

if IS_WINDOWS:
    ARCH = "amd64" if "ARM" in os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() \
        or "AARCH64" in os.environ.get("PROCESSOR_ARCHITECTURE", "").lower() else "x64"
else:
    try:
        ARCH = subprocess.run(["uname", "-m"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or "x86_64"
    except Exception:
        ARCH = "x86_64"


def user_home() -> Path:
    """Домашняя папка пользователя корректной для текущей ОС."""
    if IS_WINDOWS:
        p = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        return Path(p)
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def portable_root_dirname() -> str:
    """Имя корневой папки портативного установщика в HOME."""
    # Исторически на РЕД ОС папка называлась wine-portable; на Windows
    # переименовываем в mc-portable (Wine там нет). Если старая папка
    # уже существует (миграция), используем её, чтобы не потерять кэш.
    if IS_WINDOWS:
        home = user_home()
        old = home / "wine-portable"
        new = home / "mc-portable"
        if old.exists() and not new.exists():
            return "wine-portable"   # миграция: продолжаем в старой папке
        return "mc-portable"
    return "wine-portable"


def which(name: str):
    """Аналог `which`: путь к исполняемому файлу или None (учитывает .exe/.bat)."""
    found = shutil.which(name)
    if found:
        return found
    if IS_WINDOWS:
        for ext in (".exe", ".bat", ".cmd"):
            found = shutil.which(name + ext)
            if found:
                return found
    return None


def run_cmd(args, capture=False, timeout=None):
    """Безопасный запуск внешней команды; никогда не бросает исключение наружу."""
    try:
        kw = dict(capture_output=capture, text=True, timeout=timeout)
        if IS_WINDOWS:
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.run(args, **kw)
    except Exception as e:
        if capture:
            class _R:  # заглушка результата
                returncode = -1
                stdout = ""
                stderr = str(e)
            return _R()
        raise


def open_in_os(path):
    """Открыть папку/файл штатным способом ОС (explorer.exe / xdg-open)."""
    path = str(path)
    try:
        if IS_WINDOWS:
            subprocess.Popen(["explorer", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


def terminal_title(title: str):
    """Заголовок окна терминала (cmd Windows поддерживает ANSI-set-title)."""
    try:
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()
    except Exception:
        pass


def enable_vt():
    """Включить ANSI escape коды в консоли Windows 10 (VT processing).

    cmd.exe по умолчанию игнорирует \033[... последовательности цвета;
    включаем ENABLE_VIRTUAL_TERMINAL_PROCESSING через kernel32.SetConsoleMode.
    На неудачу — молча продолжаем (цвета просто не отрисуются либо
    раскрасит Windows Terminal, где VT включён изначально).
    """
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        ENABLE_PROCESSED_OUTPUT = 0x0001
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ok_any = False
        for std in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            h = kernel32.GetStdHandle(std)
            mode = wintypes.DWORD()
            if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                r = kernel32.SetConsoleMode(
                    h, mode.value | ENABLE_PROCESSED_OUTPUT
                       | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
                ok_any = ok_any or bool(r)
        return ok_any
    except Exception:
        return False


def is_admin_windows() -> bool:
    """True, если процесс elevated (для предупреждений про права)."""
    if not IS_WINDOWS:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False
