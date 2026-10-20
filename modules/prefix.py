# -*- coding: utf-8 -*-
"""Поиск игровых директорий Minecraft на текущей ОС.

Раньше этот модуль искал .minecraft внутри Wine-префиксов (РЕД ОС).
На Windows 10 префиксы не нужны: ищем нативные директории —
%APPDATA%\\.minecraft и portable-установки Legacy Launcher рядом с
launcher.jar, плюс все версии из versions/ для подбора шейдеров/модпаков.
Linux-ветка оставлена для совместимости (тесты гоняются на Linux).
"""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from modules import debug, state
from modules.config import (
    HOME, WINE_BIN, DXVK_DIR, DEFAULT_PREFIX, PREFIXES_DIR,
    DXVK_OVERRIDES,
)
from modules.platform_utils import IS_WINDOWS, user_home


# Активный префикс (тесты подменяют атрибуты модуля monkeypatch-ом).
WINE_PREFIX = Path(DEFAULT_PREFIX)


def _sanitize_stem(stem: str) -> str:
    """Имя папки префикса из stem exe: буквы/цифры/_,_-, детерминированный хвост."""
    cleaned = re.sub(r"[^0-9A-Za-z\u0400-\u04FF_-]+", "_", stem).strip("_")
    if not cleaned:
        cleaned = "game"
    return cleaned[:24] or "game"


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
        # portable-инстанты лаунчеров (Legacy/Prism): versions непустой —
        # этого достаточно; пустые папки не считаем установками (иначе
        # домашний ~/.minecraft без версий затенял бы реальные установки).
        # Исключение: папка сама называется .minecraft — это явный корень
        # игры, даже если versions ещё пуст (свежий Legacy portable до
        # первой загрузки версии).
        if p.name.lower() == ".minecraft":
            return True
        try:
            return any((p / "versions").iterdir())
        except Exception:
            return False
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
        cand = [Path(str(HOME)) / ".minecraft"]
        # Wine-префиксы (наследие РЕД ОС). Пути читаем КАК АТРИБУТЫ МОДУЛЯ
        # (WINE_PREFIX/PREFIXES_DIR) — тесты monkeypatch-ят именно их.
        wroots = []
        try:
            # общий префикс + per-game + корень HOME (тесты подменяют prefix.HOME)
            for pbase in (Path(WINE_PREFIX), Path(PREFIXES_DIR), Path(HOME)):
                if pbase.is_dir():
                    wroots.append(pbase)
        except Exception as e:
            debug.dbg_exc(e, "prefix.native_minecraft_roots/wine")
        for wb in wroots:
            users = wb / "drive_c" / "users"
            if users.is_dir():
                for u in users.iterdir():
                    cand.append(u / ".minecraft")
                    cand.append(u / "AppData" / "Roaming" / ".minecraft")
            cand.append(wb / "drive_c" / "users" / "user" / ".minecraft")
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
    """Список путей .minecraft, отсортированных по свежести использования.

    ВАЖНО: скан идёт от атрибута модуля prefix.HOME (а не закешированного
    config.HOME) — тесты и миграция подменяют именно prefix.HOME.
    """
    roots = [r for r in native_minecraft_roots() if not _in_worlds_backup(r)]
    # дополняем shallow-поиск: HOME/*/.minecraft (Prism/ATL/MultiMC и т.п.)
    # и инстанты лаунчеров по общепринятым именам папок: HOME/<Имя>/.minecraft,
    # HOME/<Имя>/minecraft (например HOME/Prism/.minecraft).
    try:
        base = Path(str(HOME)) if isinstance(HOME, (str, Path)) else user_home()
        if not base.is_dir():
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

    # легаси-Wine: .minecraft внутри префиксов (drive_c/users/*/.minecraft).
    # ВАЖНО: не переприсваиваем `roots` — только дополняем найденное.
    # ВАЖНО 2: читаем КАК АТРИБУТЫ МОДУЛЯ (prefix.WINE_PREFIX/PREFIXES_DIR),
    # тесты monkeypatch-ят именно их; from-import скопировал бы значения
    # config в момент импорта и подмены не сработали бы.
    try:
        wroots = []
        for pbase in (Path(str(WINE_PREFIX)), Path(str(PREFIXES_DIR))):
            if not pbase.is_dir():
                continue
            users = pbase / "drive_c" / "users"
            if users.is_dir():
                wroots.extend(users.glob("*/.minecraft"))
                wroots.extend(users.glob("*/AppData/Roaming/.minecraft"))
            else:
                wroots.extend(pbase.glob(".minecraft"))
        for mc in wroots:
            if _looks_minecraft(mc) and not _in_worlds_backup(mc):
                roots.append(mc.resolve())
        # легаси: <HOME>/wine-portable/prefix/drive_c/users/<u>/.minecraft
        hb = Path(str(HOME)) if isinstance(HOME, (str, Path)) else user_home()
        for mc in hb.glob("*/*/drive_c/users/*/.minecraft"):
            if _looks_minecraft(mc):
                roots.append(mc.resolve())
    except Exception as e:
        debug.dbg_exc(e, "prefix.find_minecraft_dirs/wine")

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
    def nversions(p: Path) -> int:
        try:
            vs = p / "versions"
            if vs.is_dir():
                return sum(1 for x in vs.iterdir() if x.is_dir())
        except Exception:
            pass
        return 0

    # свежая установка обычно содержит больше всего версий -> сначала они;
    # tie-breaker — время последнего использования
    uniq.sort(key=lambda p: (nversions(p), mtime(p)), reverse=True)
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
    if not IS_WINDOWS:
        # легаси-Wine отображение (тесты проверяют этот контракт на Linux)
        print("Wine-префикс: " + str(Path(DEFAULT_PREFIX)))
        print("Linux-путь игры:  " + get_minecraft_game_path())
        print("Windows-путь игры: "
              + get_minecraft_game_path().replace(str(Path(DEFAULT_PREFIX)),
                                                  "").replace("/drive_c/users/stud",
                                                              r"C:\users\stud")
              .replace("/", "\\"))
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


