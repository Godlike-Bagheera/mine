# -*- coding: utf-8 -*-
"""Поиск игровых директорий Minecraft на текущей ОС.

Раньше этот модуль искал .minecraft внутри Wine-префиксов (РЕД ОС).
На Windows 10 префиксы не нужны: ищем нативные директории —
%APPDATA%\\.minecraft и portable-установки Legacy Launcher рядом с
launcher.jar, плюс все версии из versions/ для подбора шейдеров/модпаков.
Linux-ветка оставлена для совместимости (тесты гоняются на Linux).
"""
import json
import os
import re
from pathlib import Path

from modules import debug
from modules.config import WINE_DIR
from modules.platform_utils import IS_WINDOWS, user_home


def _looks_minecraft(p: Path) -> bool:
    """Каталог похож на корень .minecraft (versions/ + любой из маркеров)."""
    try:
        if not p.is_dir():
            return False
        if not (p / "versions").is_dir():
            return False
        for marker in ("assets", "libraries", "launcher_profiles.json",
                       ".minecraft"):
            if (p / marker).exists():
                return True
        # portable-инстанты лаунчеров (Legacy/Prism): versions непустой ИЛИ
        # есть saves — иначе пустые portable-корни не находились совсем
        if (p / "saves").is_dir():
            return True
        return any((p / "versions").iterdir())
    except Exception as e:
        debug.dbg_exc(e, f"prefix._looks_minecraft({p})")
        return False


def native_minecraft_roots() -> list:
    """Все известные расположения .minecraft на этой машине (без сортировки)."""
    if IS_WINDOWS:
        appdata = user_home() / "AppData" / "Roaming"
        cand = [appdata / ".minecraft"]
        # Legacy Launcher portable: %APPDATA%\LegacyLauncher и папки рядом
        legacy_cfg = appdata / "legacylauncher" / "settings.json"
        try:
            if legacy_cfg.exists():
                data = json.loads(legacy_cfg.read_text(encoding="utf-8-sig",
                                                        errors="ignore"))
                for key in ("gameDir", "gamePath", "lastSelectedInstance"):
                    v = data.get(key)
                    if isinstance(v, str) and v:
                        cand.append(Path(v))
        except Exception as e:
            debug.dbg_exc(e, "prefix.legacy_settings")
        # portable-корни: куда обычно распаковывают Legacy portable zip
        for base in (user_home(), user_home() / "Downloads",
                     user_home() / "Desktop", Path("D:/"), Path("C:/")):
            for name in ("LegacyLauncher", "legacy", "Minecraft",
                         ".minecraft", "minecraft"):
                cand.append(base / name)
    else:
        cand = [user_home() / ".minecraft"]
        # Wine-префиксы (наследие РЕД ОС — на случай миграции/тестов)
        prefixes = WINE_DIR / "prefixes"
        if prefixes.is_dir():
            for pr in prefixes.iterdir():
                cand.append(pr / "drive_c" / "users" / "user"
                            / "AppData" / "Roaming" / ".minecraft")
        cand.append(WINE_DIR / "prefix" / "drive_c" / "users" / "user"
                    / "AppData" / "Roaming" / ".minecraft")
        cand.append(user_home() / "wine-portable" / "games")

    out, seen = [], set()
    for c in cand:
        try:
            rp = c.resolve()
        except Exception:
            continue
        key = str(rp).lower()
        if key in seen:
            continue
        seen.add(key)
        if _looks_minecraft(rp):
            out.append(rp)
    return out


def _in_worlds_backup(p: Path) -> bool:
    """True, если путь лежит внутри папки-бэкапа миров (minecraft-worlds /
    world-backup). Такие копии — не живые установки: их нельзя предлагать
    как игровую директорию и целевую папку saves."""
    try:
        parts = {q.lower() for q in Path(p).parts}
    except Exception:
        return False
    return ("minecraft-worlds" in parts or "world-backup" in parts
            or "worlds-backup" in parts)


def find_minecraft_dirs(limit: int = 12) -> list:
    """Список путей .minecraft, отсортированных по свежести использования."""
    roots = [r for r in native_minecraft_roots() if not _in_worlds_backup(r)]
    # дополняем shallow-поиск: HOME/*/.minecraft (Prism/ATL/MultiMC и т.п.)
    # и инстанты лаунчеров по общепринятым именам папок: HOME/<Имя>/.minecraft,
    # HOME/<Имя>/minecraft (например HOME/Prism/.minecraft).
    try:
        base = user_home()
        if base.is_dir():
            for d in base.iterdir():
                if not d.is_dir():
                    continue
                cands = []
                if d.name.lower().startswith(".minecraft"):
                    cands.append(d)
                for sub in (".minecraft", "minecraft"):
                    cands.append(d / sub)
                for cand in cands:
                    if cand.is_dir() and not _in_worlds_backup(cand) \
                            and _looks_minecraft(cand):
                        roots.append(cand.resolve())
    except OSError as e:
        debug.dbg_exc(e, "prefix.find_minecraft_dirs.shallow")

    def mtime(p: Path) -> float:
        best = p.stat().st_mtime
        try:
            vs = p / "versions"
            if vs.is_dir():
                for v in vs.iterdir():
                    best = max(best, v.stat().st_mtime)
        except Exception:
            pass
        return best

    uniq, seen = [], set()
    for r in roots:
        k = str(r).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    uniq.sort(key=mtime, reverse=True)
    return uniq[:limit]


