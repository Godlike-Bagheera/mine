"""Поиск .exe и запуск через Wine (+ gamescope, MangoHud, TTS).

Игры запускаются в ФОНОВОМ режиме: subprocess.Popen + фоновый поток-читатель
лога. Терминал сразу остаётся доступным для команд (gamestatus / stopgame /
waitgame / games). proc.wait() больше не блокирует меню.
"""
import os
import re
import sys
import time
import shutil
import threading
import subprocess
from pathlib import Path
from modules import debug, state
from modules.colors import ok, info, warn, err, hint, CYAN, BOLD, RESET
from modules.config import (
    HOME, WINE_BIN, LOG_DIR, PRIORITY_DIRS, EXCLUDE_DIRS,
    SUPPORTED_EXT, OK_RUN_SECONDS,
)
from modules.prefix import ensure_prefix, get_wine_env
from modules.wine import install_dxvk_to_wine
from modules.winetricks import (
    detect_missing_libs, filter_line,
)
from modules.settings import load_settings, load_history, update_history
from modules.gamemode import gamemode_available
from modules import gamestate


def _unique_name(stem):
    """Уникальное имя процесса в реестре: game, game#2, game#3 ..."""
    if stem not in gamestate.running() and stem not in getattr(_taken_names, "s", set()):
        return stem
    taken = set(gamestate.running()) | getattr(_taken_names, "s", set())
    i = 2
    while f"{stem}#{i}" in taken:
        i += 1
    return f"{stem}#{i}"


_taken_names = set()


def stop_all_games():
    """Exit-guard: корректно останавливает все активные фоновые игры."""
    n = gamestate.stop_all(kill_after=5)
    if n:
        info(f"Остановлено активных игр: {n}")
    return n


def cmd_game_status():
    """Команда gamestatus — что запущено прямо сейчас."""
    games = gamestate.running()
    if not games:
        info("Активных игр нет.")
        hint("Запусти игру (<имя.exe>) — терминал останется доступен.")
        return True
    print(f"\n{BOLD}═══ АКТИВНЫЕ ИГРЫ ═══{RESET}")
    for name, g in sorted(games.items()):
        print(f"  {CYAN}{name}{RESET}: pid {g['proc'].pid}, "
              f"идёт {gamestate.fmt_elapsed(g['start'])}")
        print(f"    {Path(g['path']).name}  {CYAN}{g['path']}{RESET}")
        if g.get("log"):
            print(f"    лог: {g['log']}")
    hint("stopgame <имя|all> — остановить, waitgame <имя> — дождаться выхода.")
    return True


def cmd_stopgame(arg=""):
    """Команда stopgame — остановка фоновых игр (по имени или all)."""
    games = gamestate.running()
    if not games:
        info("Активных игр нет.")
        return True
    arg = (arg or "").strip().lower()
    if arg in ("", "all", "все"):
        stopped_one = False
        for name in list(games):
            if gamestate.stop_one(name):
                ok(f"Остановлено: {name}")
                stopped_one = True
        if not stopped_one:
            info("Все процессы уже завершились сами.")
        return True
    # точное имя или префикс (game -> game#2 тоже подходит)
    matches = [n for n in games if n.lower() == arg] or \
              [n for n in games if n.lower().startswith(arg)]
    if not matches:
        err(f"Игра '{arg}' не найдена среди запущенных.")
        hint(f"Запущено: {', '.join(sorted(games))}")
        return True
    name = matches[0]
    if gamestate.stop_one(name):
        ok(f"Остановлено: {name}")
    else:
        info(f"'{name}' уже завершилась — убрано из списка.")
    return True


