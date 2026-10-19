# -*- coding: utf-8 -*-
"""gamemode (GTK) — заглушка для Windows 10.

На РЕД ОС модуль оборачивал запуск игр в `gamemoderun` (LOTTA/GTK).
В Windows аналога нет; функции оставлены, чтобы старые импорты ui.py не
падали, и честно сообщают о неприменимости.
"""
from modules.colors import warn


def cmd_gamemode(*a, **kw):
    warn("GameMode (gamemoderun) — утилита Linux, на Windows 10 недоступна")
    warn("Аналог в Windows: Игры > Параметры графики > приоритет высокого "
         "производительности для java.exe (делается один раз вручную).")
    return False


def gamemode_available() -> bool:
    """GameMode недоступен на Windows 10 (утилита Linux)."""
    return False
