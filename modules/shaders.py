"""Установка шейдер-паков для Minecraft (Complementary, BSL и др.).

Логика:
1. Определяем запущенную/установленную версию Minecraft:
   - активные фоновые игры лаунчера (modules.gamestate);
   - процессы java/wine с minecraft в командной строке;
   - папки .minecraft — по времени последнего изменения (последняя игра).
2. Предлагаем популярные паки: живой каталог через Modrinth API + офлайн-
   список (на случай школьной сети без интернета).
3. Скачанный zip валидируется (сигнатура PK, наличие файлов шейдеров)
   и кладётся в shaderpacks/ выбранной игры.

Шейдеры работают с OptiFine / Iris / Fabulously Optimized.
"""
import json
import re
import shutil
import zipfile
import urllib.parse
import urllib.request
import subprocess
from pathlib import Path

from modules import debug
from modules.colors import (ok, info, warn, err, hint, CYAN, YELLOW, BOLD,
                            GREEN, RESET)
from modules.config import WINE_DIR, make_github_mirrors
from modules.download import download_file
from modules.prefix import find_minecraft_dirs

MODRINTH_SEARCH = ("https://api.modrinth.com/v2/search?"
                   "query={query}&facets=%5B%5B%22project_type%3Ashader%22%5D%5D"
                   "&limit=8&index=relevance")

# Офлайн-каталог: если Modrinth API недоступен (школа, нет сети).
# Ссылки — на стабильные CDN/GitHub-URLы с фиксированной версией (проверены):
# «latest/download» у этих паков отдаёт 404, поэтому версии указаны явно.
OFFLINE_SHADERS = [
    {
        "name": "ComplementaryReimagined",
        "desc": "самый популярный универсальный пак (низкие/средние требования)",
        "url": "https://github.com/ComplementaryDevelopment/ComplementaryReimagined/"
               "releases/download/latest/ComplementaryReimagined_r5.4.zip",
        # r5.4 стабилен; свежие r5.x лежат на Modrinth-CDN — запасной вариант
        "alt_urls": ["https://cdn.modrinth.com/data/HVnmMxH1/versions/"
                     "Bqen1mJX/ComplementaryReimagined_r5.9.3.zip"],
    },
    {
        "name": "BSL Shaders",
        "desc": "мягкое освещение, средняя нагрузка",
        "url": "https://cdn.modrinth.com/data/Q1vvjJYV/versions/"
               "Y68yiql9/BSL_v10.1.8.zip",
        "alt_urls": [],
    },
    {
        "name": "Sildur's Vibrant Shaders",
        # Основным — Lite: целевое железо (i5-9400F + GTX 1660) тянет его с
        # запасом; Extreme на 1660 даёт ~15-25 FPS и не является разумным
        # дефолтом. Качество крутится в настройках пака (Lite→Extreme).
        "desc": "яркий классический пак, легко крутить качество (Lite→Extreme)",
        "url": "https://cdn.modrinth.com/data/z8EjLYqN/versions/"
               "cianYi38/Sildur%27s%20Vibrant%20Shaders%20v2.02%20Lite.zip",
        "alt_urls": ["https://cdn.modrinth.com/data/z8EjLYqN/versions/"
                     "EisvxIzu/Sildur%27s%20Vibrant%20Shaders%20v2.02%20Medium.zip",
                     "https://cdn.modrinth.com/data/z8EjLYqN/versions/"
                     "J4RlzpKF/Sildur%27s%20Vibrant%20Shaders%20v2.02%20Extreme.zip"],
    },
    {
        "name": "Chocapic13 V9",
        "desc": "классика, хорошо идёт на слабых ПК "
                "(рекомендован для GTX 1660 / средних ПК)",
        "url": "https://cdn.modrinth.com/data/xM96NJmk/versions/"
               "hOdwb79n/YoFPS%2BHigh%2B%28Chocapic13%2BEdit%29.zip",
        "alt_urls": [],
    },
    {
        "name": "Nostalgia Shader",
        "desc": "ретро-вид «как в старых версиях»",
        "url": "https://cdn.modrinth.com/data/xEItlMn3/versions/"
               "fzxeGgx7/Nostalgia_v5.1.zip",
        "alt_urls": [],
    },
]