def cmd_waitgame(arg=""):
    """Команда waitgame — дождаться завершения фоновой игры."""
    games = gamestate.running()
    if not games:
        info("Нет активных игр — ждать нечего.")
        return True
    arg = (arg or "").strip().lower()
    if arg:
        matches = [n for n in games if n.lower() == arg] or \
                  [n for n in games if n.lower().startswith(arg)]
        if not matches:
            err(f"Игра '{arg}' не запущена.")
            return True
        name = matches[0]
    elif len(games) == 1:
        name = next(iter(games))
    else:
        names = ", ".join(sorted(games))
        try:
            name = input(f"{CYAN}Какую ждать? ({names}): {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            return True
        if name not in games:
            err(f"Нет такой игры: {name}")
            return True
    g = games[name]
    info(f"Жду завершения '{name}' (Ctrl+C — перестать ждать)...")
    try:
        rc = g["proc"].wait()
    except KeyboardInterrupt:
        warn("Перестал ждать. Игра продолжает работать в фоне.")
        return True
    secs = gamestate.fmt_elapsed(g["start"])
    gamestate.unregister(name)
    if rc == 0:
        ok(f"'{name}' завершилась сама (время в игре: {secs}).")
    else:
        warn(f"'{name}' завершилась с кодом {rc} (время: {secs}).")
        if g.get("log"):
            hint(f"Лог: {g['log']}")
    return True


def cmd_games_list():
    """Команда games — история + активные."""
    data = load_history()
    games_hist = data.get("games", [])
    active = gamestate.running()
    print(f"\n{BOLD}═══ ИГРЫ ═══{RESET}")
    if not games_hist:
        info("История пуста.")
    for i, g in enumerate(games_hist, 1):
        p = Path(g["path"])
        mark = ""
        for name in active:
            if name.split("#")[0].lower() == p.stem.lower():
                mark = f"  {CYAN}[активна]{RESET}"
                break
        print(f"  {i}) {p.name}  (запусков: {g.get('count', 1)}){mark}")
    if active:
        print("\n  Активные прямо сейчас:")
        for line in gamestate.status_lines():
            print(line)
    hint("Номер или !! — запустить снова; stopgame — остановить.")
    return True


def get_log_path(exe_path):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    name = re.sub(r"[^\w\-]", "_", exe_path.stem)[:40]
    return LOG_DIR / f"{name}_{stamp}.log"


def show_log(log_path, lines=40):
    from modules.colors import DIM
    if not log_path.exists() or log_path.stat().st_size == 0:
        warn("Лог пустой.")
        return
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        tail = content.splitlines()[-lines:]
        print(f"{DIM}{'─' * 60}{RESET}")
        print(f"{BOLD}Последние {len(tail)} строк лога:{RESET}")
        print(f"{DIM}{'─' * 60}{RESET}")
        for line in tail:
            print(line)
        print(f"{DIM}{'─' * 60}{RESET}")
        hint(f"Полный лог: {log_path}")
    except Exception as e:
        debug.dbg_exc(e, "show_log")
        err(f"Не прочитать лог: {e}")


def scan_dir(base, targets, max_depth):
    found = []
    base_depth = len(base.parts)
    try:
        for root, dirs, files in os.walk(base):
            current_depth = len(Path(root).parts) - base_depth
            if current_depth >= max_depth:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
            for fn in files:
                if fn.lower() in targets:
                    found.append(Path(root) / fn)
    except (PermissionError, OSError):
        pass
    return found


def _find_native_launcher(name):
    """Нативные (Linux, НЕ wine/.exe) лаунчеры: Prism Launcher и т.п.

    Ищем точное имя файла среди исполняемых файлов в PRISM_DIR — только
    файлы, не папки (папка `PrismLauncher/` или `PrismLauncherIcons/`
    «запускается» невозможным образом). Если кандидатов несколько —
    берём самый крупный (настоящий ELF-бинарник больше заглушек).
    Возвращает Path или None.
    """
    from modules.config import PRISM_DIR
    try:
        target = name.lower().strip()
    except Exception:
        return None
    if not target or not PRISM_DIR.exists():
        return None
    candidates = []
    for root, dirs, files in os.walk(PRISM_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for f in files:
            if f.lower() != target:
                continue
            p = Path(root) / f
            try:
                if not p.is_file() or not os.access(p, os.X_OK):
                    continue
                size = p.stat().st_size
            except OSError:
                continue
            candidates.append((size, p))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def launch_native(path):
    """Запуск нативного Linux-приложения (без Wine) через общий механизм."""
    info(f"Движок: {BOLD}нативный Linux{RESET} (без Wine)")
    return launch(path, engine="native")


def find_exe(name):
    import sys
    p = Path(name).expanduser()
    if p.is_absolute():
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT:
            return [p]
        # без расширения: может быть уже скачанный нативный бинарник
        # (например ~/prism/.../PrismLauncher) — проверяем до перебора .exe
        if not p.suffix:
            if p.is_file() and os.access(p, os.X_OK):
                return [p]
            for ext in SUPPORTED_EXT:
                candidate = Path(str(p) + ext)
                if candidate.is_file():
                    return [candidate]
        return []
    target_name = Path(name.lower().strip()).name
    targets = set()
    if target_name.endswith(SUPPORTED_EXT):
        targets.add(target_name)
    else:
        for ext in SUPPORTED_EXT:
            targets.add(target_name + ext)
    for base in PRIORITY_DIRS:
        if base.exists():
            sys.stdout.write(f"\r  ищу в {base}...                    ")
            sys.stdout.flush()
            res = scan_dir(base, targets, max_depth=5)
            if res:
                print()
                return res
    sys.stdout.write(f"\r  ищу в {HOME}...                    ")
    sys.stdout.flush()
    res = scan_dir(HOME, targets, max_depth=3)
    print()
    return res


def _tts_say(text):
    if not state.USE_TTS_NOTIFY:
        return
    spd = shutil.which("spd-say")
    if not spd:
        return
    try:
        subprocess.Popen([spd, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        debug.dbg_exc(e, "launcher")
def _build_cmd(path, use_gm):
    suffix = path.suffix.lower()
    if suffix == ".lnk":
        base_cmd = [str(WINE_BIN), "start", "/unix", str(path)]
    elif suffix == ".msi":
        base_cmd = [str(WINE_BIN), "msiexec", "/i", str(path)]
    else:
        base_cmd = [str(WINE_BIN), str(path)]

    if use_gm:
        base_cmd = ["gamemoderun"] + base_cmd

    if state.USE_GAMESCOPE:
        gs = shutil.which("gamescope")
        if gs:
            base_cmd = [gs, "-W", "1920", "-H", "1080", "-f", "--"] + base_cmd

    return base_cmd


def _finalize_game(path, name, log_path, start_time, rc, missing_pkgs):
    """Вызывается из фонового потока когда игра завершилась."""
    gamestate.unregister(name)
    _taken_names.discard(name)
    duration = time.time() - start_time
    # ok если игра прожила достаточно долго; ранний выход с кодом 0 — тоже падение
    status = "ok" if duration >= OK_RUN_SECONDS else "crash"
    if missing_pkgs:
        warn(f"Во время игры не хватало библиотек: {', '.join(sorted(missing_pkgs))}. "
             f"Введи: fonts / winetricks-фиксы, затем перезапусти.")
    update_history(path, status=status, duration=duration)
    if status == "ok":
        ok(f"Игра '{name}' завершилась ({gamestate.fmt_elapsed(start_time)} в игре).")
        _tts_say(f"{path.stem} закрылась")
    else:
        warn(f"Игра '{name}' завершилась за {duration:.1f} с — похоже, упала "
             f"(код {rc}).")
        hint(f"Лог: {log_path}")
        _tts_say(f"{path.stem} завершилась с ошибкой")


def _watch_game(proc, logf, path, name, log_path, start_time):
    """Фоновый поток: читает вывод игры в лог пока процесс жив."""
    missing_pkgs = set()
    crashed = False   # чтение stdout упало — код возврата получить нельзя
    try:
        for line in proc.stdout:
            try:
                logf.write(line)
                logf.flush()
            except (OSError, ValueError):
                pass
            shown = filter_line(line)
            if shown is not None:
                try:
                    sys.stdout.write(shown)
                    sys.stdout.flush()
                except Exception as e:
                    debug.dbg_exc(e, "launcher")
            try:
                for pkg in detect_missing_libs(line):
                    missing_pkgs.add(pkg)
            except Exception as e:
                debug.dbg(f"detect_missing_libs: {e}")
    except Exception as e:
        debug.dbg_exc(e, f"_watch_game/{name}")
        crashed = True
    try:
        rc = proc.wait(timeout=10)
    except Exception:
        rc = -1
    finally:
        try:
            logf.close()
        except Exception as e:
            debug.dbg_exc(e, "launcher")
    if crashed and rc == 0:
        # Код 0 мог быть недостоверным (процесс ещё жив, вывод потерян) —
        # не засчитываем «успех», чтобы история не врала о статусе игры.
        rc = -1
    try:
        _finalize_game(path, name, log_path, start_time, rc, missing_pkgs)
    except Exception as e:
        debug.dbg_exc(e, "_finalize_game")


def _is_elf_binary(path):
    """Настоящий ли это исполняемый ELF-файл (нативный Linux-бинарник)?"""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def launch(path, engine=None):
    """Запуск игры в фоновом режиме: терминал сразу свободен для команд.

    engine="native" — запускать бинарник напрямую (Prism Launcher и другие
    Linux-приложения). Без явного указания движок определяется сам: файл
    без wine-расширения (.exe/.lnk/.msi), который является ELF или AppRun
    portable-архива, запускается НАПРЯМУЮ; раньше он попадал в `wine <файл>`
    и «открыться» не мог.
    """
    suffix = path.suffix.lower()
    if engine is None and suffix not in SUPPORTED_EXT:
        if _is_elf_binary(path) or path.name == "AppRun":
            engine = "native"
    if engine == "native":
        if not os.access(path, os.X_OK):
            try:
                path.chmod(path.stat().st_mode | 0o111)
                hint("Дописал флаг исполнения (+x) на бинарнике")
            except OSError as e:
                debug.dbg_exc(e, "launch/native-chmod")
        cmd = [str(path)]
        if shutil.which("gamemoderun") and gamemode_available():
            settings = load_settings()
            if settings.get("use_gamemode", False):
                cmd = ["gamemoderun"] + cmd
        env = None
        # Portable-сборки Prism (AppImage-подобные) могут требовать FUSE —
        # предупреждаем заранее, как это делает check_host_deps для Wine.
        if path.name == "AppRun":
            import subprocess as _sp
            has_fuse = (_sp.run(["bash", "-c", "test -e /dev/fuse"]).returncode == 0
                        or shutil.which("squashfuse") is not None)
            if not has_fuse:
                warn("Для portable AppRun нужен FUSE: sudo apt install fuse libfuse2")
        log_path = get_log_path(path)
        name = _unique_name(path.stem)
        _taken_names.add(name)
        info(f"Движок: {BOLD}нативный Linux{RESET} (без Wine)")
        info(f"Запускаю: {path}")
        hint(f"Лог: {log_path}")
        print()
        _tts_say(f"Запускаю {path.stem}")
        start_time = time.time()
        try:
            logf = open(log_path, "w", encoding="utf-8", errors="replace")
        except OSError as e:
            err(f"Не могу создать лог: {e}")
            return
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(path.parent), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
            )
        except Exception as e:
            debug.dbg_exc(e, "launch/native-Popen")
            err(f"Ошибка запуска: {e}")
            try:
                logf.close()
            except OSError:
                pass
            _taken_names.discard(name)
            update_history(path, status="crash", duration=time.time() - start_time)
            return
        gamestate.register(name, proc, path, log=log_path)
        t = threading.Thread(target=_watch_game,
                             args=(proc, logf, path, name, log_path, start_time),
                             daemon=True, name=f"gamewatch-{name}")
        t.start()
        ok(f"Игра '{name}' запущена в фоне — терминал доступен.")
        hint("Команды: gamestatus, stopgame <имя|all>, waitgame <имя>, games")
        return
    active = gamestate.running()
    for name, g in sorted(active.items()):
        print(f"  {CYAN}[{name}]{RESET} идёт {gamestate.fmt_elapsed(g['start'])}")
    log_path = get_log_path(path)
    settings = load_settings()
    use_gm = settings.get("use_gamemode", False) and gamemode_available()
    info(f"Движок: {BOLD}Wine{RESET}")
    if use_gm:
        info(f"GameMode: {BOLD}включён{RESET}")
    info(f"Запускаю: {path}")
    hint(f"Лог: {log_path}")
    if not ensure_prefix(exe_path=path):
        warn("Префикс не готов")
    from modules.config import DXVK_DIR
    prefix = get_wine_env(exe_path=path)["WINEPREFIX"]
    if DXVK_DIR.exists() and (Path(prefix) / "drive_c" / "windows" / "system32").exists():
        install_dxvk_to_wine(exe_path=path)
    print()
    _tts_say(f"Запускаю {path.stem}")

    wine_env = get_wine_env(exe_path=path)
    cmd = _build_cmd(path, use_gm)
    debug.dbg(f"Launch: {cmd}")
    start_time = time.time()
    try:
        logf = open(log_path, "w", encoding="utf-8", errors="replace")
    except OSError as e:
        err(f"Не могу создать лог: {e}")
        return
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(path.parent), env=wine_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
    except Exception as e:
        debug.dbg_exc(e, "launch/Popen")
        err(f"Ошибка запуска: {e}")
        try:
            logf.close()
        except Exception as e:
            debug.dbg_exc(e, "launcher")
        update_history(path, status="crash", duration=time.time() - start_time)
        return
    if proc.stdout is None:
        # крайне маловероятно; фиксируем как обычный фоновый процесс без лога
        name = _unique_name(path.stem)
        gamestate.register(name, proc, path, log=log_path)
        _taken_names.add(name)
        ok(f"Игра '{name}' запущена в фоне. Команды: gamestatus, stopgame, waitgame")
        return
    name = _unique_name(path.stem)
    _taken_names.add(name)
    gamestate.register(name, proc, path, log=log_path)
    t = threading.Thread(target=_watch_game, args=(proc, logf, path, name, log_path, start_time),
                         daemon=True, name=f"gamewatch-{name}")
    t.start()
    ok(f"Игра '{name}' запущена в фоне — терминал доступен.")
    hint("Команды: gamestatus, stopgame <имя|all>, waitgame <имя>, games")
