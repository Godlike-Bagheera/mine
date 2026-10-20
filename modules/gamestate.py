"""Состояние фоновых игр: реестр запущенных процессов, статусы, остановка.

Лаунчер запускает игры через subprocess.Popen и НЕ блокирует терминал —
пользователь может вводить команды (gamestatus / stopgame / waitgame / games).
При выходе из скрипта активные игры корректно останавливаются (exit-guard
вызывается из modules.launcher.stop_all_games в конце main()).
"""
import time

from modules import debug
from modules.colors import CYAN, DIM, RESET

# name -> {"proc", "path", "log", "start", "kind"}
_running = {}


def register(name, proc, path, log=None, kind="wine"):
    """Добавляет запущенную игру в реестр фоновых процессов."""
    _running[name] = {
        "proc": proc,
        "path": str(path),
        "log": str(log) if log else None,
        "start": time.time(),
        "kind": kind,
    }
    debug.dbg(f"game registered: {name} pid={getattr(proc, 'pid', '?')}")


def unregister(name):
    return _running.pop(name, None)


def running():
    """Только живые процессы; мёртвые автоматически убираются из реестра."""
    alive = {}
    for name, g in list(_running.items()):
        try:
            if g["proc"].poll() is None:
                alive[name] = g
            else:
                _running.pop(name, None)
        except Exception as e:
            debug.dbg(f"running({name}): {e}")
            _running.pop(name, None)
    return alive


def fmt_elapsed(start_ts):
    s = int(time.time() - start_ts)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}ч {m:02d}м"
    if m:
        return f"{m}м {sec:02d}с"
    return f"{sec}с"


def status_lines():
    """Строки статуса для приглашения «Игра >» и команды gamestatus."""
    games = running()
    if not games:
        return []
    lines = []
    for name, g in sorted(games.items()):
        lines.append(
            f"  {CYAN}[{name}]{RESET} идёт {fmt_elapsed(g['start'])} "
            f"{DIM}(pid {g['proc'].pid}){RESET}"
        )
    return lines


def stop_one(name, kill_after=5):
    """Аккуратно останавливает одну игру (terminate → kill). True если убивали."""
    g = _running.get(name)
    if not g:
        return False
    proc = g["proc"]
    stopped = False
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=kill_after)
            except Exception:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except Exception as e:
                    debug.dbg_exc(e, "gamestate")
            stopped = True
    except Exception as e:
        debug.dbg(f"stop_one({name}): {e}")
    _running.pop(name, None)
    return stopped


def stop_all(kill_after=5):
    """Останавливает все активные игры. Возвращает число остановленных."""
    count = 0
    for name in list(_running.keys()):
        try:
            if stop_one(name, kill_after=kill_after):
                count += 1
        except Exception as e:
            debug.dbg(f"stop_all({name}): {e}")
            _running.pop(name, None)
    return count