def _http_get(url, timeout=30, headers=None):
    """GET с фолбеком строгой проверки сертификатов (см. net.ssl_ctx)."""
    from modules.download import open_checked
    req = urllib.request.Request(url, headers=headers or {})
    return open_checked(req, timeout)


# ─────────────────────────────────────────────────────────────────────
#  Определение версии Minecraft (запущенная / последняя установленная)
# ─────────────────────────────────────────────────────────────────────

def _extract_mc_version(line):
    """Версия Minecraft из путей вида versions/1.xx.x/ в строке запуска.

    Ищет самое длинное совпадение — иначе жадный regex может взять
    '1.0' вместо '1.20.1'. Возвращает str или None.
    """
    best = None
    for m in re.finditer(r"versions[/\\](1[.\w\-+]*\d)[/\\]", line):
        v = m.group(1)
        if best is None or len(v) > len(best):
            best = v
    return best


def detect_running_mc_version():
    """Ищет запущенный процесс Minecraft (java с -cp ... или wine).

    Возвращает dict {"version": str|None, "dir": Path|None, "running": bool}.
    """
    result = {"version": None, "dir": None, "running": False}
    # 1) фоновые игры самого лаунчера (реестр gamestate)
    try:
        from modules import gamestate
        for name, g in gamestate.running().items():
            low = (name + " " + g["path"]).lower()
            if "minecraft" in low or "tlauncher" in low or "prism" in low:
                result["running"] = True
                result["dir"] = _mc_dir_from_path(Path(g["path"]))
                # Версия из путей вида versions/1.xx.x/ в имени/пути игры;
                # если её нет — берём последнюю игранную из профилей папки.
                ver = _extract_mc_version(name + " " + g["path"])
                if not ver and result["dir"] is not None:
                    vers = installed_mc_versions(result["dir"])
                    last = _last_played_version(result["dir"])
                    ver = last if last in vers else (vers[0] if vers else None)
                if ver:
                    result["version"] = ver
                break
    except Exception as e:
        debug.dbg(f"detect_running_mc gamestate: {e}")
    # 2) процессы системы
    try:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception as e:
        debug.dbg(f"detect_running_mc ps: {e}")
        return result
    for line in out.splitlines():
        low = line.lower()
        if "minecraft" not in low and "tlauncher" not in low and ".minecraft" not in low:
            continue
        # «shaders» убрано из фильтра: процесс игры с словом "shaders"
        # в аргументах запуска (путь к shaderpacks, имя шейдер-пака)
        # раньше отбрасывался — запущенная игра «не виделась».
        if "grep " in low or "wine.py" in low:
            continue
        result["running"] = True
        # Версия игры из путей вида .../versions/1.xx.x/...
        # (jar, natives, assetIndex в аргументах java/wine).
        ver = _extract_mc_version(line)
        if ver and not result["version"]:
            result["version"] = ver
        # Папка игры из аргументов лаунчера (--gameDir / --worldDir / userHome).
        if result["dir"] is None:
            gm = re.search(r"--gameDir[\s=]\"?([^\s\"]+)", line)
            if gm:
                gd = _mc_dir_from_path(Path(gm.group(1)).expanduser())
                if gd is not None:
                    result["dir"] = gd
        m2 = re.search(r"([^\s\"']*/\.minecraft)", line)
        if m2 and result["dir"] is None:
            d = Path(m2.group(1)).expanduser()
            if d.is_dir():
                result["dir"] = d
        if result["version"] and result["dir"]:
            break
    return result


def _mc_dir_from_path(p):
    """По пути .exe/.jar игры определяет папку .minecraft рядом с ней."""
    try:
        for parent in [p] + list(p.parents):
            if parent.name.lower() in (".minecraft", "game") or \
               "minecraft" in parent.name.lower():
                if parent.is_dir():
                    return parent
    except OSError:
        pass
    return None