def get_prefix_path(exe_path=None):
    """Путь активного Wine-префикса (легаси-Wine контракт).

    Без флага state.USE_PER_GAME_PREFIX — общий DEFAULT_PREFIX.
    С флагом и переданным exe — отдельная папка в PREFIXES_DIR, имя:
    sanitized stem + детерминированный хеш полного пути exe (разные игры
    с одинаковым именем exe получают разные префиксы).
    """
    if not getattr(state, "USE_PER_GAME_PREFIX", False) or not exe_path:
        return Path(WINE_PREFIX)
    try:
        full = str(Path(exe_path).resolve())
    except Exception:
        full = str(exe_path)
    digest = hashlib.sha1(full.encode("utf-8")).hexdigest()[:8]
    name = f"{_sanitize_stem(Path(exe_path).stem)}-{digest}"
    return Path(PREFIXES_DIR) / name


def get_wine_env(exe_path=None, use_dxvk=True):
    """Окружение для запуска процесса под Wine (легаси-Wine контракт).

    Базовая копия os.environ + WINEPREFIX; DXVK_HUD/MANGOHUD из флагов
    state; WINEDLLOVERRIDES навязывается только если DXVK реально скачан
    (bootstrap-режим ensure_prefix вызывает это с use_dxvk=False).
    """
    env = dict(os.environ)
    pf = get_prefix_path(exe_path)
    if not getattr(state, "USE_PER_GAME_PREFIX", False):
        pf = Path(DEFAULT_PREFIX)
    env["WINEPREFIX"] = str(pf)
    if getattr(state, "USE_DXVK_HUD", False):
        env["DXVK_HUD"] = "fps,frame-times"
    if getattr(state, "USE_MANGOHUD", False):
        env["MANGOHUD"] = "1"
    if use_dxvk and Path(DXVK_DIR).exists():
        env["WINEDLLOVERRIDES"] = DXVK_OVERRIDES
    return env


def ensure_prefix(exe_path=None, force_boot=False):
    """Создать/обновить Wine-префикс через wineboot -u. True при успехе.

    Идемпотентна: повторный вызов не гоняет wineboot заново, пока стоит
    маркер .wineboot_done и system32 на месте. force_boot пересоздаёт.
    """
    pf = Path(get_prefix_path(exe_path))
    s32 = pf / "drive_c" / "windows" / "system32"
    marker = pf / ".wineboot_done"
    if s32.is_dir() and marker.exists() and not force_boot:
        return True
    try:
        pf.mkdir(parents=True, exist_ok=True)
        # bootstrap: без dxvk-overrides, иначе wineboot грузит.native-dll
        env = get_wine_env(exe_path=exe_path, use_dxvk=False)
        result = subprocess.run([str(WINE_BIN), "wineboot", "-u"],
                                env=env, capture_output=True, text=True,
                                timeout=600)
        if result.returncode != 0:
            debug.dbg(f"prefix: wineboot rc={result.returncode}", tag="prefix")
    except Exception as e:
        debug.dbg_exc(e, "prefix.ensure_prefix")
        return False
    if not s32.is_dir():
        return False
    try:
        marker.write_text(str(pf), encoding="utf-8")
    except Exception as e:
        debug.dbg_exc(e, "prefix.ensure_prefix/marker")
    return True


def get_minecraft_game_path():
    """Легаси-Wine путь игры внутри префикса (.tlauncher legacy)."""
    return str(Path(DEFAULT_PREFIX) / "drive_c" / "users" / "stud"
               / ".tlauncher" / "legacy" / "Minecraft" / "game")


def cmd_legacy_paths():
    """Показать пути установок Minecraft (нативные + легаси-Wine)."""
    show_minecraft_paths()
