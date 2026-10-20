"""Help, process_input, диагностика, main."""
import os
import sys
import subprocess
from pathlib import Path
from modules import state, debug
from modules.colors import (
    ok, info, warn, err, hint, sep,
    CYAN, BOLD, GREEN, YELLOW, BLUE, RESET,
)
from modules.config import (
    HOME, WINE_BIN, WINE_PREFIX, DXVK_DIR, WINETRICKS_BIN, ARIA2C_BIN,
    DEBUG_LOG, EXPECTED_COMMANDS, CURRENT_VERSION,
)
from modules.settings import (
    load_history, show_history_menu,
    load_settings, save_settings, apply_settings,
)
from modules.launcher import (
    launch, find_exe, stop_all_games, _find_native_launcher,
    cmd_game_status, cmd_stopgame, cmd_waitgame, cmd_games_list,
)
from modules.java import find_java, cmd_install_java
from modules.minecraft import (
    cmd_minecraft, setup_fabulously_optimized, setup_optifine,
    setup_prism, setup_legacy, cmd_fo_autofix, setup_optimized_fps,
)
from modules.commands import (
    cmd_debug, cmd_debugreport, cmd_reset, cmd_vulkan, cmd_verify,
    cmd_update, cmd_desktop, cmd_log, cmd_settings, cmd_bin,
    cmd_steamfix, cmd_download,
    cmd_export, cmd_gpu_temp, cmd_free_games,
)
from modules.winetricks import cmd_fonts, download_winetricks
from modules.gamemode import cmd_gamemode
from modules.worlds import cmd_save_worlds, cmd_load_worlds, cmd_worlds_menu
from modules.shaders import cmd_shaders
from modules import gamestate
from modules.prefix import get_wine_env, find_minecraft_dirs
from modules.wine import (
    download_wine, download_dxvk, create_runexe,
)
from modules.debug import rotate_old_logs
from modules.art import print_art
from modules.config import ARIA2C_MIRRORS, MIN_ARIA2C_SIZE, BIN_DIR
from modules.download import try_download_manual, cmd_netcheck


def _setup_readline():
    """Автодополнение путей по Tab в input(). Только Unix (Linux/macOS)."""
    try:
        import readline  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        # На Windows модуля нет — тихо выходим, автодополнение не работает
        return

    from modules.config import PRIORITY_DIRS

    def completer(text, state):
        if "/" in text:
            return None
        candidates = []
        for base in PRIORITY_DIRS + [Path.cwd()]:
            if not base.exists():
                continue
            try:
                for f in base.iterdir():
                    if f.name.lower().startswith(text.lower()):
                        candidates.append(f.name)
            except Exception as e:
                debug.dbg_exc(e, "ui")
        candidates = sorted(set(candidates))
        if state < len(candidates):
            return candidates[state]
        return None

    try:
        readline.set_completer(completer)          # type: ignore[attr-defined]
        readline.parse_and_bind("tab: complete")   # type: ignore[attr-defined]
        readline.set_completer_delims(" \t\n")     # type: ignore[attr-defined]
    except Exception as e:
        debug.dbg_exc(e, "ui")
