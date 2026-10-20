#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MC Installer + Game Launcher (entry point)

Запуск:
    python3 launcher.py
    python3 launcher.py --debug
    python3 launcher.py --help
    python3 launcher.py --version

Исторически файл назывался wine.py — это имя осталось в install.cmd как
алиас, чтобы старые копии bootstrap продолжали работать. Wine используется
только на Linux/РЕД ОС; на Windows 10 всё нативное.

Модули в папке modules/.
"""
import os
import sys
import traceback
from pathlib import Path

# Форсируем рабочую директорию = папка скрипта (можно запускать откуда угодно)
SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(SCRIPT_DIR)
sys.path.insert(0, str(SCRIPT_DIR))

# Модули приложения (debug нужен уже здесь: его использует _print_art ниже;
# раньше импорт шёл только в блоке «ЗАПУСК», и --help с нечитаемым
# ascii-art.txt падал NameError: name 'debug' is not defined).
from modules import debug, state  # noqa: E402

# ---------- ЕДИНЫЙ ИСТОЧНИК ВЕРСИИ ----------
from modules.config import CURRENT_VERSION as VERSION  # noqa: E402


def _print_art():
    """Печатает ASCII-арт из ascii-art.txt (в корне)."""
    art_path = SCRIPT_DIR / "ascii-art.txt"
    try:
        art = art_path.read_text(encoding="utf-8").rstrip("\n")
        if art:
            print(f"\033[96m{art}\033[0m")
    except Exception as e:
        debug.dbg_exc(e, "wine")


if "--version" in sys.argv:
    print(f"MC Installer + Game Launcher v{VERSION}")
    sys.exit(0)

if "--help" in sys.argv or "-h" in sys.argv:
    _print_art()
    print(f"MC Installer + Game Launcher v{VERSION}")
    print()
    print("Использование: python3 launcher.py [опции]")
    print()
    print("Опции:")
    print(r"  --debug     подробное логирование в %USERPROFILE%\mc-portable\logs\debug.log (Linux: ~/wine-portable)")
    print("  --version   показать версию и выйти")
    print("  --help, -h  эта справка")
    print()
    print("Примеры:")
    print("  python3 launcher.py")
    print("  python3 launcher.py --debug")
    print("  bash install.sh --debug")
    sys.exit(0)

# ---------- WARNING ПРО ROOT / ADMIN ----------
from modules.platform_utils import IS_WINDOWS, is_admin_windows  # noqa: E402

# На Windows 10 права администратора не нужны (ставим в профиль юзера, %APPDATA%),
# но запуск от Admin может испортить права на папки — предупреждаем, НЕ выходим.
if IS_WINDOWS:
    if is_admin_windows():
        print("\033[93m[!]\033[0m Скрипт запущен от администратора.")
        print("\033[96m[→]\033[0m Это не ошибка, но лучше запускать от обычного пользователя:")
        print("\033[96m[→]\033[0m игры и шейдеры ставятся в твой профиль (%APPDATA%).")
elif hasattr(os, "geteuid") and os.geteuid() == 0:
    # Legacy-проверка для Unix-систем; на Windows geteuid отсутствует.
    print("\033[93m[!]\033[0m Не рекомендуется запускать скрипт от root (sudo).")
    print("\033[96m[→]\033[0m Файлы могут создаться с правами root.")
    print("\033[96m[→]\033[0m Продолжаем запуск...")

# ---------- ЗАПУСК ----------
# (debug/state уже импортированы в начале файла)


def main():
    # ANSI-цвета в cmd.exe Windows 10 без этого не работают (куски \033[...m
    # печатались бы как мусор). На Linux/Windows Terminal — no-op.
    try:
        from modules.platform_utils import enable_vt
        enable_vt()
    except Exception:
        pass
    from modules.ui import main as ui_main
    ui_main()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print()
    except Exception as e:
        debug.dbg_exc(e, "main/top")
        print()
        from modules.colors import err, hint
        from modules.config import DEBUG_LOG
        err(f"Скрипт упал: {e}")
        if state.DEBUG_MODE:
            traceback.print_exc()
            hint(f"Лог: {DEBUG_LOG}")
            hint("Отчёт: перезапусти и введи `debugreport`")
        sys.exit(1)