def _profile_version(data, sel):
    """Версия Minecraft из launcher_profiles.json по выбранному профилю.

    Vanilla/TLauncher: selectedProfile — id профиля в profiles{}, где
    lastVersionId = версия (например "1.20.1").
    PrismLauncher: selectedProfile — имя профиля, версия — lastUsedVersion.
    Если по id не нашлось — возвращаем sel как есть (может быть самой версией).
    """
    def ver_of(p):
        if isinstance(p, dict):
            return p.get("lastVersionId") or p.get("lastUsedVersion")
        return None

    if isinstance(sel, str) and sel:
        # TLauncher иногда пишет полный путь .../versions/1.20.1
        m = re.search(r"versions[/\\](.+)$", sel.replace("\\", "/"))
        if m:
            return m.group(1)
        prof = (data.get("profiles") or {}).get(sel)
        v = ver_of(prof)
        if v:
            return v
        # selectedProfile может быть и самой версией
        if re.match(r"^1[.\d]", sel):
            return sel
    # без selectedProfile: последний профиль с известной версией
    best = None  # (lastUsed, версия)
    for prof in (data.get("profiles") or {}).values():
        if isinstance(prof, dict):
            v = ver_of(prof)
            if v:
                lu = prof.get("lastUsed") or ""
                if best is None or lu > best[0]:
                    best = (lu, v)
    if best:
        return best[1]
    return None