def print_help():
    from modules.platform_utils import IS_WINDOWS
    # Динамическая рамка: ширина считается от содержимого, не «едет» при v2.10+
    title = f"  Команды v{CURRENT_VERSION}"
    width = max(len(title), len("  MC Installer + Game Launcher")) + 2
    border = "═" * width
    print(f"{BOLD}╔{border}╗")
    print(f"║{title.ljust(width)}║")
    print(f"╚{border}╝{RESET}")
    print(f"{BOLD}── Игры ──────────────────────────────────────────{RESET}")
    print(f"  {CYAN}<имя.exe>{RESET}         — запустить игру")
    print(f"  {CYAN}<номер>{RESET}             — из истории")
    print(f"  {CYAN}!!{RESET}                  — последнюю игру")
    print(f"  {CYAN}desktop <имя.exe>{RESET}  — ярлык на рабочем столе")
    print(f"  {CYAN}download <URL>{RESET}     — скачать/распаковать архив")
    print(f"{BOLD}── Фоновые игры ──────────────────────────────────{RESET}")
    print(f"  {CYAN}gamestatus{RESET}           — активные игры (pid, время)")
    print(f"  {CYAN}games{RESET}                — история + активные")
    print(f"  {CYAN}stopgame <имя|all>{RESET} — остановить игру(ы)")
    print(f"  {CYAN}waitgame <имя>{RESET}      — дождаться выхода из игры")
    print(f"{BOLD}── Minecraft ─────────────────────────────────────{RESET}")
    print(f"  {CYAN}minecraft{RESET}            — меню (все варианты)")
    print(f"  {CYAN}fo{RESET}                   — Fabulously Optimized v5.2.8 (1.20.1) + Luxium")
    print(f"  {CYAN}ofps{RESET}                 — Optimized fps (Forge + Luxium, запасной)")
    print(f"  {CYAN}fo-autofix{RESET}           — диагностика/починка .minecraft")
    print(f"  {CYAN}optifine{RESET}             — OptiFine")
    print(f"  {CYAN}prism{RESET}                — Prism Launcher")
    print(f"  {CYAN}legacy{RESET}               — Legacy Launcher")
    print(f"  {CYAN}shaders{RESET}              — установить шейдер-пак (shaderpacks)")
    print(f"  {CYAN}shaders <URL>{RESET}       — пак по прямой ссылке (.zip)")
    print(f"  {CYAN}install-java{RESET}         — портативная JDK 17")
    if IS_WINDOWS:
        print(f"{BOLD}── Миры ──────────────────────────────────────────{RESET}")
    else:
        print(f"{BOLD}── Миры (Диск D, RED OS) ─────────────────────────{RESET}")
    print(f"  {CYAN}worlds{RESET}              — меню миров")
    print(f"  {CYAN}saveworlds{RESET}          — сохранить миры")
    print(f"  {CYAN}loadworlds{RESET}          — загрузить миры")
    print(f"{BOLD}── Утилиты ───────────────────────────────────────{RESET}")
    print(f"  {CYAN}steamfix <имя.exe>{RESET} — заглушка steam_api.dll")
    print(f"  {CYAN}verify{RESET}              — целостность")
    print(f"  {CYAN}update{RESET}              — обновить скрипт из GitHub")
    if IS_WINDOWS:
        # Wine/DXVK/winetricks/GameMode/vulkaninfo — это Linux-путь (РЕД ОС).
        # На Windows всё нативное: эти команды скрыты из справки, но если
        # ввести их случайно — получат понятное сообщение, а не краш.
        pass
    else:
        print(f"  {CYAN}fonts{RESET}               — corefonts (Wine)")
        print(f"  {CYAN}gamemode{RESET}            — GameMode вкл/выкл")
        print(f"  {CYAN}vulkan{RESET}              — проверка Vulkan")
        print(f"  {CYAN}dxvk{RESET}                — только DXVK")
        print(f"  {CYAN}reset{RESET}               — удалить Wine-префикс")
        print(f"  {CYAN}bin{RESET}                 — содержимое bin/")
    print(f"{BOLD}── Настройки ─────────────────────────────────────{RESET}")
    print(f"  {CYAN}settings{RESET}            — показать")
    print(f"  {CYAN}settings quiet|gamemode|debug on|off{RESET}")
    print(f"  {CYAN}quiet{RESET}               — тихий режим")
    print(f"{BOLD}── Отладка ───────────────────────────────────────{RESET}")
    print(f"  {CYAN}debug{RESET}               — статус")
    print(f"  {CYAN}debug on|off{RESET}       — вкл/выкл")
    print(f"  {CYAN}debugreport{RESET}         — архив логов")
    print(f"  {CYAN}netcheck{RESET}            — диагностика сети/DNS "
          f"(если «недоступные домены» при живом интернете)")
    print(f"{BOLD}── Прочее ────────────────────────────────────────{RESET}")
    print(f"  {CYAN}history{RESET}, {CYAN}log{RESET}         — история / последний лог")
    print(f"  {CYAN}help{RESET}, {CYAN}?{RESET}             — эта справка")
    print(f"  {CYAN}exit{RESET}, {CYAN}q{RESET}             — выход (игры остановятся)")
    print(f"{BOLD}── Дополнительно ────────────────────────────────{RESET}")
    print(f"  {CYAN}export{RESET}              — экспорт истории и настроек")
    print(f"  {CYAN}gpu-temp{RESET}            — температура и загрузка GPU")
    print(f"  {CYAN}freegames{RESET}           — каталог бесплатных игр")
    print()


