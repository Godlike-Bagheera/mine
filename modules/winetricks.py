# -*- coding: utf-8 -*-
"""Winetricks/шрифты — НЕ ИСПОЛЬЗУЮТСЯ на Windows 10 (заглушка совместимости).

На РЕД ОС модуль ставил через winetricks шрифты (corefonts) в Wine-префикс.
На нативной Windows шрифты уже есть в системе; функции оставлены только для
того, чтобы старые импорты (ui.py / commands.py) не падали.
"""
from modules.colors import warn, hint


def download_winetricks(*a, **kw):
    warn("winetricks не нужен на Windows 10 (нет Wine-префиксов)")
    return False


def cmd_fonts(*a, **kw):
    warn("Шрифты ставить не нужно: вы на Windows 10, системные шрифты доступны")
    hint("Если в игре квадраты вместо букв — обнови драйвер видеокарты.")
    return True


def detect_missing_libs(line: str):
    """Wine-эвристика «not found → пакет» — на Windows не применяется.

    launcher.py использует её для подсказок при запуске через Wine;
    возвращаем пустой список, чтобы вызывающий код не падал.
    """
    return []


def filter_line(line: str) -> str:
    """Приглушает шум Wine в логе запуска. На Windows пропускаем строку как есть."""
    return line