def latest_used_dir():
    dirs = find_minecraft_dirs(limit=1)
    return dirs[0] if dirs else None


def installed_versions(mc_dir: Path) -> list:
    """Версии Minecraft, реально установленные в данной директории.

    Возвращает список словарей {'id', 'mc_version', 'path'}: id — имя папки
    versions/<id>, mc_version — базовая версия MC (из json или из имени).
    Используется командой `fo`, чтобы поставить модпак «точно той же
    версии, что уже скачана в лаунчере».
    """
    result = []
    try:
        vs = Path(mc_dir) / "versions"
        if not vs.is_dir():
            return result
        for d in sorted(vs.iterdir(), key=lambda p: p.stat().st_mtime,
                        reverse=True):
            if not d.is_dir():
                continue
            j = d / f"{d.name}.json"
            mc = None
            if j.exists():
                try:
                    data = json.loads(j.read_text(encoding="utf-8",
                                                  errors="ignore"))
                    mc = data.get("minecraftVersion") or None
                    if not mc and data.get("inheritsFrom"):
                        mc = data["inheritsFrom"]
                except Exception as e:
                    debug.dbg_exc(e, f"prefix.version_json({d.name})")
            if not mc:
                m = re.search(r"\b(1\.\d+(?:\.\d+)?)\b", d.name)
                mc = m.group(1) if m else None
            result.append({"id": d.name, "mc_version": mc, "path": d})
    except Exception as e:
        debug.dbg_exc(e, "prefix.installed_versions")
    return result


def pick_mc_dir_interactive(ui_ok=None):
    """Предложить пользователю выбрать игровую директорию (или взять свежую)."""
    dirs = find_minecraft_dirs(limit=12)
    if not dirs:
        return None
    if len(dirs) == 1:
        return dirs[0]
    print("Найдено несколько установок Minecraft:")
    for i, d in enumerate(dirs, 1):
        print(f"  [{i}] {d}")
    print("  [0] самая свежая (авто)")
    try:
        s = input("Выбор: ").strip()
    except EOFError:
        return dirs[0]
    if s.isdigit() and 1 <= int(s) <= len(dirs):
        return dirs[int(s) - 1]
    return dirs[0]


def choose_minecraft_dir(auto: bool = True):
    """Выбрать игровую директорию (.minecraft / portable game-папку).

    auto=True — в небоевом/неинтерактивном режиме берём самую свежую;
    иначе предлагаем список и ручной ввод пути. Возвращает Path или None.
    """
    dirs = find_minecraft_dirs(limit=12)
    if auto and dirs:
        return dirs[0]
    if not dirs:
        return None
    if len(dirs) == 1:
        return dirs[0]
    print("Найдено несколько установок Minecraft:")
    for i, d in enumerate(dirs, 1):
        print(f"  [{i}] {d}")
    print("  [0] самая свежая (авто)")
    try:
        s = input("Выбор: ").strip()
    except (EOFError, KeyboardInterrupt):
        return dirs[0]
    if s.isdigit() and 1 <= int(s) <= len(dirs):
        return dirs[int(s) - 1]
    return dirs[0]


def show_minecraft_paths():
    """Команда «пути»: показать все найденные игровые директории и версии."""
    dirs = find_minecraft_dirs(limit=24)
    if not dirs:
        warn = print
        warn("Игровые директории Minecraft не найдены.")
        warn("Скачай версию через меню legacy (или запусти любой лаунчер один раз).")
        return
    print(f"Найдено установок: {len(dirs)}")
    for i, d in enumerate(dirs, 1):
        vers = installed_versions(d)
        mc_list = sorted({v["mc_version"] for v in vers if v["mc_version"]})
        tag = "  (самая свежая)" if i == 1 else ""
        print(f"\n[{i}] {d}{tag}")
        print(f"    версий: {len(vers)}, Minecraft: "
              + (", ".join(mc_list[-8:]) if mc_list else "—"))
        shaderp = d / "shaderpacks"
        if shaderp.is_dir():
            try:
                n = len([p for p in shaderp.iterdir()
                         if p.suffix.lower() in (".zip", ".7z", ".lgsl",
                                                 ".glsl")])
                print(f"    шейдеров установлено: {n}")
            except Exception:
                pass


def get_wine_env(exe_path=None):
    """Окружение Wine — на Windows 10 не используется (заглушка совместимости).

    Старые wine-ветки launcher.py/commands.py/ui.py вызывают её с
    необязательным exe_path; возвращаем копию os.environ, чтобы любой
    subprocess.run(..., env=get_wine_env()) работал как обычно.
    """
    return dict(os.environ)


def ensure_prefix(exe_path=None):
    """Совместимость с wine-версией скрипта: на Windows префиксы не нужны.

    Возвращаем True (и создаём каталог данных, если вдруг отсутствует),
    чтобы вызывающий код launcher.py работал без изменений.
    """
    try:
        WINE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        debug.dbg_exc(e, "prefix.ensure_prefix")
    return True