def check_all_commands():
    """EXPECTED_COMMANDS против реестра COMMAND_REGISTRY/ARGS_COMMANDS.

    Сверка по данным, а не по исходнику process_input (антипаттерн с
    inspect.getsource убран): каждая ожидаемая команда должна быть ключом
    одного из двух реестров. Команды выхода (EXIT_COMMANDS) обрабатываются
    process_input до разбора реестра — в реестре их нет, но они не «потеряны».
    """
    try:
        missing = [c for c in EXPECTED_COMMANDS
                   if c not in COMMAND_REGISTRY and c not in ARGS_COMMANDS
                   and c not in EXIT_COMMANDS]
        if missing:
            warn(f"Потеряны команды: {missing}")
        else:
            debug.dbg(f"Все {len(EXPECTED_COMMANDS)} команд обрабатываются")
    except Exception as e:
        debug.dbg_exc(e, "check_all_commands")


# ─────────────────────────────────────────────────────────────────────
#  РЕЕСТР КОМАНД (dict вместо цепочки if/elif):
#  нормализованное имя/синоним -> handler.
#  Точные команды — в COMMAND_REGISTRY; команды с аргументом — в ARGS_COMMANDS
#  (ключ в начале строки, хвост передаётся аргументом).
#  check_all_commands и тесты сверяют с этим реестром EXPECTED_COMMANDS —
#  без inspect.getsource(process_input).
#  ВАЖНО: handlers берутся по имени модуля (ui.cmd_stopgame(...)), а не по
#  прямой ссылке — тогда monkeypatch в тестах продолжает работать.
# ─────────────────────────────────────────────────────────────────────
COMMAND_REGISTRY = {
    # ── точные команды (без аргумента) ──
    "help": print_help, "?": print_help,
    "помощь": print_help, "справка": print_help,
    "debugreport": cmd_debugreport, "отладка": cmd_debugreport,
    "netcheck": lambda a="": cmd_netcheck(),
    "диагностикасети": lambda a="": cmd_netcheck(),
    "проверкасвязи": lambda a="": cmd_netcheck(),
    "games": lambda a="": cmd_games_list(), "игры": lambda a="": cmd_games_list(),
    "minecraft": lambda a="": cmd_minecraft(), "майнкрафт": lambda a="": cmd_minecraft(),
    "fo": lambda a="": setup_fabulously_optimized(),
    "майнкрафт fo": lambda a="": setup_fabulously_optimized(),
    "minecraft fo": lambda a="": setup_fabulously_optimized(),
    "ofps": lambda a="": setup_optimized_fps(),
    "оптимизированныйфпс": lambda a="": setup_optimized_fps(),
    "майнкрафт ofps": lambda a="": setup_optimized_fps(),
    "minecraft ofps": lambda a="": setup_optimized_fps(),
    "fo-autofix": lambda a="": cmd_fo_autofix(), "foautofix": lambda a="": cmd_fo_autofix(),
    "фо-автофикс": lambda a="": cmd_fo_autofix(), "автофикс": lambda a="": cmd_fo_autofix(),
    "optifine": lambda a="": setup_optifine(), "оф": lambda a="": setup_optifine(),
    "майнкрафт optifine": lambda a="": setup_optifine(),
    "minecraft optifine": lambda a="": setup_optifine(),
    "prism": lambda a="": setup_prism(), "майнкрафт prism": lambda a="": setup_prism(),
    "minecraft prism": lambda a="": setup_prism(),
    "legacy": lambda a="": setup_legacy(), "майнкрафт legacy": lambda a="": setup_legacy(),
    "minecraft legacy": lambda a="": setup_legacy(),
    "saveworlds": lambda a="": cmd_save_worlds(), "сохранитьмиры": lambda a="": cmd_save_worlds(),
    "миры на диск": lambda a="": cmd_save_worlds(), "worlds-save": lambda a="": cmd_save_worlds(),
    "loadworlds": lambda a="": cmd_load_worlds(), "загрузитьмиры": lambda a="": cmd_load_worlds(),
    "миры с диска": lambda a="": cmd_load_worlds(), "worlds-load": lambda a="": cmd_load_worlds(),
    "worlds": lambda a="": cmd_worlds_menu(), "миры": lambda a="": cmd_worlds_menu(),
    "дискd": lambda a="": cmd_worlds_menu(), "диск d": lambda a="": cmd_worlds_menu(),
    "fonts": lambda a="": cmd_fonts(), "шрифты": lambda a="": cmd_fonts(),
    "gamemode": lambda a="": cmd_gamemode(),
    "vulkan": lambda a="": cmd_vulkan(), "вулкан": lambda a="": cmd_vulkan(),
    "verify": lambda a="": cmd_verify(), "проверка": lambda a="": cmd_verify(),
    "update": lambda a="": cmd_update(), "обновить": lambda a="": cmd_update(),
    "history": lambda a="": show_history_menu(), "история": lambda a="": show_history_menu(),
    "reset": lambda a="": cmd_reset(), "сброс": lambda a="": cmd_reset(),
    "log": lambda a="": cmd_log(), "лог": lambda a="": cmd_log(),
    "bin": lambda a="": cmd_bin(), "утилиты": lambda a="": cmd_bin(),
    "install-java": lambda a="": cmd_install_java(), "java": lambda a="": cmd_install_java(),
    "gpu-temp": lambda a="": cmd_gpu_temp(), "gtemp": lambda a="": cmd_gpu_temp(),
    "темп": lambda a="": cmd_gpu_temp(),
    "freegames": lambda a="": cmd_free_games(), "free": lambda a="": cmd_free_games(),
    "free-games": lambda a="": cmd_free_games(), "фри": lambda a="": cmd_free_games(),
    "gamestatus": lambda a="": cmd_game_status(),
    "состояниеигр": lambda a="": cmd_game_status(),
    "статусигр": lambda a="": cmd_game_status(),
    "dxvk": lambda a="": _cmd_dxvk(),
    "quiet": lambda a="": _cmd_quiet(), "тихо": lambda a="": _cmd_quiet(),
}