def _last_played_version(mc_dir):
    """Последняя игранная версия из launcher_profiles.json.

    Читает selectedProfile / lastVersionId (форматы разных лаунчеров).
    Возвращает str или None. Надёжнее st_mtime папок versions/, который
    меняется от любых операций с файлами.
    """
    try:
        pf = Path(mc_dir) / "launcher_profiles.json"
        if not pf.is_file():
            return None
        data = json.loads(pf.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as e:
        debug.dbg(f"_last_played_version: {e}")
        return None
    if not isinstance(data, dict):
        return None
    return _profile_version(data, data.get("selectedProfile") or data.get("lastVersionId"))


def installed_mc_versions(mc_dir):
    """Версии из <mc>/versions; последняя игранная — первая, далее свежие сверху."""
    versions = []
    try:
        vdir = Path(mc_dir) / "versions"
        if not vdir.is_dir():
            return versions
        for v in vdir.iterdir():
            if v.is_dir() and ((v / "version.json").exists() or (v / f"{v.name}.json").exists()):
                try:
                    versions.append((v.stat().st_mtime, v.name))
                except OSError:
                    versions.append((0, v.name))
    except OSError as e:
        debug.dbg(f"installed_mc_versions: {e}")
    versions.sort(reverse=True)
    names = [name for _, name in versions]
    # st_mtime ненадёжен (меняется от любых операций с файлами) —
    # реально последнюю игранную версию ставим на первое место.
    last = _last_played_version(mc_dir)
    if last and last in names:
        names.remove(last)
        names.insert(0, last)
    return names


def pick_target_mc():
    """Куда ставить шейдеры: сначала запущенная игра, потом самая свежая .minecraft."""
    run = detect_running_mc_version()
    if run["running"]:
        info("Обнаружен запущенный Minecraft.")
        if run["version"]:
            info(f"Версия игры: {CYAN}{run['version']}{RESET}")
        if run["dir"] is not None:
            return run["dir"], run.get("version")
    dirs = find_minecraft_dirs(limit=12)
    if not dirs:
        return None, run.get("version")
    # самая «свежая» по времени модификации
    def key(d):
        try:
            return d.stat().st_mtime
        except OSError:
            return 0
    best = max(dirs, key=key)
    vers = installed_mc_versions(best)
    if run.get("version"):
        return best, run["version"]
    if run["running"]:
        # Игра запущена, но версия не определилась из аргументов запуска —
        # берём последнюю игранную (launcher_profiles.json), а не mtime.
        last = _last_played_version(best)
        if last and (not vers or last in vers):
            return best, last
    return best, (vers[0] if vers else None)


# ─────────────────────────────────────────────────────────────────────
#  Каталог шейдеров: Modrinth API + офлайн
# ─────────────────────────────────────────────────────────────────────

def fetch_modrinth_shaders(query="shader"):
    """Список [{name, downloads, desc, files:[urls]}] или [] при ошибке сети."""
    url = MODRINTH_SEARCH.format(query=query)
    try:
        with _http_get(url, timeout=15,
                       headers={"User-Agent": "wine-installer/2.6"}) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        debug.dbg(f"modrinth search: {e}")
        return []
    packs = []
    for hit in data.get("hits", [])[:8]:
        files = hit.get("files") or []
        urls = [f.get("url") for f in files if f.get("url")]
        if not urls:
            # у search-результата файлов может не быть — берём список версий проекта
            pid = hit.get("project_id") or hit.get("slug")
            try:
                # ВАЖНО: без параметра loaders. «loaders=[]» Modrinth трактует
                # как фильтр «только версии с пустым списком загрузчиков» и
                # отдаёт [] — из-за этого раньше весь онлайн-каталог был пуст
                # и всегда включался офлайн-фолбэк.
                with _http_get(
                        f"https://api.modrinth.com/v2/project/{pid}/version",
                        timeout=15,
                        headers={"User-Agent": "wine-installer/2.6"}) as r2:
                    vlist = json.loads(r2.read().decode("utf-8", errors="replace"))
                for v in vlist[:1]:
                    urls = [f.get("url") for f in v.get("files", []) if f.get("url")]
            except Exception as e:
                debug.dbg(f"modrinth version: {e}")
        if not urls:
            continue
        packs.append({
            "name": hit.get("title") or hit.get("slug", "shader"),
            "downloads": int(hit.get("downloads", 0)),
            "desc": (hit.get("description") or "")[:70],
            "urls": urls,
        })
    return packs


def build_shader_catalog():
    """Онлайн (Modrinth) → при неудаче офлайн-каталог. [(name, meta), ...]."""
    packs = fetch_modrinth_shaders()
    if packs:
        info(f"Modrinth: найдено {len(packs)} шейдер-паков.")
        return [(p["name"], p) for p in packs]
    warn("Modrinth API недоступен — включаю офлайн-каталог.")
    return [(p["name"], {"name": p["name"], "downloads": 0, "desc": p["desc"],
                         "urls": [p["url"]] + p["alt_urls"]})
            for p in OFFLINE_SHADERS]


# ─────────────────────────────────────────────────────────────────────
#  Валидация и установка zip
# ─────────────────────────────────────────────────────────────────────

def validate_shader_zip(path):
    """Проверяет что zip открывается и содержит файлы шейдеров (.fsh/.vsh/glsl)
    либо структуру папки пакета. True/False."""
    p = Path(path)
    if not p.is_file() or p.stat().st_size < 1024:
        return False
    if not zipfile.is_zipfile(p):
        debug.dbg(f"validate: {p} не zip")
        return False
    try:
        with zipfile.ZipFile(p) as z:
            names = [n.lower() for n in z.namelist()]
            if not names:
                return False
            has_shader_files = any(n.endswith((".fsh", ".vsh", ".glsl", ".gsh"))
                                   for n in names)
            # Resource-пак: pack.mcmeta + assets/, без единого шейдерного
            # файла — обычный resource/texture-пак, а не шейдер-пак.
            is_resource_pack = ("pack.mcmeta" in names and not has_shader_files and
                                any(n.startswith("assets/") for n in names))
            # Скобки обязательны: без них из-за приоритета and/or условие
            # истинно для любого .zip с подстрокой "shader" в имени —
            # обычные resource-паки проходили как шейдер-паки.
            has_pack_meta = any(("shader" in n and n.endswith(".zip")) or
                                n.endswith("shaders/") or "/shaders/" in n
                                for n in names)
            # Пак с папками/files «shaders», но без реальных шейдерных
            # файлов (.fsh/.vsh/.glsl/.gsh), отсекается.
            return (has_shader_files or has_pack_meta) and not is_resource_pack
    except (zipfile.BadZipFile, OSError) as e:
        debug.dbg(f"validate: {e}")
        return False


def install_shader_zip(zip_path, shaderpacks_dir):
    """Кладёт проверенный zip в shaderpacks/. Возвращает путь к файлу или None."""
    zip_path = Path(zip_path)
    if not validate_shader_zip(zip_path):
        err("Файл не похож на шейдер-пак (битый zip или не шейдеры).")
        return None
    try:
        shaderpacks_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        err(f"Не могу создать {shaderpacks_dir}: {e}")
        return None
    dest = shaderpacks_dir / zip_path.name
    try:
        shutil.copy2(zip_path, dest)
    except OSError as e:
        err(f"Копирование не удалось: {e}")
        return None
    return dest


def _download_to_temp(url, fname):
    """Скачивает файл по URL в WINE_DIR/shaders. Возвращает Path или None.

    Огрызки неполных закачек никогда не остаются в кэше: проверка размера
    выполняется внутри блока try (ошибка st_size < min_bytes раньше возникала
    ДО try/finally и оставляла битый файл в WINE_DIR/shaders навсегда), а
    удаление — в finally.
    """
    tmp = WINE_DIR / "shaders"
    tmp.mkdir(parents=True, exist_ok=True)
    if not fname.lower().endswith((".zip", ".7z", ".jar")):
        fname += ".zip"
    dest_zip = tmp / fname
    # Leftover от предыдущей (возможно, частичной) загрузки с тем же
    # именем может устроить конфликт/пропуск — удаляем перед новой загрузкой.
    if dest_zip.exists():
        try:
            dest_zip.unlink()
        except OSError as e:
            debug.dbg(f"_download_to_temp cleanup: {e}")
    mirrors = [("direct", url)]
    gh = make_github_mirrors(url)
    if gh:
        mirrors += gh
    good = False
    try:
        if download_file(mirrors, dest_zip, fname, min_size_mb=0, silent=False):
            # Валидация — ВНУТРИ try: любое исключение (в т.ч. при проверке
            # размера огрызка) не должно «прыгать» мимо finally.
            min_bytes = 1024
            size = dest_zip.stat().st_size          # FileNotFoundError -> raise
            if size < min_bytes:                    # оборванный огрызок
                debug.dbg(f"_download_to_temp: {fname} слишком мал ({size} Б)")
            elif not _looks_like_archive(dest_zip):
                debug.dbg(f"_download_to_temp: {fname} не архив (HTML?)")
            else:
                good = True
    finally:
        if not good:
            # Неудача/битый огрызок — не оставляем мусор в кэше.
            try:
                dest_zip.unlink(missing_ok=True)
            except OSError:
                pass
    return dest_zip if good else None


def _looks_like_archive(path):
    """Архив ли скачанный файл (zip/jar по сигнатуре PK, 7z по магическим
    байтам)? Против HTML-заглушек прокси; содержимое проверяется дальше
    в validate_shader_zip."""
    p = Path(path)
    try:
        with open(p, "rb") as f:
            head = f.read(6)
    except OSError:
        return False
    if head.startswith(b"PK"):          # .zip / .jar
        return True
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):   # .7z
        return True
    return False


