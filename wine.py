#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Алиас entry point: wine.py -> launcher.py.

Файл переименован в launcher.py (на Windows Wine не используется вовсе).
Старые копии bootstrap/install.cmd, скачанные до переименования, всё ещё
запускают «wine.py» — поэтому этот тонкий прокси оставлен и перенаправляет
вызов в launcher.py без изменений поведения.
"""
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(_SCRIPT_DIR)
sys.path.insert(0, str(_SCRIPT_DIR))

if __name__ == "__main__":
    # Заменяем argv[0] для корректного имени процесса в логах
    sys.argv[0] = str(_SCRIPT_DIR / "launcher.py")
    _code = compile((_SCRIPT_DIR / "launcher.py").read_text(encoding="utf-8"),
                    "launcher.py", "exec")
    _globals = {"__name__": "__main__", "__file__": str(_SCRIPT_DIR / "launcher.py")}
    exec(_code, _globals)