def _cmd_dxvk():
    from modules.platform_utils import IS_WINDOWS
    if IS_WINDOWS:
        from modules.commands import _win_no_wine
        _win_no_wine("dxvk")
        return
    from modules.wine import cmd_dxvk
    cmd_dxvk()


def _cmd_quiet():
    state.QUIET_MODE = not state.QUIET_MODE
    s = load_settings()
    s["quiet_mode"] = state.QUIET_MODE
    save_settings(s)
    ok(f"Тихий режим {'вкл' if state.QUIET_MODE else 'выкл'}")


def _cmd_stopgame(a=""):
    cmd_stopgame(a)


def _cmd_waitgame(a=""):
    cmd_waitgame(a)


def _cmd_shaders(a=""):
    cmd_shaders(a)


def _cmd_desktop(a=""):
    cmd_desktop(a)


def _cmd_steamfix(a=""):
    cmd_steamfix(a)


def _cmd_download(a=""):
    cmd_download(a)


# Команды с аргументом: ключ в начале строки, хвост — аргумент handler'у.
# Обёртки (_cmd_*) нужны, чтобы monkeypatch в тестах (ui.cmd_stopgame = ...)
# продолжал работать: ссылка берётся в момент вызова, а не в момент импорта.
ARGS_COMMANDS = {
    "settings": cmd_settings, "настройки": cmd_settings,
    "debug": cmd_debug,
    "stopgame": _cmd_stopgame, "остановитьигру": _cmd_stopgame, "стопигра": _cmd_stopgame,
    "waitgame": _cmd_waitgame, "ждатьигру": _cmd_waitgame,
    "shaders": _cmd_shaders, "шейдеры": _cmd_shaders, "шейдер": _cmd_shaders,
    "export": cmd_export, "экспорт": cmd_export,
    "desktop": _cmd_desktop, "ярлык": _cmd_desktop,
    "steamfix": _cmd_steamfix,
    "download": _cmd_download, "скачать": _cmd_download,
}

# Выход из REPL
EXIT_COMMANDS = ("exit", "quit", "q", "выход")