def _install_from_url(name, url, shaderpacks_dir):
    """Скачивает zip по ссылке и устанавливает в shaderpacks/. True/False."""
    info(f"Скачиваю '{name}'...")
    fname = url.split("/")[-1].split("?")[0] or f"{name}.zip"
    dest_zip = _download_to_temp(url, fname)
    if dest_zip is None:
        err("Скачать не удалось.")
        return False
    try:
        installed = install_shader_zip(dest_zip, shaderpacks_dir)
    finally:
        # Временный архив больше не нужен (ни в случае успеха, ни при
        # невалидном файле) — убираем, чтобы не копился в кэше.
        try:
            dest_zip.unlink(missing_ok=True)
        except OSError:
            pass
    if installed is None:
        hint("Файл удалён как невалидный. Попробуй другой пак.")
        return False
    ok(f"Шейдер установлен: {installed}")
    return True


# ─────────────────────────────────────────────────────────────────────
#  Список установленных шейдер-паков (для UI)
# ─────────────────────────────────────────────────────────────────────

SHADER_ARCHIVE_EXTS = (".zip", ".7z", ".jar")


def get_shader_list(mc_dir=None):
    """Шейдер-паки, реально установленные в игре: имена файлов вида
    <gameDir>/shaderpacks/*.{zip,7z,jar} БЕЗ расширений.

    Раньше сюда попадали первые записи офлайн-каталога Complementary-ссылок
    (названия файлов latest.json вроде 'ComplementaryReimagined_r5.4.zip') —
    пользователь видел в UI «мусор», даже когда ни один пак не установлен.

    Если папок .minecraft нет вовсе — возвращаем [] (неизвестно, куда ставить).
    Если shaderpacks существует, но пуст — отдаём известный офлайн-каталог
    как фолбэк («что можно установить»), помечая его star=True.
    """
    dirs = [Path(mc_dir)] if mc_dir else find_minecraft_dirs(limit=12)
    if not dirs:
        return []
    names = []
    seen_sp = False
    for d in dirs:
        sp = Path(d) / "shaderpacks"
        if not sp.is_dir():
            continue
        seen_sp = True
        try:
            entries = sorted(sp.iterdir(), key=lambda p: p.name.lower())
        except OSError as e:
            debug.dbg(f"get_shader_list: {e}")
            continue
        for f in entries:
            if not f.is_file():
                continue
            if f.suffix.lower() not in SHADER_ARCHIVE_EXTS:
                continue
            name = f.name[:-len(f.suffix)]
            if name and name not in names:
                names.append(name)
    if names:
        return [{"name": n, "star": False} for n in names]
    if seen_sp:
        # Ничего не установлено — фолбэк: известный офлайн-каталог.
        return [{"name": p["name"], "star": True} for p in OFFLINE_SHADERS]
    return []


