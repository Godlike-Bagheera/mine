# -*- coding: utf-8 -*-
"""Резервное копирование / перенос миров Minecraft (Windows 10).

На РЕД ОС миры лежали внутри Wine-префикса; на Windows — нативно в
%APPDATA%\\.minecraft\\saves (и в portable-папках лаунчера). Функции:
  cmd_save_worlds — архив всех миров установки в mc-portable/world-backup;
  cmd_load_worlds — распаковка обратно;
  cmd_worlds_menu — интерактивный список миров.
"""
import os
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


# ═══════════════ Разбор структуры бэкапа (миры из разных инстантов) ═════════

_WORLDS_ROOT_NAMES = {"minecraft-worlds", "world-backup", "worlds-backup"}
# каталоги, которые являются корнями .minecraft-подобных установок в бэкапе
_MC_MARKERS = ("versions", "assets", "libraries", "launcher_profiles.json")


def find_disk_d():
    """Ищет диск/папку с бэкапом миров (исторически «Диск D») внутри HOME.

    Возвращает Path или None. Также учитывает явный путь от пользователя.
    """
    from modules.config import HOME
    cands = []
    for base in (Path(HOME), Path(HOME) / "Рабочий стол",
                 Path(HOME) / "Desktop"):
        try:
            if not base.is_dir():
                continue
            for d in base.iterdir():
                if d.is_dir() and any((d / r).is_dir() for r in _WORLDS_ROOT_NAMES):
                    cands.append(d)
        except OSError as e:
            debug.dbg_exc(e, "worlds.find_disk_d")
    if cands:
        return max(cands, key=lambda p: p.stat().st_mtime)
    return None


def _looks_like_world(p: Path) -> bool:
    """Каталог похож на мир: level.dat + (region|DIM-1|DIM1|data)."""
    try:
        if not p.is_dir():
            return False
        if not (p / "level.dat").exists():
            return False
        return any((p / m).exists() for m in ("region", "DIM1", "DIM-1", "data"))
    except OSError:
        return False


def _iter_saves_dirs(root: Path):
    """Все папки saves/saves.bak-*/worlds внутри дерева root — без
    сплошного рекурсивного обхода «всего подряд»: спускаемся только
    по осмысленным веткам (инстанты и их saves/saves.bak-*/worlds)."""
    out = []
    seen = set()

    def add(d: Path, inst: str, snap: str):
        try:
            rp = str(d.resolve())
        except OSError:
            return
        if rp in seen:
            return
        seen.add(rp)
        try:
            mt = d.stat().st_mtime
        except OSError:
            mt = 0.0
        out.append((d, inst, snap, mt))

    def walk(d: Path, depth: int):
        try:
            rp = str(d.resolve())
        except OSError:
            return
        if rp in seen or depth > 6:
            return
        seen.add(rp)
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for e in entries:
            if not e.is_dir():
                continue
            name = e.name.lower()
            if name == "node_modules":
                continue
            # снимки saves.bak-* и скрытые каталоги (.minecraft/.config) —
            # НЕ пропускаем: внутри них живут реальные saves-директории
            if name.startswith(".") and not name.startswith(".minecraft"):
                continue
            if name.startswith("saves") or name == "worlds":
                # сам мир тоже может называться saves...? нет: у мира есть
                # level.dat, а у saves-директории миров внутри — нет
                if not (e / "level.dat").exists():
                    inst = e.parent.name or "root"
                    snap = name if name.startswith("saves.") else ""
                    add(e, inst, snap)
                    continue
            # мир целиком (не спускаемся в region/DIM-области)
            if (e / "level.dat").exists():
                continue
            # каталог-обёртка инстанта (.minecraft/Prism_Minecraft/wine_prefix)
            walk(e, depth + 1)

    root_name = root.name.lower()
    if root_name.startswith("saves") or root_name == "worlds":
        add(root, root.parent.name or "root",
            root_name if root_name.startswith("saves.") else "")
    else:
        walk(root, 0)
    return out


