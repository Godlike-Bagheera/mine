"""Вспомогательные команды лаунчера (вынесены из commands.py).

Ярлыки на рабочем столе, экспорт конфигурации, температура GPU и каталог
бесплатных игр. commands.py реэкспортирует эти имена — публичный API не менялся.
"""
import os
import re
import shutil
import subprocess
from modules import debug, state
from modules.colors import ok, info, warn, err, hint, CYAN, BOLD, DIM, YELLOW, RESET
from modules.config import (
    WINE_DIR, BIN_DIR, DESKTOP_DIRS, SHORTCUTS_DIR, DXVK_OVERRIDES,
    WINE_PREFIX, WINE_BIN, DEBUG_LOG, HISTORY_FILE, SETTINGS_FILE,
    FREE_GAMES,
)
from modules.gamemode import gamemode_available
from modules.java import find_java
from modules.launcher import find_exe
from modules.settings import load_settings, save_settings, apply_settings


def get_desktop_dir():
    for d in DESKTOP_DIRS:
        if d.exists():
            return d
    return None


def create_desktop_shortcut(exe_path):
    desktop = get_desktop_dir()
    if not desktop:
        err("Рабочий стол не найден")
        return False
    SHORTCUTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\-]", "_", exe_path.stem)
    wrapper = SHORTCUTS_DIR / f"{safe_name}.sh"
    wrapper_content = (
        f'#!/bin/bash\n'
        f'export WINEPREFIX="{WINE_PREFIX}"\n'
        f'export WINEDLLOVERRIDES="{DXVK_OVERRIDES}"\n'
        f'cd "{exe_path.parent}" || exit 1\n'
        f'exec "{WINE_BIN}" "{exe_path.name}"\n'
    )
    wrapper.write_text(wrapper_content, encoding="utf-8")
    wrapper.chmod(0o755)
    desktop_file = desktop / f"{exe_path.stem}.desktop"
    desktop_content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={exe_path.stem}\n"
        f"Exec={wrapper}\n"
        "Icon=applications-games\n"
        "Terminal=false\n"
        "Categories=Game;\n"
    )
    desktop_file.write_text(desktop_content, encoding="utf-8")
    desktop_file.chmod(0o755)
    ok(f"Ярлык: {desktop_file}")
    return True


def cmd_desktop(name):
    info(f"Ищу '{name}'...")
    results = find_exe(name)
    if not results:
        err(f"'{name}' не найден.")
        return
    if len(results) > 1:
        for i, r in enumerate(results, 1):
            print(f"  {i}) {r}")
        try:
            idx = int(input("Номер: ").strip()) - 1
            if 0 <= idx < len(results):
                create_desktop_shortcut(results[idx])
        except (ValueError, KeyboardInterrupt):
            err("Отменено")
        return
    create_desktop_shortcut(results[0])


def cmd_settings(args=""):
    """Показ/изменение пользовательских настроек (settings.json → state.*)."""
    s = load_settings()
    if not args:
        gm_ok = gamemode_available()
        java_bin, java_home, java_status = find_java()

        def flag(key):
            return f"{CYAN}{'вкл' if s.get(key) else 'выкл'}{RESET}"

        print(f"{BOLD}Настройки:{RESET}")
        print(f"  Тихий режим:     {flag('quiet_mode')}")
        print(f"  GameMode:        {flag('use_gamemode')}"
              f"  {DIM}(библиотека {'есть' if gm_ok else 'нет'}){RESET}")
        print(f"  Дебаг:           {CYAN}{'вкл' if state.DEBUG_MODE else 'выкл'}{RESET}")
        print(f"  DXVK HUD:        {flag('use_dxvk_hud')}")
        print(f"  MangoHud:        {flag('use_mangohud')}")
        print(f"  Gamescope:       {flag('use_gamescope')}")
        print(f"  Озвучка (TTS):   {flag('use_tts_notify')}")
        print(f"  Префикс на игру: {flag('use_per_game_prefix')}")
        print(f"  Хранить логи:    {s.get('log_keep_days', 30)} дн.")
        print(f"  Java:            {CYAN}{java_status}{RESET}")
        print(f"  Префикс:         {DIM}{WINE_PREFIX}{RESET}")
        print(f"  Debug лог:       {DIM}{DEBUG_LOG}{RESET}")
        print()
        hint("settings <ключ> on|off; ключи: quiet gamemode debug dxvkhud "
             "mangohud gamescope tts pergameprefix logdays")
        hint("Пример: settings mangohud on   |   settings logdays 14")
        return
    parts = args.split()
    if len(parts) == 2:
        key, value = parts
        low = key.lower()
        # logdays принимает число, а не on/off
        if low in ("logdays", "logs", "дней"):
            try:
                days = int(value)
            except ValueError:
                warn("settings logdays <число от 1 до 365>")
                return
            if not 1 <= days <= 365:
                warn("settings logdays <число от 1 до 365>")
                return
            s["log_keep_days"] = days
            save_settings(s)
            apply_settings()
            ok(f"Логи хранятся {days} дн.")
            return
        value_on = value.lower() in ("on", "true", "1", "yes", "да", "вкл")
        value_off = value.lower() in ("off", "false", "0", "no", "нет", "выкл")
        bool_keys = {
            "quiet": "quiet_mode", "тихо": "quiet_mode",
            "gamemode": "use_gamemode", "гаммод": "use_gamemode",
            "dxvkhud": "use_dxvk_hud", "hud": "use_dxvk_hud",
            "mangohud": "use_mangohud", "мангохад": "use_mangohud",
            "gamescope": "use_gamescope", "геймскоп": "use_gamescope",
            "tts": "use_tts_notify", "озвучка": "use_tts_notify",
            "pergameprefix": "use_per_game_prefix", "pergame": "use_per_game_prefix",
        }
        if low in bool_keys and (value_on or value_off):
            s[bool_keys[low]] = value_on
            save_settings(s)
            apply_settings()
            names = {"quiet_mode": "Тихий режим", "use_gamemode": "GameMode",
                     "use_dxvk_hud": "DXVK HUD", "use_mangohud": "MangoHud",
                     "use_gamescope": "Gamescope", "use_tts_notify": "Озвучка",
                     "use_per_game_prefix": "Префикс на игру"}
            ok(f"{names[bool_keys[low]]} {'вкл' if value_on else 'выкл'}")
            return
        if low in ("debug", "дебаг") and (value_on or value_off):
            from modules.commands import cmd_debug  # лениво: см. реэкспорт в commands.py
            cmd_debug("on" if value_on else "off")
            return
    warn("settings <ключ> on|off — см. `settings` (список ключей)")