# ─────────────────────────────────────────────────────────────────────
#  Установка мода (.jar/.zip) в mods/ правильной версии Minecraft
# ─────────────────────────────────────────────────────────────────────

def install_mod_from_zip(zip_path, mc_dir=None):
    """Кладёт мод (.jar/.zip) в <gameDir>/mods выбранной игры.

    Цель выбирается через pick_target_mc(): запущенная игра / последняя
    игранная версия (launcher_profiles.json). Раньше использовался
    installed_mc_versions()[0] — первая папка по mtime, а mtime меняется от
    любых файловых операций (включая саму установку), из-за чего мод улетал
    не в ту версию Minecraft (тот же класс бага, что в shaders.py).
    Возвращает путь к установленному файлу или None.
    """
    src = Path(zip_path)
    if not src.is_file():
        err(f"Файл не найден: {src}")
        return None
    if src.suffix.lower() not in (".jar", ".zip"):
        err("Мод должен быть .jar или .zip")
        return None
    if mc_dir is None:
        mc_dir, _ver = pick_target_mc()
    if mc_dir is None:
        err("Папка .minecraft не найдена — сначала установи игру.")
        return None
    mods_dir = Path(mc_dir) / "mods"
    try:
        mods_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        err(f"Не могу создать {mods_dir}: {e}")
        return None
    dest = mods_dir / src.name
    try:
        shutil.copy2(src, dest)
    except OSError as e:
        err(f"Копирование не удалось: {e}")
        return None
    ok(f"Мод установлен: {dest}")
    return dest



# ═══════════════════════════════════════════════════════════════
#  РЕКОМЕНДАЦИИ ПО ЖЕЛЕЗУ (sysinfo → выбор шейдера)
# ═══════════════════════════════════════════════════════════════

# Оценочная «тяжесть» паков 0..10 (по данным Modrinth/сообществу).
HEAVINESS = {
    "yofps": 1.0, "optilous": 1.5, "sapphires": 2.0, "complementary": 3.0,
    "rethinking visuals": 2.5, "chocapic13 v9 lite": 2.0, "chocapic13": 4.0,
    "bsl": 5.0, "sildur": 6.0, "roblo": 5.5, "nostalgia": 4.5,
    "seus ptgi": 8.5, "seus": 8.0, "continuum": 9.5, "kappa": 9.0,
    "projectwowo": 7.5, "interstellar": 8.0, "vdworld": 7.0,
    "mizuno": 6.5, "rocveti": 6.0, "unishader": 6.5, "makeup": 7.0,
}


def _heaviness_of(name: str, meta: dict = None) -> float:
    """Нагрузка пака 0..10: явная из API-описания или по таблице."""
    meta = meta or {}
    h = meta.get("heaviness")
    if isinstance(h, (int, float)) and 0 <= h <= 10:
        return float(h)
    low = name.lower()
    for key, val in HEAVINESS.items():
        if key in low:
            # Sildur's имеет варианты Lite/Medium/Extreme — уточняем по имени
            if key == "sildur":
                if "lite" in low: return 3.0
                if "medium" in low: return 6.0
                if "high" in low or "extreme" in low: return 8.5
            if key == "chocapic13" and "v9 lite" in low:
                return 2.0
            return val
    desc = (meta.get("desc") or "").lower()
    if "лёгк" in desc or "слабы" in desc or "low " in desc:
        return 2.0
    if "тяжёл" in desc or "potato" in desc:
        return 2.5
    if "максимальн" in desc or "extreme" in desc:
        return 8.5
    return 5.0  # неизвестный пак считаем средним