def resolve_command(low):
    """(handler, arg) по нормализованной строке ввода или (None, None)."""
    if low in COMMAND_REGISTRY:
        return COMMAND_REGISTRY[low], ""
    for key in ARGS_COMMANDS:
        if low == key or low.startswith(key + " "):
            return ARGS_COMMANDS[key], low[len(key):].strip()
    # префиксные exact-команды (как в прежнем startswith-разборе)
    for key in ("gamestatus", "состояниеигр", "статусигр"):
        if low.startswith(key):
            return COMMAND_REGISTRY[key], ""
    # русские синонимы могут быть написаны слитно с аргументом: "стопигра mine"
    nospace = low.replace(" ", "")
    for key in ("остановитьигру", "стопигра", "ждатьигру"):
        if nospace.startswith(key) and len(nospace) > len(key):
            idx = low.find(key)
            tail = low[idx + len(key):]
            if tail.startswith(" "):
                tail = tail[1:]
            else:
                tail = tail.strip()
            return ARGS_COMMANDS[key], tail
    return None, None


def process_input(name, last_exe):
    low = name.lower().strip()
    if not low:
        return last_exe, True
    if low in EXIT_COMMANDS:
        return last_exe, False
    handler, args = resolve_command(low)
    if handler is not None:
        # desktop/steamfix раньше требовали, что строка НЕ начинается с "/":
        # «/home/user/desktop game.exe» — это путь к игре, а не команда.
        first = low.split(maxsplit=1)[0] if low else ""
        if first in ("desktop", "ярлык", "steamfix") and name.startswith("/"):
            pass  # падаем в поиск игры ниже
        elif low in ("desktop", "ярлык") and not args:
            warn("desktop <имя.exe>")
            return last_exe, True
        else:
            handler(args)
            return last_exe, True
    if low == "!!":
        if not last_exe:
            warn("Нет истории")
            return last_exe, True
        path = Path(last_exe)
        if not path.exists():
            err(f"Файл не существует: {path}")
            return last_exe, True
        launch(path)
        return str(path), True
    if low.isdigit():
        data = load_history()
        games = data.get("games", [])
        num = int(low)
        if 1 <= num <= len(games):
            path = Path(games[num - 1]["path"])
            if not path.exists():
                err(f"Файл не существует: {path}")
                return last_exe, True
            launch(path)
            return str(path), True
        err(f"Нет игры #{num}")
        return last_exe, True
    # Нативные Linux-лаунчеры (Prism и др.): их обычный поиск ниже ищет
    # только *.exe/*.lnk/*.msi, поэтому «PrismLauncher» после успешной
    # загрузки выдавал «не найден». Ищем точное имя среди исполняемых
    # файлов PRISM_DIR и запускаем БЕЗ Wine.
    if not name.startswith("/") and Path(name).suffix.lower() not in \
            (".exe", ".lnk", ".msi"):
        native = _find_native_launcher(low)
        if native is not None:
            launch(native, engine="native")
            return str(native), True
    info(f"Ищу '{name}'...")
    results = find_exe(name)
    if not results:
        err(f"'{name}' не найден.")
        return last_exe, True
    if len(results) == 1:
        chosen = results[0]
    else:
        for i, r in enumerate(results, 1):
            print(f"  {i}) {r}")
        try:
            idx = int(input("Номер: ").strip()) - 1
            if 0 <= idx < len(results):
                chosen = results[idx]
            else:
                err("Неверный номер")
                return last_exe, True
        except (ValueError, KeyboardInterrupt):
            err("Отменено")
            return last_exe, True
    launch(chosen)
    return str(chosen), True


def _netcheck_summary():
    """Быстрая (без HTTPS) DNS-сводка для диагностики при старте.

    Различает «интернета нет» и «DNS провайдера блокирует домены лаунчера»
    — именно этот симптом даёт окно Legacy Launcher «Список недоступных
    доменов» при работающем интернете. Подробная проверка: команда netcheck.
    """
    try:
        from modules.download import diagnose_dns, NETCHECK_DOMAINS
        res = diagnose_dns(NETCHECK_DOMAINS[:5])
        blocked = [d for d, r in res.items() if r["verdict"] == "dns_block"]
        dead = [d for d, r in res.items() if r["verdict"] == "no_internet"]
        if dead and len(dead) == len(res):
            warn("Похоже, интернет отсутствует (домены не резолвятся "
                 "даже через 8.8.8.8).")
        elif blocked:
            warn(f"DNS-блокировка: {', '.join(blocked)} — интернет есть, "
                 f"но провайдер перехватывает эти домены.")
            hint("Поставь DNS 77.88.8.8 / 8.8.8.8; подробности: команда "
                 f"{CYAN}netcheck{RESET}")
        else:
            ok("DNS: домены лаунчера резолвятся корректно")
    except Exception as e:
        debug.dbg_exc(e, "_netcheck_summary")