def cmd_bin():
    print(f"{BOLD}{BIN_DIR}:{RESET}")
    if not BIN_DIR.exists():
        warn("Папка отсутствует")
        return
    items = list(BIN_DIR.iterdir())
    if not items:
        warn("Пусто")
        return
    for f in sorted(items):
        size = f.stat().st_size
        size_str = f"{size/1024/1024:.1f} МБ" if size > 1024*1024 else f"{size/1024:.0f} КБ"
        mark = "✓" if os.access(f, os.X_OK) else " "
        print(f"  [{mark}] {f.name}  {DIM}({size_str}){RESET}")
    print()


# ═══════════════════════════════════════════════════════════════════
#  ДОПОЛНИТЕЛЬНЫЕ КОМАНДЫ
# ═══════════════════════════════════════════════════════════════════


def cmd_export(args=""):
    """Экспорт истории и настроек в tar.gz."""
    import tarfile
    archive = WINE_DIR / "wine-config-export.tar.gz"
    if archive.exists():
        archive.unlink()
    files_to_pack = []
    if HISTORY_FILE.exists():
        files_to_pack.append(HISTORY_FILE)
    if SETTINGS_FILE.exists():
        files_to_pack.append(SETTINGS_FILE)
    if not files_to_pack:
        warn("Нечего экспортировать — нет истории и настроек")
        return
    try:
        with tarfile.open(archive, "w:gz") as tar:
            for f in files_to_pack:
                tar.add(f, arcname=f.name)
        ok(f"Экспорт: {archive} ({archive.stat().st_size // 1024} КБ)")
        hint("Перенеси этот файл на другой ПК и распакуй вручную в ~/wine-portable/")
    except Exception as e:
        debug.dbg_exc(e, "cmd_export")
        err(f"Экспорт: {e}")


def cmd_gpu_temp():
    """Показать температуру GPU (nvidia-smi или sensors)."""
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        try:
            r = subprocess.run(
                [nvidia, "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                parts = [p.strip() for p in r.stdout.strip().split(",")]
                if len(parts) >= 4:
                    temp, util, mem_u, mem_t = parts[:4]
                    ok(f"NVIDIA GPU: {temp}°C, загрузка {util}%, память {mem_u}/{mem_t} МБ")
                    return
        except Exception as e:
            debug.dbg_exc(e, "cmd_gpu_temp/nvidia")
    sensors = shutil.which("sensors")
    if sensors:
        try:
            r = subprocess.run([sensors], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                found = False
                for line in r.stdout.splitlines():
                    if "temp" in line.lower() or "°C" in line:
                        print("  " + line)
                        found = True
                if found:
                    return
        except Exception as e:
            debug.dbg_exc(e, "cmd_gpu_temp/sensors")
    warn("Ни nvidia-smi, ни sensors не найдены")
    hint("Установи: flatpak --user install flathub org.kde.ksensors  (или lm_sensors в системе)")


def _cmd_download_local(url):
    """Скачать и распаковать архив по URL, затем запустить найденную игру.

    Тонкая обёртка над commands.cmd_download (ленивый импорт — иначе
    циклическая инициализация: commands реэкспортирует этот модуль).
    """
    from modules.commands import cmd_download
    cmd_download(url)


def cmd_free_games():
    """Каталог бесплатных игр — можно скачать одной командой."""
    print(f"\n{BOLD}Бесплатные игры:{RESET}")
    for i, (name, url) in enumerate(FREE_GAMES, 1):
        print(f"  {CYAN}{i:2d}{RESET}) {name}")
    print()
    hint("Введи номер, чтобы скачать и распаковать, или Enter для отмены")
    try:
        choice = input(f"{YELLOW}Номер: {RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        return
    if not choice:
        return
    try:
        idx = int(choice) - 1
        if not (0 <= idx < len(FREE_GAMES)):
            err("Неверный номер")
            return
    except ValueError:
        err("Не число")
        return
    name, url = FREE_GAMES[idx]
    info(f"Выбрана: {name}")
    _cmd_download_local(url)