# Каталог офлайн-рекомендаций: от самых простых к самым тяжёлым.
# tier — верхняя граница мощности ПК (см. sysinfo.hardware_summary.power).
RECOMMENDATIONS = [
    {"name": "YoFPS (+Chocapic13 Edit)", "tier_max": 2.5, "heaviness": 1.0,
     "desc": "максимально лёгкий: FPS почти не режет, идёт на Intel HD/офисных ПК",
     "url": "https://cdn.modrinth.com/data/xM96NJmk/versions/hOdwb79n/"
            "YoFPS%2BHigh%2B%28Chocapic13%2BEdit%29.zip"},
    {"name": "Sildur's Vibrant Shaders Lite", "tier_max": 3.5, "heaviness": 3.0,
     "desc": "самый популярный лёгкий пак с нормальным качеством картинки",
     "url": "https://cdn.modrinth.com/data/z8EjLYqN/versions/cianYi38/"
            "Sildur%27s%20Vibrant%20Shaders%20v2.02%20Lite.zip",
     "alt_urls": ["https://cdn.modrinth.com/data/z8EjLYqN/versions/EisvxIzu/"
                  "Sildur%27s%20Vibrant%20Shaders%20v2.02%20Medium.zip"]},
    {"name": "ComplementaryReimagined", "tier_max": 5.0, "heaviness": 3.0,
     "desc": "универсальный: отлично выглядит, оптимизирован, крутится в настройках",
     "url": "https://github.com/ComplementaryDevelopment/"
            "ComplementaryReimagined/releases/download/latest/"
            "ComplementaryReimagined_r5.4.zip",
     "alt_urls": ["https://cdn.modrinth.com/data/HVnmMxH1/versions/Bqen1mJX/"
                  "ComplementaryReimagined_r5.9.3.zip"]},
    {"name": "BSL Shaders", "tier_max": 6.5, "heaviness": 5.0,
     "desc": "мягкое реалистичное освещение, средняя нагрузка (GTX 1060/RX 580+)",
     "url": "https://cdn.modrinth.com/data/Q1vvjJYV/versions/Y68yiql9/"
            "BSL_v10.1.8.zip"},
    {"name": "Sildur's Vibrant Shaders Extreme", "tier_max": 8.0,
     "heaviness": 8.5,
     "desc": "яркий хардкорный пак для мощных GPU (RTX 3060+ / RX 6700+)",
     "url": "https://cdn.modrinth.com/data/z8EjLYqN/versions/J4RlzpKF/"
            "Sildur%27s%20Vibrant%20Shaders%20v2.02%20Extreme.zip"},
    {"name": "SEUS PTGI EDD", "tier_max": 99.0, "heaviness": 9.0,
     "desc": "трассировка лучей на Райзен-уровне железа: RTX 3070+/4060+ и 16 ГБ ОЗУ",
     "url": "https://cdn.modrinth.com/data/btQHy6aC/versions/"
            "S9fPfh3j/seus-ptgi-limited-edition-1.0.1.zip"},
]


def recommend_shader(hw: dict):
    """Лучший пак под железо: самый тяжёлый, который ПК «потянет».

    Берём последний элемент RECOMMENDATIONS с heaviness <= power+1
    (запас +1 чтобы картинка была интереснее, но не убивала FPS).
    """
    if not hw:
        return None
    power = hw.get("power")
    if power is None:
        return None
    candidates = [r for r in RECOMMENDATIONS if r["heaviness"] <= power + 1.0]
    if not candidates:
        return RECOMMENDATIONS[0]
    best = max(candidates, key=lambda r: r["heaviness"])
    out = dict(best)
    out.setdefault("alt_urls", [])
    return out


