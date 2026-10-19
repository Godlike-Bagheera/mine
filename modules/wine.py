# -*- coding: utf-8 -*-
"""Wine/DXVK — НЕ ИСПОЛЬЗУЮТСЯ на Windows 10.

Модуль-заглушка для обратной совместимости импортов (ui.py, commands.py):
историческая кодовая база РЕД ОС скачивала Wine AppImage и DXVK в префикс.
На нативной Windows 10 всё это не нужно — лаунчер и игры запускаются напрямую.
Функции существуют, чтобы `import` не падал, и честно сообщают, что операция
неприменима.
"""
from modules.colors import warn, hint


def _na(what):
    warn(f"{what}: не требуется на Windows 10 (модуль Wine упразднён)")
    hint("Legacy Launcher и Minecraft запускаются нативно через Java.")
    return False


def download_wine():
    # Возвращаем True: на Windows Wine не нужен, и вызывающий код в ui.py
    # (Linux-ветка main()) не должен считатать это фатальной ошибкой.
    _na("Скачивание Wine")
    return True


def download_dxvk():
    return _na("Скачивание DXVK")


def cmd_dxvk(*a, **kw):
    return _na("DXVK")


def create_runexe():
    return _na("runexe (обёртка Wine)")


def cmd_update(*a, **kw):
    """`update` на Windows = пересоздать папки кэша; ничего переустанавливать не надо."""
    from modules.config import WINE_DIR
    WINE_DIR.mkdir(parents=True, exist_ok=True)
    warn("Wine/DXVK переустанавливать не нужно — вы на Windows 10.")
    hint("Для обновления скрипта: git pull (или перекачай install.cmd).")
    return True


def install_dxvk_to_wine(*a, **kw):
    return _na("Установка DXVK в префикс")


def cmd_verify_prefix(*a, **kw):
    return _na("Проверка Wine-префикса")