def _collect_backup_worlds(backup_dir):
    """Собирает миры из дерева бэкапа: {путь_мира: уникальное_имя}.

    Фиксы дефектов:
    1. Коллизия имён (несколько «New World» из разных инстантов/снимков) —
       проигравшие получают метку [инстант-снимок], молчаливой потери нет;
    2. Вложенные datapack-области (World/DIM-1/DIM1) не считаются мирами —
       мир берётся целиком по level.dat+region на верхнем уровне;
    3. Обход целевой (только saves*/worlds, без сплошного рекурсивного
       сканирования всего дерева бэкапа);
    """
    backup_dir = Path(backup_dir)
    found = {}          # path -> display name
    if not backup_dir.is_dir():
        return found

    worlds = _iter_saves_dirs(backup_dir)

    # свежие снимки первыми: победитель коллизии — самый новый
    worlds.sort(key=lambda t: t[3], reverse=True)

    used_names = {}
    for sd, inst, snap, _mt in worlds:
        try:
            entries = sorted(sd.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for w in entries:
            if not _looks_like_world(w):
                continue
            base_name = w.name
            if base_name in used_names:
                tag = f"[{inst}" + (f" {snap[:8]}" if snap else "") + "]"
                disp = f"{base_name} {tag}"
                n = 2
                while disp in used_names:
                    disp = f"{base_name} {tag}#{n}"
                    n += 1
            else:
                disp = base_name
            used_names[disp] = w
            found[w] = disp
    return found


def get_saves_dirs():
    """Живые папки saves: [(saves_path, instance_name)] — бэкапы исключены.

    Источники: установки из find_minecraft_dirs (с versions/) плюс portable-
    корни Legacy Launcher, где saves есть, а versions ещё пуст.
    """
    from modules.prefix import find_minecraft_dirs, _in_worlds_backup
    out, seen = [], set()

    def push(mc):
        try:
            sd = mc / "saves"
            if not sd.is_dir():
                return
            rp = str(sd.resolve())
        except OSError:
            return
        if rp in seen or _in_worlds_backup(sd):
            return
        seen.add(rp)
        out.append((sd, mc.name if mc.name != "saves" else mc.parent.name))

    for mc in find_minecraft_dirs(limit=12):
        push(Path(mc))
    return out


def choose_saves_target(saves_list):
    """Выбор целевой папки saves при нескольких лаунчерах (фикс №4).

    Одна папка — берётся без спроса; пустой ввод — первый вариант
    (обратная совместимость); некорректный номер — откат к первому.
    """
    if not saves_list:
        return None
    if len(saves_list) == 1:
        return saves_list[0][0]
    print("Найдено несколько установок — куда восстанавливать?")
    for i, (sd, name) in enumerate(saves_list, 1):
        print(f"  [{i}] {name}: {sd}")
    try:
        s = input("Номер (Enter — первый): ").strip()
    except (KeyboardInterrupt, EOFError):
        return saves_list[0][0]
    if not s:
        return saves_list[0][0]
    if s.isdigit() and 1 <= int(s) <= len(saves_list):
        return saves_list[int(s) - 1][0]
    warn("Некорректный номер — беру первый вариант")
    return saves_list[0][0]


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
                for dirpath, _dirs, files in os.walk(w):
                    for fn in files:
                        fp = Path(dirpath) / fn
                        z.write(fp, arcname=str(Path(w.name)
                                                / fp.relative_to(w)))
        ok(f"Сохранено: {dest} ({dest.stat().st_size // 1024} КБ)")
        hint(f"Все бэкапы: {BACKUP_ROOT}")
        return True
    except Exception as e:
        debug.dbg_exc(e, "worlds/save")
        err(f"Архивация: {e}")
        return False


def cmd_load_worlds(arg=""):
    info(f"{BOLD}Восстановление миров из бэкапа{RESET}")
    archives = []
    backup_tree = None
    if BACKUP_ROOT.is_dir():
        archives = sorted(BACKUP_ROOT.glob("worlds-*.zip"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        backup_tree = BACKUP_ROOT
    if not archives:
        # альтернатива: «Диск D» с деревом minecraft-worlds (миграция с РЕД ОС)
        disk = find_disk_d()
        if disk is not None:
            for r in _WORLDS_ROOT_NAMES:
                if (disk / r).is_dir():
                    backup_tree = disk / r
                    break
            else:
                backup_tree = disk
    if backup_tree is None and not archives:
        try:
            s = input("Укажи путь к папке с бэкапом миров (Enter — отмена): ").strip()
        except (KeyboardInterrupt, EOFError):
            return False
        if s and Path(s).is_dir():
            backup_tree = Path(s)
    if backup_tree is None and not archives:
        err("Бэкапов не найдено — сначала сделай save-worlds")
        return False

    # куда восстанавливать: живой выбор среди нескольких установок (фикс №4)
    saves_list = get_saves_dirs()
    target = choose_saves_target(saves_list) if saves_list else None
    if target is None:
        mc = pick_mc_dir_interactive()
        if not mc:
            err("Куда восстанавливать — установка Minecraft не найдена")
            return False
        target = mc / "saves"
    target.mkdir(parents=True, exist_ok=True)

    restored = 0
    # 1) zip-архивы наших же save-worlds
    for arc in archives[:1]:
        try:
            with zipfile.ZipFile(arc) as z:
                for m in z.namelist():        # защита от zip-slip
                    rp = (target / m).resolve()
                    if not str(rp).startswith(str(target.resolve())):
                        raise RuntimeError(f"Подозрительный путь в архиве: {m}")
                z.extractall(target)
            ok(f"Распакован архив: {arc.name}")
            restored += 1
        except Exception as e:
            debug.dbg_exc(e, "worlds/load-zip")
            err(f"Распаковка {arc.name}: {e}")

    # 2) дерево бэкапа (разные инстанты/снимки, коллизии имён — с метками)
    if backup_tree is not None:
        found = _collect_backup_worlds(backup_tree)
        if found:
            import shutil
            for wpath, disp in found.items():
                dest = target / disp
                try:
                    if dest.exists():
                        continue
                    shutil.copytree(wpath, dest)
                    restored += 1
                except Exception as e:
                    debug.dbg_exc(e, f"worlds/load-tree {wpath}")
            ok(f"Из дерева бэкапа восстановлено миров: {len(found)}")

    if restored:
        ok("Миры восстановлены")
        return True
    warn("Восстанавливать нечего")
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
        size = sum(fn.stat().st_size for dp, _dd, fs in os.walk(w)
                   for f2 in fs if (fn := Path(dp) / f2).is_file())
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