def cmd_shaders(arg=""):
    """Команда shaders / шейдеры — интерактивная установка шейдер-пака.

    Аргументы:
      shaders            — автоподбор по железу ПК + каталог от простых к
                           тяжёлым (Modrinth + офлайн-список);
      shaders auto       — только автоподбор и установка лучшего варианта;
      shaders <URL>      — установить пак по прямой ссылке (.zip).
    """
    arg = (arg or "").strip()
    print(f"\n{BOLD}═══ УСТАНОВКА ШЕЙДЕРОВ ═══{RESET}")

    # 1. Смотрим комплектующие ПК и предлагаем лучший шейдер
    try:
        from modules.sysinfo import hardware_summary, format_summary
        hw = hardware_summary()
        info(f"{BOLD}Твоё железо:{RESET}")
        for line in format_summary(hw).splitlines():
            print(f"    {line}")
    except Exception as e:
        debug.dbg_exc(e, "shaders/sysinfo")
        hw = None
    rec = recommend_shader(hw) if hw else None
    if rec:
        ok(f"Рекомендация под твоё ПК: {CYAN}{rec['name']}{RESET} "
           f"(класс «{rec['tier']}», нагрузка ~{rec['heaviness']}/10)")
        hint(rec["desc"])

    mc, version = pick_target_mc()
    if mc is None:
        err("Папка .minecraft не найдена.")
        hint("Сначала установи Minecraft (legacy / fo) или запусти игру один раз.")
        return True
    vers = installed_mc_versions(mc)
    info(f"Игра: {CYAN}{mc}{RESET}")
    if version:
        info(f"Целевая версия: {CYAN}{version}{RESET}")
    elif vers:
        info(f"Установленные версии: {CYAN}{', '.join(vers[:5])}{RESET}")
    shaderpacks = mc / "shaderpacks"

    # Прямая ссылка: shaders https://.../MyShaders.zip
    if arg.lower().startswith(("http://", "https://")):
        _install_from_url(Path(arg.split("?")[0].split("/")[-1]).stem or "shader",
                          arg, shaderpacks)
        hint("В игре: Настройки → Графика → Шейдеры → выбрать пакет → Применить.")
        return True

    packs = build_shader_catalog()
    # дополняем каталог рекомендованным паком, если его там нет
    if rec and not any(p[0].lower().startswith(rec["name"].lower()[:12])
                       for p in packs):
        packs.insert(0, (rec["name"], {"urls": [rec["url"]],
                                       "desc": rec["desc"]}))
    if not packs:
        err("Каталог пуст (ни сеть, ни офлайн-список не помогли).")
        return True

    # 2. Авто-режим: ставим рекомендацию без вопросов
    if arg.lower() in ("auto", "авто", "a"):
        if not rec:
            warn("Не удалось оценить железо — выбирай из списка вручную")
        else:
            for url in ([rec["url"]] + list(rec.get("alt_urls", []))):
                if _install_from_url(rec["name"], url, shaderpacks):
                    break
            hint("В игре: Настройки → Графика → Шейдеры → применить пакет.")
            return True

    # 3. Каталог, отсортированный ОТ ПРОСТЫХ К ТЯЖЁЛЫМ
    ranked = sorted(packs, key=lambda p: (_heaviness_of(p[0], p[1]),
                                          -(p[1].get("downloads") or 0)))
    print(f"\n{BOLD}Шейдер-паки (сверху — самые лёгкие, снизу — тяжёлые):{RESET}")
    for i, (name, meta) in enumerate(ranked, 1):
        h = _heaviness_of(name, meta)
        dl = (f"  {meta['downloads']:,} загрузок".replace(",", " ")
              if meta.get("downloads") else "")
        stars = "▇" * max(1, int(h))
        mark = ""
        if rec and name.lower().startswith(rec["name"].lower()[:12]):
            mark = f"  {GREEN}★ рекомендую{RESET}"
        print(f"  {CYAN}{i:>2}{RESET}) [{stars:<6}] {YELLOW}{h:.0f}/10{RESET} "
              f"{name}{dl}{mark}")
        if meta.get("desc"):
            print(f"        {meta['desc']}")
    print(f"  {CYAN}0{RESET}) Отмена")
    try:
        choice = input(f"{YELLOW}Номер пака (Enter — установить "
                       f"рекомендованный): {RESET}").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return True
    if choice == "" and rec:
        target = next(((n, m) for n, m in ranked
                       if n.lower().startswith(rec["name"].lower()[:12])),
                      (rec["name"], {"urls": [rec["url"]]}))
    elif choice.isdigit() and 1 <= int(choice) <= len(ranked):
        target = ranked[int(choice) - 1]
    else:
        warn("Отменено.")
        return True
    name, meta = target
    urls = meta.get("urls") or ([rec["url"]] if rec and
                                name.startswith(rec["name"]) else [])
    if not urls:
        err("Для этого пака нет ссылки.")
        return True
    ok_installed = False
    for url in urls:
        if _install_from_url(name, url, shaderpacks):
            ok_installed = True
            break
    if not ok_installed:
        return True
    hint("В игре: Настройки → Графика → Шейдеры → выбрать пакет → Применить.")
    hint("Нужен OptiFine или Iris (Fabulously Optimized уже содержит Iris).")
    return True