def print_diagnostics():
    from modules.platform_utils import IS_WINDOWS
    if IS_WINDOWS:
        # Нативная диагностика Windows 10 (без Wine/DXVK/префиксов)
        sep()
        info(f"{BOLD}Диагностика системы (Windows){RESET}")
        print()
        try:
            from modules.sysinfo import get_hardware, format_hw
            hw = get_hardware()
            print(format_hw(hw))
        except Exception as e:
            debug.dbg_exc(e, "print_diagnostics/sysinfo")
            warn("Не удалось собрать инфо о железе")
        mc_dirs = find_minecraft_dirs(limit=8)
        if mc_dirs:
            for d in mc_dirs[:5]:
                ok(f"Игровая папка: {d}")
        else:
            hint("Игровых папок не найдено — введи `legacy` для установки")
        java_bin, java_home, java_status = find_java()
        if java_home or java_bin:
            ok(f"Java: {java_home or java_bin}")
        else:
            warn(f"Java: {java_status}")
        _netcheck_summary()
        if state.DEBUG_MODE:
            ok(f"Дебаг: включён → {DEBUG_LOG}")
        print()
        return
    sep()
    info(f"{BOLD}Диагностика системы{RESET}")
    print()
    items = [
        ("Wine AppImage", WINE_BIN.exists(),
         f"{WINE_BIN.stat().st_size//1024//1024 if WINE_BIN.exists() else 0} МБ"),
        ("DXVK", DXVK_DIR.exists(), ""),
        ("Префикс", (WINE_PREFIX / "drive_c").exists(), ""),
        ("winetricks", WINETRICKS_BIN.exists(),
         f"{WINETRICKS_BIN.stat().st_size//1024 if WINETRICKS_BIN.exists() else 0} КБ"),
        ("aria2c", ARIA2C_BIN.exists(),
         f"{ARIA2C_BIN.stat().st_size//1024//1024 if ARIA2C_BIN.exists() else 0} МБ"),
    ]
    for name, exists, extra in items:
        if exists:
            ok(f"{name}: {extra}" if extra else f"{name}: OK")
        else:
            if name == "Префикс":
                hint(f"{name}: не создан (появится при первом запуске игры)")
            else:
                warn(f"{name}: отсутствует")
    java_bin, java_home, java_status = find_java()
    if java_home:
        ok(f"Java GUI: {java_home}")
    else:
        warn(f"Java: {java_status}")
        hint(f"Хочешь поставить OptiFine? Введи: {CYAN}install-java{RESET}")
    if state.DEBUG_MODE:
        ok(f"Дебаг: включён → {DEBUG_LOG}")
    print()


