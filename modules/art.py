"""Загрузка ASCII-арта из ascii-art.txt (в корне проекта)."""
from pathlib import Path

_ART_PATH = Path(__file__).resolve().parent.parent / "ascii-art.txt"
_cache = None


def get_art() -> str:
    """
    Возвращает ASCII-арт как есть.
    НЕ делает strip() — пробелы в конце строк важны для формы.
    Убирает только завершающие переводы строки.
    """
    global _cache
    if _cache is not None:
        return _cache
    try:
        _cache = _ART_PATH.read_text(encoding="utf-8").rstrip("\n")
    except Exception:
        _cache = ""
    return _cache


def print_art(color: str = ""):
    """Печатает арт. color — ANSI-код (например, colors.CYAN)."""
    art = get_art()
    if not art:
        return
    from modules.colors import RESET
    print(f"{color}{art}{RESET}")
