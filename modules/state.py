"""Глобальные флаги, изменяемые в рантайме.

Пользовательские опции (dxvk_hud / mangohud / gamescope / per-game prefix /
log_keep_days) живут здесь: их загружает settings.apply_settings() из
settings.json, а потребители (prefix.py, launcher.py, debug.py) читают
state.* — а не константы config.py, которые фиксированы на этапе импорта.
Константы USE_* в config.py остались только как значения по умолчанию.
"""
import os
import sys

DEBUG_MODE = ("--debug" in sys.argv) or (os.environ.get("WI_DEBUG") == "1")
QUIET_MODE = False

# Runtime-носители пользовательских настроек (см. DEFAULT_SETTINGS в config.py)
USE_DXVK_HUD = False
USE_MANGOHUD = False
USE_GAMESCOPE = False
USE_TTS_NOTIFY = False
USE_PER_GAME_PREFIX = False
LOG_KEEP_DAYS = 30

# Счётчики для slow-mode в download.py
slow_mirror_count = 0
slow_mode_active = False