def main():
    # ASCII-арт из ascii-art.txt
    print_art(CYAN)
    title1 = f"  MC Installer + Game Launcher  v{CURRENT_VERSION}"
    title2 = "  Windows 10: Legacy + Fabulously Optimized + шейдеры"
    width = max(len(title1), len(title2)) + 2
    border = "═" * width
    print(f"{BLUE}╔{border}╗")
    print(f"║{title1.ljust(width)}║")
    print(f"║{title2.ljust(width)}║")
    print(f"╚{border}╝{RESET}\n")

    apply_settings()

    try:
        rotate_old_logs()
    except Exception as e:
        debug.dbg_exc(e, "rotate_old_logs")

    _setup_readline()

    debug.dbg("=" * 60)
    debug.dbg(f"ЗАПУСК v{CURRENT_VERSION}, DEBUG={state.DEBUG_MODE}, argv={sys.argv}")
    check_all_commands()
    if state.DEBUG_MODE:
        info(f"Дебаг включён → {DEBUG_LOG}")
        print()
    if not check_disk_space():
        sys.exit(1)

    from modules.platform_utils import IS_WINDOWS

    def _linux_wine_boot():
        """Linux/РЕД ОС: Wine-путь сохранён для совместимости."""
        try:
            check_host_deps()
        except Exception as e:
            debug.dbg_exc(e, "main/check_host_deps")
        ensure_bin_tools()
        create_runexe()
        if not (WINE_BIN.exists() and os.access(WINE_BIN, os.X_OK)):
            sep()
            info(f"{BOLD}Скачиваю Wine...{RESET}")
            if not download_wine():
                err("Wine не скачался")
                sys.exit(1)
        if not (DXVK_DIR.exists() and (DXVK_DIR / "x64").exists()):
            sep()
            info(f"{BOLD}Скачиваю DXVK...{RESET}")
            download_dxvk()
        sep()
        info("Проверяю Wine...")
        try:
            result = subprocess.run([str(WINE_BIN), "--version"], env=get_wine_env(),
                                    capture_output=True, text=True, timeout=30)
            out = (result.stdout + result.stderr).strip()
            if result.returncode == 0 and "wine" in out.lower():
                ok(f"Wine: {out.splitlines()[0]}")
            else:
                err("Wine не отвечает")
                sys.exit(1)
        except Exception as e:
            debug.dbg_exc(e, "main/wine-version")
            err(f"Wine: {e}")
            sys.exit(1)
        print_diagnostics()
        print_help()
        sep()
        print(f"{GREEN}Готово!{RESET} Пиши игру или команду.")
        if state.DEBUG_MODE:
            hint("Дебаг включён. При проблеме: `debugreport`")
        print()

    if IS_WINDOWS:
        # ── Нативный путь Windows 10: Wine/DXVK/winetricks не нужны вовсе ──
        java_bin, java_home, java_status = find_java()
        if java_home or java_bin:
            ok(f"Java найдена: {java_home or java_bin}")
        else:
            warn(f"Java: {java_status}")
            hint("Legacy/FO установятся автоматически — или введи: install-java")
        mc_dirs = find_minecraft_dirs(limit=5)
        if mc_dirs:
            ok(f"Игровые директории: {len(mc_dirs)} (свежая: {mc_dirs[0]})")
        else:
            hint("Установок Minecraft пока нет. Начни с команды: legacy "
                 "(лаунчер) → скачай версию → fo (модпак) → shaders (шейдеры).")
        print_diagnostics()
        print_help()
        sep()
        print(f"{GREEN}Готово!{RESET} Пиши команду (legacy / fo / shaders / minecraft / help).")
        if state.DEBUG_MODE:
            hint("Дебаг вкл. При проблеме: `debugreport`")
        print()
    else:
        _linux_wine_boot()

    # Общий REPL-цикл для обеих платформ. Перед выходом останавливаем все
    # запущенные игры и показываем их статус (stop_all_games + gamestate).
    if gamestate.running():
        for line in gamestate.status_lines():
            info(line)
    last_exe = None
    try:
        while True:
            try:
                prompt = f"{CYAN}mc>{RESET} "
                try:
                    name = input(prompt).strip()
                except EOFError:
                    break
            except KeyboardInterrupt:
                print()
                continue
            if not name:
                continue
            last_exe, cont = process_input(name, last_exe)
            if not cont:
                break
    finally:
        stop_all_games()



def check_host_deps():
    """Проверка зависимостей хоста перед попыткой скачать/запустить Wine.

    Даёт понятное объяснение, почему Wine может не заработать, ДО того как
    пользователь увидит невнятный «Wine не отвечает». Ничего критичного не
    блокирует (продолжаем запуск), кроме явного отсутствия fuse — без него
    AppImage в принципе не смонтируется.
    """
    import shutil
    problems = []
    # FUSE для монтирования AppImage (Wine/DXVK скачиваются как .AppImage)
    has_fuse = (shutil.which("fuse") is not None
                or Path("/dev/fuse").exists()
                or shutil.which("squashfuse") is not None
                or shutil.which("appimagetool") is not None)
    if not has_fuse:
        problems.append(("FUSE", "нет /dev/fuse и утилит fuse/squashfuse — "
                         "AppImage не смонтируется",
                         "sudo apt install fuse libfuse2"))
    # Дисплейный сервер (Wine GUI требует X11 или Wayland)
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        problems.append(("X11/Wayland", "переменные DISPLAY/WAYLAND_DISPLAY пусты — "
                         "графические игры не откроют окно",
                         "запусти из графической сессии"))
    # Vulkan-драйвер (нужен DXVK)
    try:
        r = subprocess.run(["vulkaninfo", "--summary"], capture_output=True,
                           text=True, timeout=8)
        vk_ok = r.returncode == 0 and "deviceName" in (r.stdout + r.stderr)
    except Exception as e:
        debug.dbg_exc(e, "check_host_deps/vulkaninfo")
        vk_ok = False
    if not vk_ok:
        problems.append(("Vulkan", "vulkaninfo не найден или нет устройств — "
                         "DXVK не заработает",
                         "пакеты mesa-vulkan-drivers / nvidia-vulkan / vulkan-tools"))
    if problems:
        sep()
        warn("Зависимости хоста:")
        for name, why, fix in problems:
            print(f"  {YELLOW}{name:12}{RESET} {why}")
            hint(f"Решение: {fix}")
        print()
    else:
        debug.dbg("check_host_deps: все зависимости на месте")
    return has_fuse


