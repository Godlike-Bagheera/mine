"""settings.json, history.json."""
import json
import time
from pathlib import Path
from modules import state, debug
from modules.config import (
    SETTINGS_FILE, HISTORY_FILE, DEFAULT_SETTINGS,
)
from modules.colors import GREEN, RED, DIM, CYAN, BOLD, RESET


def load_settings():
    if not SETTINGS_FILE.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        for k, v in DEFAULT_SETTINGS.items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        debug.dbg_exc(e, "load_settings")
        return dict(DEFAULT_SETTINGS)


def save_settings(data):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # gh_proxies()/log_keep_days из config читают кэш overrides — обновляем
        from modules import config
        config.reload_user_settings()
    except Exception as e:
        debug.dbg_exc(e, "save_settings")


def apply_settings():
    """Загружает settings.json в runtime-флаги state.* (единственный мост)."""
    s = load_settings()
    state.QUIET_MODE = bool(s.get("quiet_mode", False))
    state.USE_DXVK_HUD = bool(s.get("use_dxvk_hud", False))
    state.USE_MANGOHUD = bool(s.get("use_mangohud", False))
    state.USE_GAMESCOPE = bool(s.get("use_gamescope", False))
    state.USE_TTS_NOTIFY = bool(s.get("use_tts_notify", False))
    state.USE_PER_GAME_PREFIX = bool(s.get("use_per_game_prefix", False))
    try:
        days = int(s.get("log_keep_days", 30))
        state.LOG_KEEP_DAYS = days if 1 <= days <= 365 else 30
    except (TypeError, ValueError):
        state.LOG_KEEP_DAYS = 30
    if s.get("debug_mode", False) and not state.DEBUG_MODE:
        state.DEBUG_MODE = True
        debug.dbg("Дебаг включён из настроек")


def load_history():
    if not HISTORY_FILE.exists():
        return {"games": [], "last_game": None}
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        data.setdefault("games", [])
        data.setdefault("last_game", None)
        return data
    except Exception:
        return {"games": [], "last_game": None}


def save_history(data):
    try:
        HISTORY_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        debug.dbg_exc(e, "settings")
def update_history(exe_path, status=None, duration=None):
    data = load_history()
    path_str = str(exe_path)
    found = False
    for g in data["games"]:
        if g.get("path") == path_str:
            g["count"] = g.get("count", 0) + 1
            g["last"] = time.time()
            if status:
                g["status"] = status
            if duration:
                g["total_seconds"] = g.get("total_seconds", 0) + duration
            found = True
            break
    if not found:
        data["games"].append({
            "path": path_str, "count": 1, "last": time.time(),
            "status": status or "unknown",
            "total_seconds": int(duration or 0),
        })
    data["last_game"] = path_str
    data["games"].sort(key=lambda g: (-g.get("count", 0), -g.get("last", 0)))
    data["games"] = data["games"][:15]
    save_history(data)


def show_history_menu():
    data = load_history()
    games = data.get("games", [])
    if not games:
        return
    print(f"{BOLD}История запусков:{RESET}")
    for i, g in enumerate(games, 1):
        p = Path(g["path"])
        last = g.get("last", 0)
        status = g.get("status", "unknown")
        total_sec = g.get("total_seconds", 0)
        status_icon = {"ok": f"{GREEN}✓{RESET}", "crash": f"{RED}✗{RESET}"}.get(status, " ")
        when = ""
        if last:
            delta = time.time() - last
            if delta < 3600:
                when = f"{int(delta/60)} мин назад"
            elif delta < 86400:
                when = f"{int(delta/3600)} ч назад"
            else:
                when = f"{int(delta/86400)} дн назад"
        time_str = f" {DIM}[{int(total_sec//60)}м]{RESET}" if total_sec >= 60 else ""
        print(f"  {CYAN}{i:2d}{RESET}) {status_icon} {p.name} {DIM}({when}){RESET}{time_str}")
    print()
