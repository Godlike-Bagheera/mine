# -*- coding: utf-8 -*-
"""Резервное копирование / перенос миров Minecraft (Windows 10).

На РЕД ОС миры лежали внутри Wine-префикса; на Windows — нативно в
%APPDATA%\\.minecraft\\saves (и в portable-папках лаунчера). Функции:
  cmd_save_worlds — архив всех миров установки в mc-portable/world-backup;
  cmd_load_worlds — распаковка обратно;
  cmd_worlds_menu — интерактивный список миров.
"""
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from modules import debug
from modules.colors import ok, info, warn, err, hint, CYAN, YELLOW, BOLD, RESET
from modules.config import WINE_DIR
from modules.prefix import pick_mc_dir_interactive

BACKUP_ROOT = WINE_DIR / "world-backup"


def _saves_dir(mc_dir: Path):
    p = Path(mc_dir) / "saves"
    return p if p.is_dir() else None


def cmd_save_worlds(arg=""):
    info(f"{BOLD}Резервное копирование миров{RESET}")
    mc = pick_mc_dir_interactive()
    if not mc:
        err("Установка Minecraft не найдена — сначала скачай игру в лаунчере")
        return False
    saves = _saves_dir(mc)
    if not saves:
        err(f"Папка saves не найдена в {mc}")
        return False
    worlds = [d for d in saves.iterdir() if d.is_dir()]
    if not worlds:
        warn("Миров нет — сохранять нечего")
        return False
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_ROOT / f"worlds-{stamp}.zip"
    info(f"Миры: {len(worlds)} → {dest}")
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            for w in worlds:
                for f in w.rglob("*"):
                    if f.is_file():
                        z.write(f, arcname=str(Path(w.name) / f.relative_to(w)))
        ok(f"Сохранено: {dest} ({dest.stat().st_size // 1024} КБ)")
        hint(f"Все бэкапы: {BACKUP_ROOT}")
        return True
    except Exception as e:
        debug.dbg_exc(e, "worlds/save")
        err(f"Архивация: {e}")
        return False


def cmd_load_worlds(arg=""):
    info(f"{BOLD}Восстановление миров из бэкапа{RESET}")
    if not BACKUP_ROOT.is_dir():
        err("Папка бэкапов пуста — сначала сделай save-worlds")
        return False
    archives = sorted(BACKUP_ROOT.glob("worlds-*.zip"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    if not archives:
        err("Архивов worlds-*.zip не найдено")
        return False
    print("Бэкапы (свежие первыми):")
    for i, a in enumerate(archives[:10], 1):
        print(f"  [{i}] {a.name}")
    try:
        s = input(f"{YELLOW}Номер для восстановления (Enter — отмена): "
                  f"{RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        return False
    if not (s.isdigit() and 1 <= int(s) <= len(archives)):
        info("Отменено")
        return False
    arc = archives[int(s) - 1]
    mc = pick_mc_dir_interactive()
    if not mc:
        err("Куда восстанавливать — установка Minecraft не найдена")
        return False
    saves = mc / "saves"
    saves.mkdir(parents=True, exist_ok=True)
    info(f"Распаковываю {arc.name} → {saves}")
    try:
        with zipfile.ZipFile(arc) as z:
            # защита от zip-slip
            for m in z.namelist():
                rp = (saves / m).resolve()
                if not str(rp).startswith(str(saves.resolve())):
                    raise RuntimeError(f"Подозрительный путь в архиве: {m}")
            z.extractall(saves)
        ok("Миры восстановлены")
        return True
    except Exception as e:
        debug.dbg_exc(e, "worlds/load")
        err(f"Распаковка: {e}")
        return False


def cmd_worlds_menu(arg=""):
    info(f"{BOLD}Миры{RESET}")
    mc = pick_mc_dir_interactive()
    if not mc:
        err("Minecraft не найден")
        return
    saves = _saves_dir(mc)
    if not saves:
        warn(f"saves пуст/нет в {mc}")
        return
    worlds = sorted([d for d in saves.iterdir() if d.is_dir()],
                    key=lambda p: p.stat().st_mtime, reverse=True)
    if not worlds:
        warn("Миров нет")
        return
    for w in worlds:
        size = sum(f.stat().st_size for f in w.rglob("*") if f.is_file())
        t = datetime.fromtimestamp(w.stat().st_mtime).strftime("%d.%m %H:%M")
        print(f"  {CYAN}{w.name}{RESET:<40} {t}  {size // 1024} КБ")
    hint(f"Копия: {saves}")
    print()
    print(f"  {YELLOW}s{RESET} — сохранить все миры в бэкап   "
          f"{YELLOW}l{RESET} — восстановить из бэкапа   "
          f"{YELLOW}o{RESET} — открыть папку")
    try:
        c = input("Команда: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return
    if c == "s":
        cmd_save_worlds()
    elif c == "l":
        cmd_load_worlds()
    elif c == "o":
        from modules.platform_utils import open_in_os
        open_in_os(saves)