def check_disk_space():
    import shutil
    try:
        stat = shutil.disk_usage(HOME)
        free_gb = stat.free / 1024 / 1024 / 1024
        debug.dbg(f"Свободно {free_gb:.2f} ГБ")
        if free_gb < 1:
            err(f"Мало места: {free_gb:.2f} ГБ")
            return False
        return True
    except Exception as e:
        debug.dbg_exc(e, "check_disk_space")
        return True


def ensure_bin_tools():
    from modules.platform_utils import IS_WINDOWS
    if IS_WINDOWS:
        # На Windows 10 winetricks/aria2c не нужны: скачки идут через urllib
        # (modules/download.py), Wine-обёртки упразднены.
        debug.dbg("ensure_bin_tools: Windows — утилиты Wine не требуются")
        return
    try:
        has_wt = WINETRICKS_BIN.exists() and WINETRICKS_BIN.stat().st_size > 100 * 1024
        has_ar = ARIA2C_BIN.exists() and ARIA2C_BIN.stat().st_size > 500 * 1024
    except OSError as e:
        debug.dbg_exc(e, "ensure_bin_tools/stat")
        warn(f"Не могу проверить утилиты ({e}) — пропускаю автозагрузку")
        return
    if not has_wt or not has_ar:
        info(f"{BOLD}Проверка утилит...{RESET}")
        if not has_wt:
            download_winetricks()
        if not has_ar:
            download_aria2c()


def download_aria2c():
    import tarfile
    import shutil as sh
    if ARIA2C_BIN.exists() and ARIA2C_BIN.stat().st_size > MIN_ARIA2C_SIZE:
        return True
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    info("Скачиваю aria2c...")
    archive = None
    success = False
    for name, url in ARIA2C_MIRRORS:
        debug.dbg(f"aria2c: {name}")
        if url.endswith(".tar.gz"):
            archive = BIN_DIR / "aria2.tar.gz"
        elif url.endswith(".tar.bz2"):
            archive = BIN_DIR / "aria2.tar.bz2"
        else:
            continue
        if archive.exists():
            try:
                archive.unlink()
            except Exception as e:
                debug.dbg_exc(e, "ui")
        if try_download_manual(name, url, archive, silent=False):
            if archive.exists() and archive.stat().st_size > MIN_ARIA2C_SIZE:
                success = True
                break
    if not success or archive is None:
        err("aria2c не скачался")
        return False
    try:
        tmp_dir = BIN_DIR / "aria2_extract"
        if tmp_dir.exists():
            sh.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True)
        if archive.name.endswith(".tar.gz"):
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(tmp_dir)
        elif archive.name.endswith(".tar.bz2"):
            with tarfile.open(archive, "r:bz2") as tar:
                tar.extractall(tmp_dir)
        found = False
        for f in tmp_dir.rglob("aria2c"):
            if f.is_file():
                sh.copy2(f, ARIA2C_BIN)
                ARIA2C_BIN.chmod(0o755)
                found = True
                break
        sh.rmtree(tmp_dir, ignore_errors=True)
        try:
            archive.unlink()
        except Exception as e:
            debug.dbg_exc(e, "ui")
        if found:
            ok("aria2c установлен")
            return True
        err("В архиве нет aria2c")
        return False
    except Exception as e:
        debug.dbg_exc(e, "download_aria2c/extract")
        err(f"Распаковка: {e}")
        return False
