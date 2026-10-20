"""Логирование в debug.log + ротация старых логов."""
import time
import traceback
from modules import state
from modules.config import DEBUG_LOG, LOG_DIR


def dbg(msg, level="INFO"):
    if not state.DEBUG_MODE:
        return
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] [{level}] {msg}\n")
    except Exception:
        pass


def dbg_exc(e, context=""):
    if not state.DEBUG_MODE:
        return
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{stamp}] [ERROR] {context}: {e}\n")
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        pass


def rotate_old_logs():
    """Удаляет ИГРОВЫЕ логи старше state.LOG_KEEP_DAYS дней.

    Живой debug.log под защитой — он лежит в той же папке и его удаление
    потеряло бы диагностику. Количество дней берётся из настроек (state),
    а не из константы config, поэтому `settings logdays N` работает сразу.
    """
    if not LOG_DIR.exists():
        return
    keep_days = getattr(state, "LOG_KEEP_DAYS", 30)
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for f in LOG_DIR.glob("*.log"):
        if f.name == DEBUG_LOG.name:
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except Exception as e:
            dbg_exc(e, f"rotate_old_logs/{f.name}")
    if removed > 0 and not state.QUIET_MODE:
        # Локальный импорт — colors тянет debug, обходим циклический импорт
        from modules.colors import hint
        hint(f"Очищено {removed} старых логов (>{keep_days} дней)")
