"""Prism, Legacy, Fabulously Optimized, Optimized fps (Forge+Luxium), OptiFine, Fabric."""
import os
import re
import json
import shutil
import zipfile
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from modules import debug
from modules.colors import ok, info, warn, err, hint, CYAN, YELLOW, MAGENTA, BOLD, RESET
from modules.config import (
    WINE_DIR, PRISM_DIR, PRISM_URL, WINE_PREFIX,
    FO_GITHUB_API, OPTIFINE_PAGE, FABRIC_META, FABRIC_MAVEN,
    MOJANG_VERSION_MANIFEST, make_github_mirrors,
    LEGACY_JAR_MIN_SIZE, legacy_jar_mirrors,
    SYSTEM_TRUSTSTORE_PATHS, SYSTEM_TRUSTSTORE_PASSWORD,
    FABRIC_META_API, MAVEN_FABRIC, MAVEN_CENTRAL,
    OFPS_MODRINTH_API, LUXIUM_MC_SIDE, CF_EDGE_BASES, REPO_ROOT,
    EMBEDDIUM_VERSIONS_API, EMBEDDIUM_MC_VERSION, EMBEDDIUM_SHA1_PIN,
    embeddium_version_range, modrinth_file_mirrors, _ver_tuple,
    FO_1201_PIN_TAG, FO_1201_MC_VERSION, FO_1201_MRPACK_URL,
    FO_OFFICIAL_TAG, FO_OFFICIAL_MRPACK_URL, FO_MRPACK_OWNER, FO_MRPACK_REPO,
    find_local_mrpack, fo_mrpack_mirrors,
    SODIUM_VERSIONS_API, SODIUM_PINNED_VERSION, SODIUM_MC_VERSION,
    luxium_supports_fabric, luxium_fabric_dep,

    LEGACY_MANUAL_PAGE, LEGACY_MANUAL_JAR_URL,)
from modules import net
from modules.download import download_file, head_check
from modules.hash_utils import verify_sha512, verify_sha1, copy_to_clipboard
from modules.java import find_java, install_portable_java, cmd_install_java
from modules.prefix import choose_minecraft_dir


# Единый SSL-контекст проекта — в modules/net.py (раньше копировался 6 раз).


def _http_get(url, timeout=30, headers=None):
    """GET с фолбеком строгой проверки сертификатов (см. net.ssl_ctx)."""
    from modules.download import open_checked
    req = urllib.request.Request(url, headers=headers or {})
    return open_checked(req, timeout)


def _http_get_json(url, timeout=30):
    """GET url -> распарсенный JSON (SSL-контекст как в остальном проекте)."""
    with _http_get(url, timeout=timeout,
                   headers={"User-Agent": "wine-installer/2.5"}) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _github_get_json(url, timeout=30):
    """JSON c api.github.com + кэш по ETag (лимит API 60 запросов/час без токена).

    Школа: несколько установок подряд могли упереться в лимит и получить
    403 — теперь свежие ответы переиспользуются из кэша WINE_DIR.
    """
    from modules.config import CACHE_FILE
    cache_path = Path(str(CACHE_FILE) + ".github.json")
    cache = {}
    try:
        if cache_path.exists():
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    entry = cache.get(url, {})
    headers = {"User-Agent": "wine-installer/2.5", "Accept": "application/vnd.github+json"}
    if entry.get("etag"):
        headers["If-None-Match"] = entry["etag"]
    try:
        with _http_get(url, timeout=timeout, headers=headers) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        etag = r.headers.get("ETag", "")
        if etag:
            cache[url] = {"etag": etag, "data": data}
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cache), encoding="utf-8")
            except Exception as e:
                debug.dbg_exc(e, "minecraft")
        return data
    except urllib.error.HTTPError as e:
        if e.code == 304 and "data" in entry:      # не изменилось — отдаём кэш
            debug.dbg(f"github cache 304: {url}")
            return entry["data"]
        if e.code == 403 and "data" in entry:      # лимит API — спасаемся кэшем
            warn("GitHub API лимит исчерпан — использую кэшированный список релизов")
            return entry["data"]
        raise
    except Exception:
        if "data" in entry:                        # сеть лежит — берём кэш
            warn("GitHub API недоступен — использую кэш")
            return entry["data"]
        raise


def _sha1(path):
    ok_hash, actual = verify_sha1(path, "")
    return actual or ""


# ═══════════════════════════════════════════════════════════════════
#  АВТОФИКС — единая точка восстановления после сбоев
#  (битый кэш, обрыв сети, полураспакованные файлы).
# ═══════════════════════════════════════════════════════════════════

def _autofix_remove(path):
    """Удаляет файл/папку; при PermissionError ретраит и предлагает sudo."""
    p = Path(path)
    if not p.exists():
        return True
    for _ in range(3):
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            debug.dbg(f"autofix: удалено {p}")
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            info(f"Нет прав на удаление {p} — пытаемся через sudo…")
            try:
                subprocess.run(["sudo", "-n", "rm", "-rf", str(p)], check=False, timeout=60)
                if not p.exists():
                    ok("Удалено через sudo")
                    return True
            except Exception as e:
                debug.dbg_exc(e, "autofix/sudo rm")
            err(f"Не удалить {p}")
            hint(f"Удали вручную:  rm -rf '{p}'")
            return False
        except Exception as e:
            debug.dbg_exc(e, f"autofix/remove {p}")
    return not p.exists()


def _autofix_clean_file(path, min_bytes=0):
    """Если файл битый/нулевой/недокачанный — удаляем (чтобы скачался заново)."""
    p = Path(path)
    try:
        if p.exists() and p.stat().st_size < max(min_bytes, 1):
            warn(f"Битый/пустой файл в кэше: {p.name} — автофикс: удаляю и скачаю заново")
            _autofix_remove(p)
            return True
    except Exception as e:
        debug.dbg_exc(e, "autofix/clean_file")
    return False


def _autofix_is_html_jar(path):
    """JAR, который на деле HTML-заглушка (частый случай у optifine.net/GitHub-прокси)."""
    p = Path(path)
    try:
        if not p.exists():
            return False
        with open(p, "rb") as f:
            head = f.read(16)
        return head.startswith(b"<") or head.startswith(b"<!D") or head.startswith(b"\xef\xbb\xbf<")
    except Exception:
        return False


def _ask_retry(label):
    """Вежливый вопрос «повторить?» — Enter/y/д = да. Возвращает bool."""
    try:
        ans = input(f"{YELLOW}Автофикс: повторить «{label}»? [Y/n]: {RESET}").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return False
    return ans in ("", "y", "yes", "д", "да")


# ═══════════════════════════════════════════════════════════════════
#  JAVA TRUSTSTORE — русские CA (Минцифры) не в cacerts OpenJDK
# ═══════════════════════════════════════════════════════════════════

def _find_system_truststore():
    """Ищет системный Java-truststore с русскими CA."""
    for path_str in SYSTEM_TRUSTSTORE_PATHS:
        p = Path(path_str)
        try:
            if p.exists() and p.stat().st_size > 10_000:
                return str(p)
        except Exception:
            continue
    return None


def _java_env_with_truststore():
    """Копирует env и добавляет JAVA_TOOL_OPTIONS с системным truststore.

    JAVA_TOOL_OPTIONS подхватывается ВСЕМИ JVM, включая второй процесс,
    который Legacy Launcher запускает сам через BootstrapRestarter.
    Просто добавить -D... в командную строку недостаточно — внутренний
    процесс его не унаследует.

    Также задаём trustStorePassword: системный cacerts, который
    генерирует update-ca-trust, защищён паролем 'changeit'.
    """
    env = os.environ.copy()
    truststore = _find_system_truststore()
    if truststore:
        opts = [
            f"-Djavax.net.ssl.trustStore={truststore}",
            f"-Djavax.net.ssl.trustStorePassword={SYSTEM_TRUSTSTORE_PASSWORD}",
            "-Djavax.net.ssl.trustStoreType=JKS",
        ]
        existing = env.get("JAVA_TOOL_OPTIONS", "").strip()
        new_opts = " ".join(opts)
        if "javax.net.ssl.trustStore" not in existing:
            env["JAVA_TOOL_OPTIONS"] = (existing + " " + new_opts).strip()
        debug.dbg(f"JAVA_TOOL_OPTIONS={env['JAVA_TOOL_OPTIONS']}")
    else:
        warn("Системный Java-truststore не найден — SSL может падать")
        hint("Проверь /etc/pki/ca-trust/extracted/java/cacerts")
        hint("Если файла нет: sudo update-ca-trust")
    return env


# ═══════════════════════════════════════════════════════════════════
#  PRISM LAUNCHER
# ═══════════════════════════════════════════════════════════════════

def _is_executable_candidate(name):
    """Имена исполняемых файлов portable-сборки Prism (без расширения)."""
    return name in ("PrismLauncher", "AppRun") or \
        re.fullmatch(r"PrismLauncher(\.linux)?(-x86_64)?", name) is not None


def _find_prism_exe():
    """Ищет НАСТОЯЩИЙ бинарник лаунчера среди файлов (не папок!).

    Раньше условие `f.startswith("PrismLauncher")` матчило и директории
    вида `PrismLauncherIcons/` — chmod применялся к папке, а пользователю
    показывался путь до неё вместо исполняемого файла, и «открыть»
    такой «лаунчер» было невозможно. Теперь берём только файлы и только
    точные имена бинарников; если файлов-кандидатов несколько — самый
    крупный ELF (реальный бинарник больше любых заглушек/скриптов).

    На Windows portable-сборка — это prismlauncher.exe (+ Qt6*.dll и
    icu*.dll рядом), имя без расширения не матчится, поэтому там ищем
    именно exe.
    """
    from modules.platform_utils import IS_WINDOWS
    candidates = []
    for root, dirs, files in os.walk(PRISM_DIR):
        for f in files:
            p = Path(root) / f
            if IS_WINDOWS:
                if f.lower() != "prismlauncher.exe" or not p.is_file():
                    continue
                candidates.append((p.stat().st_size, p))
                continue
            if not _is_executable_candidate(f):
                continue
            try:
                if not p.is_file():
                    continue
                size = p.stat().st_size
            except OSError:
                continue
            candidates.append((size, p))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def _prism_has_icu(exe):
    """Проверка для Windows: рядом с prismlauncher.exe лежат ICU-DLL?

    Классическая ошибка «система не обнаружила icu.DLL» возникает, когда
    запускают prismlauncher.exe из Setup-дистрибутива или копируют exe без
    DLL-ов. Имена ICU зависят от сборки Qt:
      MSVC-Portable  -> icu.dll / icuuc.dll / icudt*.dll;
      MinGW-w64      -> libicuuc78.dll / libicuin78.dll / libicudt78.dll.
    Поэтому ищем ЛЮБОЙ файл вида *icu*.dll — раньше проверка смотрела только
    на startswith("icu") и рабочую MinGW-сборку считала битой (отсюда
    бесконечные перекачивания и «icu.DLL остаётся»).
    """
    try:
        d = Path(exe).parent
        return any("icu" in f.lower() and f.lower().endswith(".dll")
                   for f in os.listdir(d))
    except OSError:
        return False


def _prism_bad_installs():
    """Находит старые SETUP-установки Prism (Program Files / AppData / реестр).

    Именно они дают ошибку «не обнаружена icu.DLL»: Qt MinGW-сборка
    Setup-инсталлятора не кладёт icu*.dll рядом с exe. Возвращает список
    путей-папок, которые стоит удалить перед запуском portable-версии.
    """
    bad = []
    envs = [os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles (x86)".replace(" ", "")),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA")]
    for e in envs:
        if not e:
            continue
        for name in ("Prism Launcher", "PrismLauncher"):
            p = Path(e) / name
            if p.is_dir() and (p / "prismlauncher.exe").is_file():
                bad.append(p)
    # ярлык из меню «Пуск» тоже укажет на Setup-версию — ищем цель ссылки
    try:
        import glob
        start = os.path.join(os.environ.get("ProgramData", ""),
                             "Microsoft", "Windows", "Start Menu")
        for lnk in glob.glob(os.path.join(start, "**", "*Prism*.lnk"),
                             recursive=True):
            try:
                with open(lnk, "rb") as f:
                    data = f.read()
                # путь цели лежит в данных .lnk как utf-16le; грубо ищем
                txt = data.decode("utf-16-le", errors="ignore")
                for part in txt.replace("\\", "/").split("/"):
                    pass
                m = re.search(r"[A-Za-z]:\\\\?[^\x00]*prismlauncher\.exe",
                              data.decode("latin-1", errors="ignore"),
                              re.IGNORECASE)
                if m:
                    tgt = Path(m.group(0).replace("\\\\", "\\"))
                    if tgt.parent.is_dir():
                        bad.append(tgt.parent)
            except OSError:
                debug.dbg(f"prism lnk parse fail: {lnk}")
    except Exception as e:
        debug.dbg_exc(e, "_prism_bad_installs/lnk")
    uniq, seen = [], set()
    for b in bad:
        key = str(b).lower().rstrip("\\/")
        if key not in seen:
            seen.add(key)
            uniq.append(b)
    return uniq


def _uninstall_old_prism_windows(paths):
    """Тихо удаляем старые Setup-установки Prism, чтобы Windows больше не
    находил битый prismlauncher.exe (тот, что без icu*.dll)."""
    import shutil
    for p in paths:
        try:
            warn(f"Удаляю старую Setup-версию Prism: {p}")
            shutil.rmtree(p)
            ok(f"Удалено: {p}")
        except OSError as e:
            debug.dbg_exc(e, "_uninstall_old_prism_windows")
            hint(f"Не удалось удалить ({e}). Удали вручную через "
                 f"«Параметры → Приложения» или просто НЕ запускай тот "
                 f"ярлык Prism из меню Пуск.")


def _find_downloaded_prism_zip():
    """Ищет скачанный браузером Portable-архив Prism в типовых папках."""
    from modules.platform_utils import IS_WINDOWS
    if not IS_WINDOWS:
        return None
    cands = []
    home = Path.home()
    for folder in (home / "Downloads", home):
        if not folder.is_dir():
            continue
        try:
            for f in folder.iterdir():
                n = f.name.lower()
                if not f.is_file():
                    continue
                if ("prism" in n and "windows" in n and "msvc" in n
                        and "portable" in n
                        and (n.endswith(".zip") or f.suffix == "")
                        and f.stat().st_size > 10 * 1024**2):
                    cands.append(f)
        except OSError:
            continue
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0]


def _prism_zip_ok(path, min_mb=10):
    # (min_mb=0 в тестах — отключить порог размера)
    """Валидный ли это Portable-архив: сигнатура PK + внутри есть
    prismlauncher.exe И icu*.dll. Отсекает HTML-заглушки прокси и битые
    докачки по размеру."""
    try:
        if not path.is_file() or path.stat().st_size < min_mb * 1024**2:
            return False
        with open(path, "rb") as f:
            if f.read(2) != b"PK":
                return False
        import zipfile
        with zipfile.ZipFile(path) as z:
            names = [n.split("/")[-1].lower() for n in z.namelist()]
            has_exe = "prismlauncher.exe" in names
            # ВАЖНО: у MinGW-w64 Portable (основная сборка, см. config.py)
            # ICU-DLL называются libicuuc78.dll / libicuin78.dll /
            # libicudt78.dll — они НЕ начинаются с "icu". Строгая проверка
            # startswith("icu") считала валидный архив битым («Скачанный
            # файл не похож на Portable-архив... Перекачай») и запускала
            # бесконечный круг перекачиваний. Ищем ЛЮБОЙ *icu*.dll — ровно
            # как в _prism_has_icu (проверка уже распакованной папки).
            has_icu = any("icu" in n and n.endswith(".dll") for n in names)
            return has_exe and has_icu
    except Exception as e:
        debug.dbg_exc(e, "_prism_zip_ok")
        return False


def _prism_icu_broken(exe):
    """True, если рядом с prismlauncher.exe НЕТ ни одного icu*.dll.

    Именно такой запуск даёт «системная ошибка: не обнаружена icu.DLL».
    """
    return not _prism_has_icu(exe)


def _launch_prism_exe(exe):
    from modules.platform_utils import IS_WINDOWS
    if IS_WINDOWS and _prism_icu_broken(exe):
        err("Этот prismlauncher.exe запускается с ошибкой «не обнаружена "
            "icu.DLL»: рядом нет icu*.dll (Setup/MinGW-сборка или exe "
            "скопирован без DLL). Нужен Portable zip целиком.")
        hint(f"Папка лаунчера: {Path(exe).parent}")
        return False
    try:
        subprocess.Popen([str(exe)], cwd=str(Path(exe).parent))
        ok(f"Запускаю Prism: {exe}")
        return True
    except Exception as e:
        debug.dbg_exc(e, "_launch_prism_exe")
        hint(f"Запусти вручную двойным кликом: {exe}")
        return False


def _prism_search_roots():
    """Папки, где реально может лежать prismlauncher.exe.

    Кроме нашей portable-папки (PRISM_DIR) это типичные места ручной
    установки: Program Files, LOCALAPPDATA\\Programs, Downloads и сама
    mc-portable (пользователь мог распаковать архив в стороне). Игнорируем
    заведомо чужие/огромные деревья. Нужен для диагностики icu.DLL: понять,
    ЗАПУСКАЕТСЯ ли битый exe из Setup-установки или у portable просто нет dll.
    """
    roots = []
    envs = [os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA")]
    for e in envs:
        if e:
            roots.append(Path(e))
            roots.append(Path(e) / "Programs")
    dl = Path.home() / "Downloads"
    roots.append(dl)
    roots.append(WINE_DIR)
    return [r for r in roots if r and r.is_dir()]


def _find_all_prism_exes(limit=40):
    """Все prismlauncher.exe в типичных местах (для диагностики)."""
    found = []
    for base in _prism_search_roots():
        try:
            for p in base.rglob("prismlauncher.exe"):
                try:
                    if p.is_file():
                        found.append(p)
                except OSError:
                    continue
                if len(found) >= limit:
                    return found
        except Exception as e:
            debug.dbg(f"_find_all_prism_exes {base}: {e}", level="WARN")
    return found


def _diagnose_icu_error():
    """После сообщения Windows «не обнаружена icu.DLL» — найти виновника.

    1) Если где-то лежит prismlauncher.exe БЕЗ icu*.dll рядом — это он и есть
       (обычно старая Setup-версия в Program Files). Показываем путь,
       предлагаем удалить и копируем его в буфер (легко открыть папку).
    2) Если все найденные exe «здоровые», но ошибка всё равно вылезла —
       значит запускался не тот файл: подсказываем запускать exe строго из
       папки с dll.
    """
    bad, good = [], []
    for p in _find_all_prism_exes():
        (good if _prism_has_icu(p) else bad).append(p)
    if bad:
        err("Найден prismlauncher.exe БЕЗ icu*.dll — именно он падает:")
        for p in bad[:5]:
            print(f"    {YELLOW}{p}{RESET}")
        hint("Удали эту папку целиком (или перенеси portable-версию):")
        hint(f"    {PRISM_DIR}")
        try:
            if copy_to_clipboard(str(bad[0].parent)):
                ok("Путь к битой установке скопирован в буфер обмена "
                   "(вставь в проводник и удали папку)")
        except Exception as e:
            debug.dbg(f"clipboard: {e}", level="WARN")
    elif good:
        hint("Битых установок не найдено; рабочая(ие) portable-копии:")
        for p in good[:3]:
            print(f"    {MAGENTA}{p}{RESET}")
        hint("Запускай именно этот .exe двойным кликом ИЗ ЕГО ПАПКИ "
             "(DLL должны лежать рядом с ним).")
    else:
        hint("Ни одной копии Prism на диске не найдено — нужна установка "
             "(эта команда её и делает).")


def setup_prism():
    from modules.platform_utils import IS_WINDOWS
    info(f"{BOLD}Установка Prism Launcher{RESET}")

    # 0.5) Уже распакованный рабочий portable в PRISM_DIR? Предлагать запуск
    # ДО любых сетевых операций — сеть школы может блокировать скачивание,
    # а лаунчер уже лежит на диске. Битый (без icu*.dll) не запускаем и
    # честно говорим почему.
    if IS_WINDOWS:
        try:
            prev_exe = _find_prism_exe()
        except Exception as e:
            debug.dbg_exc(e, "setup_prism/prev-exe-early")
            prev_exe = None
        if prev_exe and _prism_has_icu(prev_exe):
            ok(f"Prism уже установлен (portable): {prev_exe}")
            launched = _launch_prism_exe(prev_exe)
            if not launched:
                _diagnose_icu_error()
            return True
        if prev_exe and not _prism_has_icu(prev_exe):
            warn("Распакованный Prism повреждён: рядом с prismlauncher.exe "
                 "нет icu*.dll (тот самый запуск даёт «система не обнаружила "
                 "icu.DLL»). Перекачаю Portable-архив заново.")

    if IS_WINDOWS:
        # На Windows .tar.gz с Linux-бинарником бесполезен — качаем
        # нативную portable-сборку .zip (в ней есть icu*.dll, в отличие
        # от Setup .exe / MinGW-сборок — см. PRISM_WIN_URLS) и распаковываем.
        from modules.config import (prism_win_urls, PRISM_WIN_ZIP_NAME,
                                    PRISM_WIN_ZIP_URL)

        # Точный размер архива с GitHub: прокси-зеркала на неизвестный asset
        # отдают заглушки; сверка по точному размеру отсекает мусор, который
        # проходит простую проверку «>= 10 МБ». Ошибку сети не считаем отказом
        # — тогда просто остаёмся на min_size_mb.
        prism_exact = None
        try:
            _ok_h, _sz_h = head_check(PRISM_WIN_ZIP_URL)
            if _ok_h and _sz_h and _sz_h > 5 * 1024 * 1024:
                prism_exact = _sz_h
        except Exception as e:
            debug.dbg_exc(e, "setup_prism/head")

        # 0) Старая SETUP-версия Prism (Program Files\Prism Launcher) —
        # именно она выдаёт «система не обнаружила icu.DLL». Если пользователь
        # запускает prismlauncher.exe оттуда (или по ярлыку из меню Пуск),
        # ошибка повторяется даже после успешной portable-установки. Удаляем.
        try:
            bad = _prism_bad_installs()
        except Exception as e:
            debug.dbg_exc(e, "setup_prism/bad-installs")
            bad = []
        if bad:
            _uninstall_old_prism_windows(bad)

        # 1) Уже распакованная и рабочая portable-копия? (проверяем наличие
        # icu*.dll рядом с exe — раньше этот блок молча возвращал битую
        # старую распаковку без dll)
        prev_exe = None
        try:
            prev_exe = _find_prism_exe()
        except Exception as e:
            debug.dbg_exc(e, "setup_prism/prev-exe")
        if prev_exe and _prism_has_icu(prev_exe):
            ok(f"Prism уже установлен (portable): {prev_exe}")
            hint("Запускаю...")
            try:
                subprocess.Popen([str(prev_exe)],
                                 cwd=str(Path(prev_exe).parent))
            except Exception as e:
                debug.dbg_exc(e, "setup_prism/win-run-prev")
                hint(f"Или запусти вручную двойным кликом: {prev_exe}")
            return True

        PRISM_DIR.mkdir(parents=True, exist_ok=True)
        dest = PRISM_DIR / PRISM_WIN_ZIP_NAME

        # 2) Валидный архив уже скачан? Иначе — перекачиваем (старый код
        # принимал ЛЮБОЙ файл >10 МБ, включая HTML-заглушки прокси и битые
        # докачки, что и приводило к запуску exe без dll).
        archive = dest if _prism_zip_ok(dest) else None

        # 3) Пользователь мог скачать Portable-архив браузером (сеть блокирует
        # Python) — ищем его в Downloads и используем.
        if archive is None:
            dl = _find_downloaded_prism_zip()
            if dl and _prism_zip_ok(dl):
                info(f"Найден скачанный браузером архив: {dl}")
                try:
                    shutil.copy2(dl, dest)
                    if _prism_zip_ok(dest):
                        archive = dest
                except OSError as e:
                    debug.dbg_exc(e, "setup_prism/copy-download")

        # 4) Если в prism/ уже распакованы файлы из настоящего Portable-архива
        # (prismlauncher.exe + icu*.dll), но сам zip потерян/бит — сеть больше
        # НЕ нужна: просто запускаем то, что лежит на диске. Это лечит случай
        # «ошибка icu.DLL остаётся», когда пользователь качал архив браузером
        # и распаковывал его сюда вручную.
        if archive is None:
            ex = _find_prism_exe()
            if ex and _prism_has_icu(ex):
                ok("Prism уже распакован в папке — запускаю без закачки")
                _launch_prism_exe(ex)
                return True

        if archive is None and not download_file(prism_win_urls(), dest,
                                                 "Prism Launcher (Windows)",
                                                 min_size_mb=10,
                                                 exact_size=prism_exact):
            err("Не удалось скачать Prism Launcher для Windows")
            hint("Скачай вручную через браузер: https://prismlauncher.org/downloads/")
            hint('выбери вариант "Portable (.zip)" (не "Installer"), например')
            hint(f"  {PRISM_WIN_ZIP_NAME}")
            hint("и сохрани в папку Загрузки — команда `prism` найдёт его сам,")
            hint("либо распакуй архив целиком в:")
            hint(f"  {PRISM_DIR}")
            try:
                copy_to_clipboard(str(PRISM_DIR))
            except Exception as e:
                debug.dbg_exc(e, "setup_prism/win-clip")
            import webbrowser
            from modules.config import PRISM_WIN_OFFICIAL_PAGE
            try:
                if webbrowser.open(PRISM_WIN_OFFICIAL_PAGE):
                    ok(f"Открыл в браузере: {PRISM_WIN_OFFICIAL_PAGE}")
            except Exception as e:
                debug.dbg_exc(e, "setup_prism/win-browser")
            _diagnose_icu_error()
            return False
        archive = dest
        if not _prism_zip_ok(archive):
            err("Скачанный файл не похож на Portable-архив Prism "
                "(нет prismlauncher.exe или icu*.dll внутри). Перекачай.")
            try:
                archive.unlink()
            except OSError as e:
                debug.dbg_exc(e, "setup_prism/unlink-bad")
            _diagnose_icu_error()
            return False
        try:
            net.safe_extract_zip(archive, PRISM_DIR)
        except Exception as e:
            debug.dbg_exc(e, "setup_prism/win-extract")
            err(f"Распаковка: {e}")
            return False
        exe = _find_prism_exe()
        if not exe:
            err("prismlauncher.exe не найден после распаковки")
            return False
        if not _prism_has_icu(exe):
            err("Рядом с prismlauncher.exe нет icu*.dll — это НЕ portable-"
                "сборка (тот самый запуск даёт «не обнаружена icu.DLL»).")
            hint("Нужен файл вида "
                 "PrismLauncher-Windows-MinGW-w64-Portable-*.zip "
                 "(или MSVC-Portable) — Portable, не Installer")
            return False
        ok(f"Prism: {exe}")
        hint("Запускаю...")
        try:
            subprocess.Popen([str(exe)], cwd=str(Path(exe).parent))
        except Exception as e:
            debug.dbg_exc(e, "setup_prism/win-run")
            hint(f"Или запусти вручную двойным кликом: {exe}")
        return True

    archive = WINE_DIR / "prism.tar.gz"
    # Зеркала: раньше качали ТОЛЬКО напрямую с github.com — в медленных/
    # блокирующих сетях Prism не скачивался вообще. Добавлены прокси-зеркала
    # (ghfast.top / ghproxy.net / ...), как для всех остальных загрузок.
    mirrors = [("GitHub (прямой)", PRISM_URL)] + make_github_mirrors(PRISM_URL)
    if not download_file(mirrors, archive, "Prism Launcher", min_size_mb=3):
        err("Не удалось скачать Prism")
        return False
    PRISM_DIR.mkdir(parents=True, exist_ok=True)
    try:
        net.safe_extract_tar(archive, PRISM_DIR)
    except Exception as e:
        debug.dbg_exc(e, "setup_prism/extract")
        err(f"Распаковка: {e}")
        return False
    exe = _find_prism_exe()
    if exe:
        try:
            exe.chmod(0o755)  # на случай архива без флага +x у бинарника
        except OSError as e:
            debug.dbg_exc(e, "setup_prism/chmod")
        ok(f"Prism: {exe}")
        hint(f"Запусти: {exe}")
        return True
    err("PrismLauncher не найден")
    return False


# ═══════════════════════════════════════════════════════════════════
#  LEGACY LAUNCHER
# ═══════════════════════════════════════════════════════════════════

def _find_legacy_installed():
    candidates = [
        WINE_PREFIX / "drive_c" / "users" / "stud" / "AppData" / "Local" / "Programs",
        WINE_PREFIX / "drive_c" / "users" / "stud" / "AppData" / "Roaming",
        WINE_PREFIX / "drive_c" / "Program Files",
        WINE_PREFIX / "drive_c" / "Program Files (x86)",
    ]
    for base in candidates:
        if not base.exists():
            continue
        try:
            for root, dirs, files in os.walk(base):
                for f in files:
                    if f.lower() == "legacylauncher.exe":
                        return Path(root) / f
        except Exception as e:
            debug.dbg_exc(e, "minecraft")
    return None


def _find_local_legacy_jar():
    """Ищет уже скачанный .jar в стандартных местах + в Downloads."""
    candidates = [
        WINE_DIR / "legacy-portable" / "LegacyLauncher.jar",
        WINE_DIR / "LegacyLauncher.jar",
        WINE_DIR / "legacy-portable" / "bootstrap.jar",
        WINE_DIR / "bootstrap.jar",
    ]
    # Пользователь мог скачать portable-джар браузером в папку Downloads —
    # ищем там по имению, чтобы не заставлять качать то же самое повторно.
    # Windows скачивает из Chrome/Edge без расширения (bootstrap / bootstrap (1)),
    # поэтому паттерны — без обязательного .jar; размер и PK-сигнатуру
    # проверяем ниже, мусор отсеется.
    for pattern in ("*egacy*auncher*", "bootstrap*", "launcher*"):
        try:
            candidates += sorted(Path.home().glob(f"Downloads/{pattern}"),
                                 key=lambda p: p.stat().st_size, reverse=True)
        except Exception as e:
            debug.dbg(f"_find_local_legacy_jar glob {pattern}: {e}",
                      level="WARN")
    # Legacy Launcher Portable умеет обновлять сам себя: если на диске уже
    # есть его домашняя папка (с jar внутри) — берём лаунчер оттуда.
    from modules.platform_utils import IS_WINDOWS as _IS_WIN
    if _IS_WIN:
        seen = set()
        for base in (Path(os.environ.get("APPDATA", "")) / ".minecraft",
                    Path(os.environ.get("APPDATA", "")) / "legacy-launcher",
                    Path.home() / ".minecraft"):
            try:
                if not base.is_dir():
                    continue
                for cand in sorted(base.rglob("*.jar"),
                                   key=lambda p: p.stat().st_size,
                                   reverse=True)[:20]:
                    nm = cand.name.lower()
                    if ("launch" in nm or "legacy" in nm
                            or "bootstrap" in nm) \
                            and cand.stat().st_size > LEGACY_JAR_MIN_SIZE \
                            and str(cand) not in seen:
                        seen.add(str(cand))
                        candidates.append(cand)
            except Exception as e:
                debug.dbg(f"_find_local_legacy_jar appdata scan: {e}",
                          level="WARN")
    for p in candidates:
        try:
            if not (p.exists() and p.is_file()):
                continue
            if p.suffix.lower() == ".exe":
                continue  # установщик не подходит (Wine WOW64 crash)
            if p.stat().st_size > LEGACY_JAR_MIN_SIZE and _looks_like_jar(p):
                return p
        except OSError:
            continue
    return None


def _looks_like_jar(p):
    """Первые 4 байта — PK\\x03\\x04 (zip/jar). Отсекает HTML-заглушки прокси."""
    try:
        with open(p, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


# ── Legacy Launcher: кэш и однократный curl-прогон (Windows) ──────────
# Вынесено из setup_legacy(), чтобы поведение проверялось тестами без
# реального запуска установщика.

def _cached_jar_ok(cand):
    """Кэш portable .jar на месте и настоящий (PK-сигнатура, размер)."""
    try:
        return (cand.exists()
                and cand.stat().st_size >= LEGACY_JAR_MIN_SIZE
                and _looks_like_jar(cand))
    except OSError:
        return False


def _exe_size_ok(p):
    try:
        return p.exists() and p.stat().st_size > 2 * 1024 * 1024
    except OSError:
        return False


def _exe_cached_ok(jar_dir):
    # Плюс «Legacy Launcher Setup.exe» — имя, под которым браузер
    # сохраняет официальный установщик; раньше кэш по нему не
    # находился и 117 МБ качались заново.
    from modules.config import LEGACY_EXE_CACHE_NAMES
    if _exe_size_ok(jar_dir / LEGACY_EXE_CACHE_NAMES[0]):
        return True
    for nm in LEGACY_EXE_CACHE_NAMES[1:]:
        if _exe_size_ok(jar_dir / nm):
            return True
    return False


def _exe_cached_path(jar_dir):
    """Куда реально лёг установщик (наше имя или имя браузера)."""
    from modules.config import LEGACY_EXE_CACHE_NAMES
    if _exe_size_ok(jar_dir / LEGACY_EXE_CACHE_NAMES[0]):
        return jar_dir / LEGACY_EXE_CACHE_NAMES[0]
    for nm in LEGACY_EXE_CACHE_NAMES[1:]:
        if _exe_size_ok(jar_dir / nm):
            return jar_dir / nm
    return None


def _legacy_curl_once(cand, exe_cand):
    """ОДИН прогон curl.exe по всем очередям Legacy с общим бюджетом.

    Очередь .jar-зеркал идёт первой; хвост очереди — официальный CDN,
    отдающий .exe-установщик (не проходит _looks_like_jar!). Чтобы такой
    файл не считался неудачей и НЕ перекачивался вторым кругом (именно
    так рождалась простыня «curl не смог» из двух полных очередей),
    валидатор принимает и PK-.jar, и MZ-.exe, а после прогона .exe
    аккуратно перекладывается из cand в exe_cand.

    Второй очередью добавляем чистый список .exe-CDN: если бюджет
    кончится на jar-зеркалах, запасной путь всё равно получит заходы
    (run_curl_queues делит бюджет между очередями). Возвращает резюме
    run_curl_queues (dict).
    """
    from modules.download import run_curl_queues
    from modules.config import LEGACY_WIN_OFFICIAL_CDN

    def _validator(p):
        try:
            with open(p, "rb") as f:
                head = f.read(2)
            return head in (b"PK", b"MZ")
        except OSError:
            return False

    resume = run_curl_queues([legacy_jar_mirrors(True),
                              list(LEGACY_WIN_OFFICIAL_CDN)],
                             cand,
                             min_bytes=LEGACY_JAR_MIN_SIZE,
                             validator=_validator)
    url = resume.get("url")
    if url and cand.exists():
        if _looks_like_jar(cand):
            ok(f"Legacy Launcher (.jar) скачан через curl: {url}")
        else:
            # Официальный CDN отдал .exe-установщик (MZ-, не PK-.jar).
            # Переименовываем: это рабочий вариант для Windows —
            # установщик сам ставит лаунчер (Qt-инсталлятор, нативный).
            try:
                os.replace(str(cand), str(exe_cand))
                ok("Скачан официальный .exe-установщик Legacy Launcher "
                   f"({exe_cand.stat().st_size / 1024 / 1024:.0f} МБ)")
            except OSError as e:
                debug.dbg_exc(e, "setup_legacy/rename-exe")
                try:
                    cand.unlink()
                except OSError:
                    pass
    elif cand.exists():
        try:
            cand.unlink()   # огрызок/HTML — мешается под кэшем
        except OSError:
            pass
    return resume


def _is_exe_installer(path):
    """PE-исполняемый файл (MZ-заголовок)? Офиц. CDN отдаёт .exe вместо .jar."""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except OSError:
        return False


def _legacy_exe_fallback(exe_path, queue):
    """Запасной путь: скачивание официального .exe-установщика.

    Если по пути уже лежит валидный установщик (например его принёс
    _legacy_curl_once или браузер) — повторно ничего НЕ качаем: раньше
    отсутствие этой проверки заставляло перекачивать 117 МБ вторым кругом.
    """
    from modules.download import download_queue_with_curl
    if _exe_cached_ok(exe_path.parent):
        return True
    return bool(download_queue_with_curl(queue, exe_path,
                                         min_bytes=2 * 1024 * 1024,
                                         validator=_is_exe_installer))


def _open_legacy_manual_page():
    """Открывает страницу legacylauncher.ru в браузере (для ручного скачивания)."""
    import webbrowser
    try:
        if webbrowser.open(LEGACY_MANUAL_PAGE):
            ok(f"Открыл в браузере: {LEGACY_MANUAL_PAGE}")
            return True
    except Exception as e:
        debug.dbg(f"webbrowser: {e}", level="WARN")
    hint(f"Открой вручную: {LEGACY_MANUAL_PAGE}")
    return False


def _run_legacy_jar(jar_path):
    """Запуск portable .jar через найденную Java (без Wine).

    На Windows — detached-запуск (javaw), терминал не блокируется.
    Возвращает True, если процесс реально стартовал; иначе понятное
    сообщение и False (пользователь сможет запустить jar двойным кликом).
    """
    from modules.platform_utils import IS_WINDOWS as _IS_WIN
    java_bin, java_home, java_status = find_java()
    if not java_bin:
        err("Java не найдена — портативный .jar требует Java.")
        hint("Windows: winget install Microsoft.OpenJDK.17  "
             "или команда `install-java` (портативная JDK 17)")
        return False
    hint(f"Запускаю Legacy Launcher ({Path(jar_path).name})...")
    try:
        env = dict(os.environ)
        truststore = None
        try:
            truststore = _find_system_truststore()
        except Exception as e:
            debug.dbg(f"truststore: {e}", level="WARN")
        if truststore and not _IS_WIN:
            # Red OS/OpenJDK: русские CA в системном truststore, но не в
            # cacerts JVM — внутренний http-клиент лаунчера падает по SSL.
            opts = env.get("JAVA_TOOL_OPTIONS", "")
            env["JAVA_TOOL_OPTIONS"] = (
                (opts + " " if opts else "")
                + f"-Djavax.net.ssl.trustStore={truststore} "
                  "-Djavax.net.ssl.trustStorePassword="
                + SYSTEM_TRUSTSTORE_PASSWORD)
        exe = java_bin
        if _IS_WIN:
            # javaw.exe — окно лаунчера без чёрной консоли; если рядом его
            # нет (например, JRE без bin\javaw), запускаем как есть.
            cand = Path(java_bin).with_name("javaw.exe")
            if cand.is_file():
                exe = str(cand)
            subprocess.Popen([exe, "-jar", str(jar_path)], env=env,
                             cwd=str(Path(jar_path).parent))
            ok("Legacy Launcher запущен (окно откроется отдельно)")
        else:
            subprocess.Popen([exe, "-jar", str(jar_path)], env=env,
                             cwd=str(Path(jar_path).parent))
            ok("Legacy Launcher запущен")
        return True
    except Exception as e:
        debug.dbg_exc(e, "_run_legacy_jar")
        err(f"Не удалось запустить jar: {e}")
        hint(f"Можно вручную: \"{java_bin}\" -jar \"{jar_path}\"")
        return False


def setup_legacy_portable():
    """Скачивает Legacy Launcher Portable .jar (если нужно) и запускает через Java.

    .exe-установщик Legacy Launcher падает в Wine 11 WOW64 с page fault
    (баг Wine, не наш). Portable .jar работает на чистой Java.

    ВАЖНО: на Red OS / RHEL русские CA (Минцифры) лежат в системном
    truststore, но не в cacerts OpenJDK. Передаём JAVA_TOOL_OPTIONS,
    чтобы внутренний JVM-процесс LL тоже доверял этим сертификатам.
    """
    info(f"{BOLD}Legacy Launcher Portable (.jar){RESET}")

    jar_dir = WINE_DIR / "legacy-portable"
    jar_dir.mkdir(parents=True, exist_ok=True)
    jar_path = jar_dir / "LegacyLauncher.jar"

    # 1. Уже скачан?
    existing = _find_local_legacy_jar()
    if existing:
        jar_path = existing
        ok(f"Portable .jar уже есть: {jar_path}")
    else:
        # 2. Скачиваем.
        #    Очередь зеркал: llaun.ch / dl.llaun.ch отдают НАСТОЯЩИЙ portable
        #    bootstrap .jar (PK-сигнатура). api.legacylauncher.net иногда не
        #    резолвится (DNS), а dl.legacylauncher.ru/legacy/installer — это
        #    .exe-установщик, который к тому же отвечает 403 на запросы без
        #    браузерных заголовков («в браузере открывается» ≠ «urlopen
        #    работает»: у urllib другой TLS-фингерпринт и UA).
        info("Скачиваю Legacy Launcher Portable...")
        from modules.platform_utils import IS_WINDOWS as _IS_WIN
        mirrors = legacy_jar_mirrors(_IS_WIN)
        downloaded = download_file(
            mirrors, jar_path,
            "Legacy Launcher Portable",
            min_size_mb=1, silent=False,
        )
        if not downloaded:
            # download_file при RST на всех Python-запросах оставляет на
            # диске битые огрызки (HTML-заглушки фильтров, 302-страницы).
            # Они НЕ проходят _looks_like_jar в _find_local_legacy_jar, но
            # мешают повторной закачке — чистим.
            try:
                if jar_path.exists() and not _looks_like_jar(jar_path):
                    jar_path.unlink()
            except OSError as e:
                debug.dbg_exc(e, "setup_legacy/unlink-bad")
        if downloaded:
            ok(f"Скачано: {jar_path}")
        else:
            # 3. Фоллбэк — ручное скачивание из браузера.
            err("Не удалось скачать Portable .jar автоматически.")
            warn("Браузер открывает сайт, а скрипт — нет? Это нормально:")
            hint("сайты Legacy Launcher блокируют запросы Python/urllib "
                  "(другой User-Agent/TLS) или отдают .exe вместо .jar.")
            print()
            hint(f"Правильная страница для ручного скачивания: {LEGACY_MANUAL_PAGE}")
            hint(f"(прямая ссылка на portable .jar: {LEGACY_MANUAL_JAR_URL})")
            hint("Скачай файл и сохрани его в:")
            hint(f"  {jar_path}")
            try:
                if copy_to_clipboard(str(jar_path)):
                    ok("Путь для сохранения скопирован в буфер обмена")
            except Exception as e:
                debug.dbg(f"clipboard: {e}", level="WARN")
            _open_legacy_manual_page()
            print()
            hint("Если браузер уже что-то скачал — просто нажми Enter ещё раз")
            hint("после окончания загрузки: команда `legacy` сама найдёт файл.")
            # Возможно, браузер уже успел скачать файл (или качал раньше) —
            # ищем сам: не придётся ничего вводить руками.
            found = _find_local_legacy_jar()
            if found:
                ok(f"Нашёл скачанный лаунчер: {found}")
                jar_path = found
            else:
                try:
                    manual = input(f"{YELLOW}Путь к скачанному файлу лаунчера"
                                   f" (Enter — отмена): {RESET}").strip().strip('"')
                except (KeyboardInterrupt, EOFError):
                    print()
                    return False
                if not manual:
                    return False
                jar_path = Path(manual).expanduser()
                if not jar_path.exists():
                    err(f"Файл не найден: {jar_path}")
                    return False
                if jar_path.stat().st_size < LEGACY_JAR_MIN_SIZE:
                    err(f"Файл слишком маленький ({jar_path.stat().st_size} Б) — "
                        "похоже, это не лаунчер")
                    return False
                if jar_path.suffix.lower() == ".exe":
                    # Раньше такой файл просто отвергался. Но официальный
                    # .exe-установщик Legacy Launcher — нормальный Windows-
                    # установщик (крашится он только в Wine на Linux). Если
                    # portable .jar скачать нельзя, пусть ставит его сам:
                    # запускаем и выходим с понятной инструкцией.
                    warn("Это .exe-установщик (не portable .jar).")
                    hint("На Windows он работает нормально — запускаю...")
                    try:
                        subprocess.Popen([str(jar_path)],
                                         cwd=str(jar_path.parent))
                        ok("Установщик запущен: установи лаунчер, затем "
                           "запусти его из меню «Пуск».")
                        return True
                    except Exception as e:
                        debug.dbg_exc(e, "setup_legacy/run-exe-installer")
                        err(f"Не удалось запустить установщик: {e}")
                        hint("Запусти файл двойным кликом вручную.")
                        return False
                if jar_path.suffix.lower() != ".jar":
                    warn(f"Неожиданное расширение: {jar_path.suffix} — пробую запустить как .jar")

    # 4. Windows: запуск detached (javaw) — терминал не блокируется, а
    # bootstrap-джар при сетевом сбое умеет зависать/падать с кодом 1; в
    # окне консоли это выглядит как «всё сломалось». На Linux оставляем
    # прежний синхронный запуск с ретраями (там доверие к CA Минцифры
    # требует JAVA_TOOL_OPTIONS и вывода кода возврата).
    from modules.platform_utils import IS_WINDOWS as _IS_WIN
    if _IS_WIN:
        print()
        return _run_legacy_jar(jar_path)

    # 5. Java
    java_bin, java_home, java_status = find_java()
    if not java_bin:
        err("Java не найдена. Portable .jar требует Java.")
        hint("Установи: install-java")
        return False
    if java_home:
        ok(f"Java: {java_home}")
    else:
        warn(f"Java: {java_status}")

    # 5. Окружение с системным truststore
    env = _java_env_with_truststore()
    truststore = _find_system_truststore()
    if truststore:
        ok(f"SSL truststore: {truststore}")
    print()

    # 6. Запуск (несколько попыток — bootstrap-джары иногда падают с кодом 1
    #    при сетевом сбое на первой загрузке)
    MAX_LAUNCH_ATTEMPTS = 2
    rc = None
    for attempt in range(1, MAX_LAUNCH_ATTEMPTS + 1):
        info(f"Запускаю Legacy Launcher Portable... (попытка {attempt})")
        hint("Закрой окно лаунчера КРЕСТИКОМ когда закончишь")
        print()
        try:
            r = subprocess.run(
                [java_bin, "-jar", str(jar_path)],
                env=env, check=False,
            )
            rc = r.returncode
        except FileNotFoundError:
            debug.dbg(f"java_bin не найден: {java_bin}", "ERR")
            err(f"Путь к Java больше не существует: {java_bin}")
            hint("Проверь install-java или удали ~/java и поставь заново")
            return False
        except Exception as e:
            debug.dbg_exc(e, "setup_legacy_portable/run")
            err(f"Не запустить: {e}")
            return False

        if rc == 0:
            ok("Legacy Launcher завершён")
            return True

        warn(f"Java завершилась с кодом {rc}")
        if attempt < MAX_LAUNCH_ATTEMPTS:
            try:
                again = input(f"{YELLOW}Попробовать ещё раз? [Y/n]: {RESET}").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print()
                break
            if again not in ("", "y", "yes", "д", "да"):
                break

    print()
    info("Если в логе выше 'PKIX path building failed':")
    hint("Сертификаты Минцифры не попали в Java-truststore.")
    hint("Проверь: ls -la /etc/pki/ca-trust/extracted/java/cacerts")
    hint("Если файла нет: sudo update-ca-trust")
    hint("Если файл есть, но пустой: поставь CA Минцифры (см. README)")
    hint("Альтернатива: используй Prism Launcher (команда `prism`)")
    print()
    return False


def setup_legacy():
    """Legacy Launcher.

    .exe-установщик на Wine 11 стабильно падает с page fault в WOW64
    (баг Wine, не наш). Если .exe-версии в префиксе нет — сразу идём в
    Portable .jar, не тратя время на скачивание и запуск установщика.

    Перед скачиванием проверяем окружение: без Java portable-вариант
    физически не запустится, поэтому предупреждаем сразу и предлагаем
    установить JDK — а не роняем скрипт ошибкой в момент запуска.
    """
    info(f"{BOLD}Legacy Launcher{RESET}")

    # 0. Окружение: Legacy Portable = чистая Java. Без неё дальше идти смысла нет.
    java_bin, java_home, java_status = find_java()
    if not java_bin:
        warn(f"Java: {java_status}. Legacy Launcher Portable работает на Java.")
        try:
            answer = input(f"{MAGENTA}Скачать портативную JDK 17 сейчас? [Y/n]: {RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return False
        if answer in ("", "y", "yes", "д", "да"):
            if not install_portable_java():
                err("JDK установить не удалось — Legacy Launcher без Java не запустить.")
                hint("Попробуй позже: install-java, затем снова legacy")
                return False
            java_bin, java_home, java_status = find_java()
        else:
            hint("Отменено. Установи Java командой install-java, затем повтори legacy")
            return False

    # 1. Уже установлен через .exe-установщик? (только Linux/Wine: на
    # Windows поиск идёт по Wine-префиксу, которого там нет)
    from modules.platform_utils import IS_WINDOWS as _IS_WIN
    if not _IS_WIN:
        found = _find_legacy_installed()
        if found:
            ok(f"Legacy Launcher уже установлен: {found}")
            return True

    if _IS_WIN:
        # На Windows Wine нет и не нужен — сразу portable .jar на чистой Java.
        info("Portable (.jar) — нативный запуск через Java, Wine не используется")
    else:
        warn("Wine-установщик Legacy Launcher известен крашем на Wine 11 (WOW64)")
        info("Использую Portable (.jar) — работает без Wine")
    print()

    # Windows: если в сети RST-блокировка Python-TLS, питоновские попытки
    # в setup_legacy_portable падают на всех зеркалах. Сначала быстро
    # прогоняем curl.exe ОДИН раз по всем очередям (см. run_curl_queues);
    # скачанный jar затем увидит _find_local_legacy_jar() и
    # setup_legacy_portable отработает офлайн.
    #
    # Раньше здесь шёл download_queue_with_curl по ВСЕМ зеркалам, а после
    # неудачи setup_legacy_portable повторял тот же круг уже Python-ом, а
    # третий круг качал .exe-запасной путь — в DPI-сети это простыня из
    # «curl не смог: rc=35» на 5~7 минут вместо результата. Теперь один
    # общий бюджет попыток на все очереди (MAX_CURL_QUEUE_ATTEMPTS).
    if _IS_WIN:
        jar_dir = WINE_DIR / "legacy-portable"
        jar_dir.mkdir(parents=True, exist_ok=True)
        cand = jar_dir / "LegacyLauncher.jar"
        exe_cand = jar_dir / "LegacyLauncher-setup.exe"

        if not _cached_jar_ok(cand) and not _exe_cached_ok(jar_dir):
            resume = _legacy_curl_once(cand, exe_cand)
            if resume.get("truncated"):
                warn(f"Сеть не пропускает домены лаунчера — "
                     f"сделано {resume['attempts']} заходов из "
                     f"{resume['mirrors_total']} (дальше бессмысленно).")
                hint("Диагноз блокировки: команда netcheck; быстрый путь — "
                     "скачать .jar браузером (llaun.ch/jar) в Downloads, "
                     "скрипт найдёт его сам при следующем `legacy`.")

        # Если есть скачанный .exe-установщик — запускаем его сразу,
        # portable .jar больше не нужен (установщик сам всё поставит).
        if _exe_cached_ok(jar_dir):
            run_target = _exe_cached_path(jar_dir)
            hint("Запускаю официальный установщик Legacy Launcher...")
            try:
                os.startfile(str(run_target))  # noqa: S606 — явное действие пользователя
                ok("Установщик запущен.")
                hint("После установки открой Legacy Launcher из меню «Пуск»,"
                     " скачай в нём версию Minecraft и повтори `legacy` —"
                     " скрипт найдёт установленный лаунчер сам.")
                return True
            except Exception as e:
                debug.dbg(f"startfile: {e}", level="WARN")
                hint(f"Запусти вручную двойным кликом: {run_target}")
                return True

    good = setup_legacy_portable()
    if not good and _IS_WIN:
        # На Windows сеть может рвать и TLS, и Java-запросы лаунчера.
        # Запасной путь: официальный .exe через curl.exe (Schannel),
        # потом запускаем установщик штатными средствами Windows.
        # Приоритет — Yandex CDN (проверен: отдаёт настоящий .exe 117 МБ,
        # медленный старт но стабильная закачка); старый URL — запасной.
        hint("Portable не скачался — пробую запасной вариант: "
             ".exe-установщик через curl.exe (как браузер)")
        exe_path = WINE_DIR / "legacy-portable" / "LegacyLauncher-setup.exe"
        from modules.download import warn_if_windows_proxy
        from modules.config import LEGACY_WIN_OFFICIAL_CDN
        # Перед каждой загрузкой — проверка прокси в настройках Windows:
        # включённый прокси даёт «долго думает, но качает» и рвёт TLS.
        warn_if_windows_proxy()
        exe_queue = list(LEGACY_WIN_OFFICIAL_CDN) + \
            [("installer (запасной)",
              "https://dl.legacylauncher.ru/legacy/installer")]
        got = _legacy_exe_fallback(exe_path, exe_queue)
        if got:
            ok(f"Скачан установщик: {exe_path}")
            try:
                os.startfile(str(exe_path))  # noqa: S606 — явное действие пользователя
                hint("Запустился установщик — после установки добавь игру в"
                     " лаунчере и повтори `legacy`")
                return True
            except Exception as e:
                debug.dbg(f"startfile: {e}", level="WARN")
                hint(f"Запусти вручную двойным кликом: {exe_path}")
                return True
        else:
            err("curl.exe тоже не смог — сеть блокирует и его.")
    return good


# ═══════════════════════════════════════════════════════════════════
#  FABRIC / FABULOUSLY OPTIMIZED
# ═══════════════════════════════════════════════════════════════════

def download_fabric_installer():
    installers_dir = WINE_DIR / "fabric"
    installers_dir.mkdir(parents=True, exist_ok=True)
    dest = installers_dir / "fabric-installer.jar"
    # автофикс: битый/HTML-заглушка в кэше -> удалить и качнуть заново
    _autofix_clean_file(dest, min_bytes=100 * 1024)
    if _autofix_is_html_jar(dest):
        warn("Кэш fabric-installer.jar — HTML-заглушка, автофикс: перекачиваю")
        _autofix_remove(dest)
    if dest.exists() and dest.stat().st_size > 100 * 1024:
        return dest
    try:
        data = _http_get_json(FABRIC_META, timeout=30)
        latest = data[0].get("version", "1.0.1")
    except Exception:
        latest = "1.0.1"
    url = f"{FABRIC_MAVEN}/{latest}/fabric-installer-{latest}.jar"
    info(f"Скачиваю Fabric installer {latest}...")
    if download_file([("direct", url)], dest, "Fabric installer", min_size_mb=0):
        if _autofix_is_html_jar(dest):
            warn("Скачался HTML вместо JAR — автофикс: удаляю битый файл")
            _autofix_remove(dest)
            return None
        return dest
    return None


def _maven_path(coord):
    """'net.fabricmc:fabric-loader:0.19.5' -> 'net/fabricmc/fabric-loader/0.19.5/fabric-loader-0.19.5.jar'"""
    g, a, v = coord.split(":")
    return f"{g.replace('.', '/')}/{a}/{v}/{a}-{v}.jar"


MAVEN_FORGE = "https://maven.minecraftforge.net/"


def _download_lib(lib_entry, game_dir, extra_mirrors=None):
    """Скачивает одну библиотеку профиля (Fabric/Forge) в libraries/.

    extra_mirrors — доп. maven-базы (например maven.minecraftforge.net для
    библиотек Forge, которых нет на fabric-maven/Central).
    Возвращает True/False.
    """
    dl = lib_entry.get("downloads", {}) or {}
    art = dl.get("artifact") or {}
    rel = art.get("path")
    if not rel:
        name = lib_entry.get("name", "")
        if ":" not in name:
            return True   # native-rule без артефакта — не критично
        rel = _maven_path(name)
    dest = game_dir / "libraries" / rel
    if dest.exists() and dest.stat().st_size > 0:
        return True
    urls = []
    if art.get("url"):
        urls.append(("direct", art["url"]))
    for base in (extra_mirrors or []):
        urls.append((base.rstrip("/").split("//")[-1], base + rel))
    urls.append(("maven.fabricmc.net", MAVEN_FABRIC + rel))
    urls.append(("Maven Central", MAVEN_CENTRAL + rel))
    dest.parent.mkdir(parents=True, exist_ok=True)
    return bool(download_file(urls, dest, Path(rel).name, min_size_mb=0, silent=True))


def install_fabric_profile(game_dir, mc_version, loader_version):
    """Устанавливает Fabric БЕЗ Java: берёт готовый профиль с meta.fabricmc.net,
    кладёт его в versions/<id>/ и скачивает библиотеки (loader+intermediary+mixin).

    Это автофикс-путь: когда нет Java или installer упал, лаунчер всё равно
    увидит рабочую версию Fabric после докачки vanilla jar (finish_version_install).
    """
    game_dir = Path(game_dir)
    version_id = f"fabric-loader-{loader_version}-{mc_version}"
    vdir = game_dir / "versions" / version_id
    prof_path = vdir / f"{version_id}.json"
    try:
        profile = _http_get_json(
            f"{FABRIC_META_API}/versions/loader/{mc_version}/{loader_version}/profile",
            timeout=60)
    except Exception as e:
        debug.dbg_exc(e, "install_fabric_profile/meta")
        warn(f"meta.fabricmc.net недоступен: {e}")
        return False
    if profile.get("id") != version_id:
        profile["id"] = version_id
    vdir.mkdir(parents=True, exist_ok=True)
    prof_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    ok(f"Профиль Fabric создан: versions/{version_id}/")
    libs = profile.get("libraries", [])
    info(f"Скачиваю библиотеки Fabric ({len(libs)} шт.)...")
    done = failed = 0
    for l in libs:
        if _download_lib(l, game_dir):
            done += 1
        else:
            failed += 1
    ok(f"Библиотек: {done}, ошибок: {failed}")
    lp = game_dir / "launcher_profiles.json"
    if not lp.exists():
        lp.write_text(json.dumps({"profiles": {}, "clientToken": ""}, indent=2), encoding="utf-8")
    return failed == 0


def install_fabric_loader(game_dir, mc_version, loader_version, java_bin="java"):
    installer = download_fabric_installer()
    if not installer:
        err("Fabric installer не скачан")
        return False
    game_dir = Path(game_dir)
    # Fabric installer refuse работать, если launcher directory не существует —
    # раньше это давало "Launcher directory not found" и установка молча падала.
    try:
        (game_dir / "versions").mkdir(parents=True, exist_ok=True)
    except Exception as e:
        debug.dbg_exc(e, "install_fabric_loader/mkdir")
        err(f"Не создать каталог игры {game_dir}: {e}")
        return False
    expected_json = game_dir / "versions" / f"fabric-loader-{loader_version}-{mc_version}" / f"fabric-loader-{loader_version}-{mc_version}.json"
    info(f"Устанавливаю Fabric {loader_version} для MC {mc_version} в {game_dir}...")
    try:
        r = subprocess.run(
            [java_bin, "-jar", str(installer), "client",
             "-dir", str(game_dir),
             "-mcversion", mc_version,
             "-loader", loader_version,
             "-noprofile"],
            check=False, timeout=600, capture_output=True, text=True,
        )
        if r.returncode != 0:
            err(f"Fabric installer код {r.returncode}")
            out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
            if out:
                print(out[-800:])
            return False
        if not expected_json.exists():
            err(f"Профиль версии не создан: {expected_json}")
            return False
        ok(f"Fabric {loader_version} установлен")
        return True
    except FileNotFoundError:
        err("Java не найдена (путь битый). Установи: install-java")
        return False
    except Exception as e:
        debug.dbg_exc(e, "install_fabric_loader")
        err(f"Fabric: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════
#  ДОБОРКА ВЕРСИИ: vanilla jar + merged profile + assets index
#  Без этого лаунчер видит профиль, но игра не стартует
#  ("сборки не знают куда встать / появляются ошибки").
# ═══════════════════════════════════════════════════════════════════

def finish_version_install(game_dir, mc_version, version_id):
    """Доводит установку до конца: скачивает client.jar Mojang,
    объединяет профиль загрузчика с манифестом Minecraft и кладёт
    assets-index. Всё — строго в <game_dir>/versions/."""
    game_dir = Path(game_dir)
    vdir = game_dir / "versions" / version_id
    prof_path = vdir / f"{version_id}.json"
    if not prof_path.exists():
        err(f"Профиль не найден: {prof_path}")
        return False
    try:
        manifest = _http_get_json(MOJANG_VERSION_MANIFEST)
        entry = next(v for v in manifest["versions"] if v["id"] == mc_version)
        vm = _http_get_json(entry["url"], timeout=60)
    except Exception as e:
        debug.dbg_exc(e, "finish_version/mojang")
        warn(f"Mojang meta недоступна: {e}")
        hint("Версия уже лежит в versions/ — лаунчер может докачать сам при запуске.")
        return True

    try:
        profile = json.loads(prof_path.read_text(encoding="utf-8"))
    except Exception as e:
        debug.dbg_exc(e, "finish_version/read_profile")
        return False

    # Уже доведено ранее?
    if profile.get("inheritsFrom") == mc_version and profile.get("mainClass"):
        ok("Версия уже доведена (merged-профиль)")
    else:
        merged = dict(vm)
        merged["id"] = version_id
        merged["inheritsFrom"] = mc_version
        merged["jar"] = mc_version
        merged["mainClass"] = profile.get("mainClass", vm.get("mainClass"))
        merged["type"] = profile.get("type", "release")
        libs = []
        for l in profile.get("libraries", []) + vm.get("libraries", []):
            l = dict(l)
            l.pop("downloaders", None)
            libs.append(l)
        merged["libraries"] = libs
        prof_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        ok("Профиль объединён с манифестом Minecraft")

    # vanilla client.jar -> versions/<mc>/<mc>.jar
    jar_path = game_dir / "versions" / mc_version / f"{mc_version}.jar"
    dl = vm.get("downloads", {}).get("client", {})
    url = dl.get("url") or dl.get("raw", {}).get("url")   # старые версии имеют raw/server
    sha1 = dl.get("sha1") or dl.get("raw", {}).get("sha1")
    if url:
        jar_path.parent.mkdir(parents=True, exist_ok=True)
        if jar_path.exists() and sha1 and _sha1(jar_path) == sha1:
            ok("client.jar уже на месте")
        else:
            info(f"Скачиваю Minecraft {mc_version} client.jar (~40 МБ)...")
            if download_file([("Mojang", url)], jar_path, "Minecraft client", min_size_mb=5):
                if sha1 and _sha1(jar_path) != sha1:
                    warn("SHA-1 client.jar не совпал — удаляю битый файл")
                    try:
                        jar_path.unlink()
                    except Exception as e:
                        debug.dbg_exc(e, "minecraft")
                else:
                    ok("client.jar скачан и проверен")
            else:
                warn("client.jar не скачан — лаунчер докачает сам")
    # assets index -> assets/indexes/<id>.json
    assets = vm.get("assetIndex", {})
    a_url, a_id = assets.get("url"), assets.get("id")
    if a_url and a_id:
        a_path = game_dir / "assets" / "indexes" / f"{a_id}.json"
        a_path.parent.mkdir(parents=True, exist_ok=True)
        if not a_path.exists():
            info("Скачиваю индекс ресурсов (assets)...")
            if download_file([("Mojang", a_url)], a_path, "Assets index", min_size_mb=0):
                ok("Assets index скачан")
            else:
                warn("Assets index не скачан — не критично")
    # launcher_profiles.json — без него Legacy/TLauncher считают папку «не игрой»
    lp = game_dir / "launcher_profiles.json"
    if not lp.exists():
        lp.write_text(json.dumps({"profiles": {}, "clientToken": ""}, indent=2), encoding="utf-8")
        ok("Создан launcher_profiles.json")
    return True


def unpack_mod_files(manifest, game_dir):
    """Доборка недостающих модов из манифеста .mrpack. Возвращает число успешных."""
    fixed = 0
    for entry in manifest.get("files", []):
        path = entry.get("path", "")
        env = entry.get("env", {})
        if env.get("client") == "unsupported":
            continue
        dest = Path(game_dir) / path
        hashes = entry.get("hashes", {})
        sha512 = hashes.get("sha512")
        if dest.exists() and sha512 and verify_sha512(dest, sha512)[0]:
            continue
        urls = entry.get("downloads", [])
        if not urls:
            continue
        mirrors = [("direct", u) for u in urls]
        dest.parent.mkdir(parents=True, exist_ok=True)
        _autofix_remove(dest) if dest.exists() else None
        if download_file(mirrors, dest, path.split("/")[-1], min_size_mb=0, silent=True):
            if not sha512 or verify_sha512(dest, sha512)[0]:
                fixed += 1
    return fixed


def unpack_mrpack_to_game(mrpack_path, game_dir, java_bin="java"):
    # game_dir может прийти строкой (например, из кэша state.json) — без
    # нормализации дальше падало `game_dir / path` (TypeError).
    game_dir = Path(game_dir)
    info(f"{BOLD}Распаковка .mrpack{RESET}")
    try:
        with zipfile.ZipFile(mrpack_path, "r") as z:
            names = z.namelist()
            index_name = "modrinth.index.json"
            if index_name not in names:
                err("modrinth.index.json не найден")
                return False
            manifest = json.loads(z.read(index_name).decode("utf-8"))
    except Exception as e:
        debug.dbg_exc(e, "unpack_mrpack/open")
        err(f"Открыть .mrpack: {e}")
        return False
    deps = manifest.get("dependencies", {})
    mc_version = deps.get("minecraft")
    loader_version = deps.get("fabric-loader") or deps.get("quilt-loader")
    if not mc_version:
        err("Не найдена версия Minecraft")
        return False
    info(f"MC {mc_version}, Fabric {loader_version or 'не указан'}")
    version_id = None
    if loader_version:
        version_id = f"fabric-loader-{loader_version}-{mc_version}"
        if not install_fabric_loader(game_dir, mc_version, loader_version, java_bin):
            warn("Fabric installer не сработал — автофикс: ставлю профиль через meta.fabricmc.net (без Java)")
            if install_fabric_profile(game_dir, mc_version, loader_version):
                ok("Fabric установлен без Java (готовый профиль + библиотеки)")
            else:
                warn("И этот путь не сработал — mods/config всё равно лягут, лаунчер докачает сам")
                version_id = None
    files = manifest.get("files", [])
    info(f"Скачиваю {len(files)} файлов модов...")
    downloaded = 0
    failed = 0
    for entry in files:
        path = entry.get("path", "")
        env = entry.get("env", {})
        if env.get("client") == "unsupported":
            continue
        dest = game_dir / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        hashes = entry.get("hashes", {})
        sha512 = hashes.get("sha512")
        if dest.exists() and sha512:
            ok_hash, _ = verify_sha512(dest, sha512)
            if ok_hash:
                downloaded += 1
                continue
        urls = entry.get("downloads", [])
        if not urls:
            failed += 1
            continue
        mirrors = [("direct", u) for u in urls]
        if download_file(mirrors, dest, path.split("/")[-1], min_size_mb=0, silent=True):
            if sha512:
                ok_hash, actual = verify_sha512(dest, sha512)
                if not ok_hash:
                    # автофикс: битый файл удалён + вторая попытка по всем зеркалам
                    warn(f"  Хеш не совпал: {path} — автофикс: перекачиваю")
                    _autofix_remove(dest)
                    if download_file(mirrors, dest, path.split("/")[-1], min_size_mb=0, silent=True) \
                            and verify_sha512(dest, sha512)[0]:
                        downloaded += 1
                        continue
                    failed += 1
                    continue
            downloaded += 1
        else:
            failed += 1
    ok(f"Модов: {downloaded}, ошибок: {failed}")
    if failed and _ask_retry("скачать недостающие моды"):
        again = unpack_mod_files(manifest, game_dir)
        downloaded += again
        failed -= again
        ok(f"После автофикса: модов {downloaded}, ошибок {max(failed, 0)}")
    try:
        with zipfile.ZipFile(mrpack_path, "r") as z:
            for prefix in ["overrides/", "client-overrides/"]:
                for name in z.namelist():
                    if name.startswith(prefix) and not name.endswith("/"):
                        rel = name[len(prefix):]
                        if not rel:
                            continue
                        target = game_dir / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(name) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
        ok("Overrides распакованы")
    except Exception as e:
        debug.dbg_exc(e, "unpack_mrpack/overrides")
        warn(f"Overrides: {e}")
    if version_id:
        info("Доводим установку версии до конца (vanilla jar + профиль)...")
        finish_version_install(game_dir, mc_version, version_id)
    return True


def _fo_is_release(tag, name=""):
    """True, если тег/имя — чистый релиз (не alpha/beta/rc/dev/nightly)."""
    s = f"{tag} {name}".lower()
    return not re.search(r"alpha|beta|rc\d|[_\-.]rc|dev|nightly|snapshot|pre", s)


def _fo_pinned_release():
    """FO v5.2.8 — последний релиз Fabulously Optimized для MC 1.20.1.

    Luxium 2.8.4-pre-alpha поддерживает Fabric только на 1.20.1, а ветки FO
    13/14/15 работают лишь на 1.21+, поэтому для модпака FO пиним 1.20.1-ветку.
    ВАЖНО: этот поиск всегда идёт по ОФИЦИАЛЬНОМУ репозиторию FO под тегом
    FO_OFFICIAL_TAG (v5.2.8) — пользовательский WI_FO_VERSION относится к НАШЕМУ
    релизу-починке в _fo_fixed_release и сюда подмешиваться не должен (иначе
    GET /releases/tags/fo-5.2.8-fixed давал бы 404 и запасной путь ломался).
    Возвращает dict в формате _pick_fo_release() или None, если тег не найден.
    """
    tag = FO_OFFICIAL_TAG or "v5.2.8"
    ver_no = tag.lstrip("v")
    mrpack_name = f"Fabulously.Optimized-{ver_no}.mrpack"
    zip_name = f"Fabulously.Optimized-{ver_no}.zip"
    # Баг (проверено живым API): v5.2.8 — ДРЕВНИЙ релиз FO, он на 3-й странице
    # выдачи (репозиторий отдаёт ~270 релизов, новые идут первыми). Поиск только
    # по ?per_page=100 (page=1) тег не находил и молча скатывался к самому
    # свежему FO под MC 1.21+ — Luxium Fabric там не работает. Поэтому: сначала
    # точечный GET /releases/tags/<tag>, затем перебор страниц с ранним выходом
    # (релизы отсортированы по убыванию даты — если встретили тег МОЛОЖЕ
    # искомого по семантике, дальше искать бессмысленно).
    def _rel_dict(r):
        assets = {a["name"]: a for a in r.get("assets", [])}
        mrpack = next((a for n, a in assets.items()
                       if n.endswith(".mrpack")), None)
        if not mrpack:
            return None
        zip_asset = next((a for n, a in assets.items()
                          if n.endswith(".zip")), None)
        return {
            "tag": tag,
            "version": ver_no,
            "mc_versions": [FO_1201_MC_VERSION],
            "mrpack_url": mrpack["browser_download_url"],
            "mrpack_name": mrpack["name"],
            "zip_url": zip_asset["browser_download_url"] if zip_asset else None,
            "zip_name": zip_asset["name"] if zip_asset else None,
            "size": mrpack.get("size", 0),
        }
    try:
        r = _github_get_json(f"{FO_GITHUB_API}/tags/{tag}", timeout=30)
        if isinstance(r, dict) and r.get("tag_name") == tag:
            got = _rel_dict(r)
            if got:
                return got
            return None          # тег есть, но .mrpack нет — fallback не нужен
    except Exception as e:
        debug.dbg_exc(e, "fo/pinned_tag_api")
    want = tuple(int(x) for x in re.findall(r"\d+", tag)) or (5, 2, 8)
    try:
        for page in range(1, 6):
            releases = _github_get_json(
                f"{FO_GITHUB_API}?per_page=100&page={page}", timeout=30)
            if not isinstance(releases, list) or not releases:
                break
            for r in releases:
                if r.get("tag_name") == tag:
                    got = _rel_dict(r)
                    if got:
                        return got
                    return None
                nums = tuple(int(x) for x in re.findall(r"\d+",
                                                        r.get("tag_name", "")))
                if nums and nums[:len(want)] > want:
                    break        # пошли более свежие теги — ниже v5.2.8 нет смысла
            else:
                continue
            break                # внутренний break -> страницу больше не качаем
    except Exception as e:
        debug.dbg_exc(e, "fo/pinned_api")
    # API недоступен — известные URL ОФИЦИАЛЬНОГО релиза v5.2.8 (детерминированный шаблон)
    if ver_no == "5.2.8":
        return {"tag": tag, "version": ver_no,
                "mc_versions": [FO_1201_MC_VERSION],
                "mrpack_url": FO_OFFICIAL_MRPACK_URL, "mrpack_name": mrpack_name,
                "zip_url": FO_OFFICIAL_MRPACK_URL[:-len(".mrpack")] + ".zip",
                "zip_name": zip_name, "size": 61666}
    return None


def _mrpack_version_from_manifest(path):
    """Версия MC из modrinth.index.json локального .mrpack (или None)."""
    try:
        with zipfile.ZipFile(path, "r") as z:
            m = json.loads(z.read("modrinth.index.json").decode("utf-8"))
        return m.get("dependencies", {}).get("minecraft")
    except Exception as e:
        debug.dbg_exc(e, "fo/local_mrpack_meta")
        return None


def _gh_asset_or_none(url, timeout=30):
    """JSON одного ассета GitHub-релиза по browser_download_url (или None)."""
    m = re.search(r"github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/(.+)$",
                  url or "")
    if not m:
        return None
    owner, repo, tag, name = m.groups()
    api = (f"https://api.github.com/repos/{owner}/{repo}/releases/"
           f"tags/{urllib.parse.quote(tag)}")
    try:
        data = _github_get_json(api, timeout=timeout)
    except Exception as e:
        debug.dbg_exc(e, "fo/fixed_tag_api")
        return None
    want = urllib.parse.unquote(name).lower()
    for a in data.get("assets", []):
        if a.get("name", "").lower() == want and a.get("browser_download_url"):
            return a
    return None


def _fo_fixed_release():
    """Починенный FO v5.2.8 (Fabric, MC 1.20.1) — приоритетный источник.

    Ванильный FO v5.2.8 плохо настроен под Luxium: обновлены Fabric loader
    и все моды, удалены Controlify и Iris Shaders (шейдеры рендерит сам
    Luxium). Готовый .mrpack экспортирован из Modrinth в GitHub-релизы этого
    репозитория — он и есть ОСНОВНОЙ источник (файл не закоммичен в git:
    выкладываешь починку один раз в релиз, пользователи получают её через
    `fo` автоматически, при этом скачивание идёт через gh-прокси — работает
    даже когда GitHub глючит). Локальный *.mrpack (корень репо / кэш) —
    только офлайн-переопределение для продвинутых, не обязательность.
    Порядок источников (файл в git НЕ нужен — это только офлайн-кэш):
      1) точечный GitHub API по тегу релиза (ассет *.mrpack) — без пагинации,
         не упирается в лимит 60 запросов/час на списке из 100 релизов;
      1b) если по тегу ассет не нашёлся — общий список релизов (на случай,
         если пользователь переопределил WI_FO_VERSION чужим тегом);
      2) известная прямая ссылка FO_1201_MRPACK_URL (API недоступен),
         скачивание через gh-прокси (fo_mrpack_mirrors); если URL точный —
         метаданные берём из API ассета (реальное имя/размер из релиза, а не
         типовое имя из формулы — иначе при переименовании ассета получаем
         404 на всех зеркалах);
      3) локальный *.mrpack (корень репо / WINE_DIR) — офлайн-переопределение,
         используется когда сеть не отдала манифест релиза.
    Возвращает dict в формате _pick_fo_release() или None.
    """
    tag = (FO_1201_PIN_TAG or "").strip() or "fo-5.2.8-fixed"
    # чистим и префикс ('fo-', 'v'), и суффикс ('-fixed'): для отображения и
    # типовых имён остаётся голое '5.2.8' (иначе вернём '5.2.8-fixed')
    ver_no = re.sub(r"^[^\d]*", "", tag).split("-")[0] or "5.2.8"
    api = (f"https://api.github.com/repos/{FO_MRPACK_OWNER}/"
           f"{FO_MRPACK_REPO}/releases")

    def _mk(mrpack):
        return {"tag": tag, "version": ver_no,
                "mc_versions": [FO_1201_MC_VERSION],
                "mrpack_url": mrpack["browser_download_url"],
                "mrpack_name": mrpack["name"],
                "zip_url": None, "zip_name": None,
                "size": mrpack.get("size", 0)}

    # 1) точечный эндпоинт по тегу (1 запрос вместо списка на 100 релизов)
    try:
        rel = _github_get_json(f"{api}/tags/{urllib.parse.quote(tag)}", timeout=30)
        assets = rel.get("assets", [])
        mrpack = next((a for a in assets if a["name"].endswith(".mrpack")), None)
        if not mrpack:  # суффикс -fixed уже в теге — в имени он избыточен
            guess = f"Fabulously.Optimized-{ver_no}.mrpack"
            mrpack = next((a for a in assets
                           if a["name"].lower() == guess.lower()), None)
        if mrpack:
            return _mk(mrpack)
        if assets:
            # ассеты есть, но ни один не .mrpack — вероятно, другой формат
            warn("В релизе нет .mrpack-ассета; пробую остальные источники")
    except Exception as e:
        debug.dbg_exc(e, "fo/fixed_tag_api")
    # 1b) запасной путь — общий список релизов (например, WI_FO_VERSION
    # переопределён тегом, которого нет как отдельного эндпоинта... эндпоинт
    # по тегу работает всегда, но на случай редких глюков API держим fallback)
    try:
        releases = _github_get_json(f"{api}?per_page=100", timeout=30)
        for r in releases:
            if r.get("tag_name") != tag:
                continue
            mrpack = next((a for a in r.get("assets", [])
                           if a["name"].endswith(".mrpack")), None)
            if not mrpack:
                return None
            return _mk(mrpack)
    except Exception as e:
        debug.dbg_exc(e, "fo/fixed_api")
    # API недоступен — известная ссылка релиза. FO_1201_MRPACK_URL может быть
    # точным URL файла или префиксом .../download/<tag>/ — во втором случае
    # дописываем типовое имя ассета нашего релиза.
    url = FO_1201_MRPACK_URL
    name = url.rsplit("/", 1)[-1]
    if not name.endswith(".mrpack"):
        # «fixed» уже входит в имя тега (fo-5.2.8-fixed) — не удваивать суффикс
        suffix = "" if "fixed" in tag.lower() else "-fixed"
        name = f"Fabulously.Optimized-{ver_no}{suffix}.mrpack"
        url = url.rstrip("/") + "/" + name
    local = find_local_mrpack()
    if local is None and EMBEDDIUM_SHA1_PIN:
        # Вниманию владельца репозитория: embeddium*.jar нужен ТОЛЬКО запасному
        # Forge-пути `ofps` (Embeddium — mandatory-зависимость Luxium в mods.toml).
        # В Fabric-пути `fo` его ставить НЕЛЬЗЯ: FO содержит Sodium, а Embeddium —
        # форк Sodium, два рендер-бэкенда конфликтуют. Локальный embeddium*.jar в
        # корне/кэше — офлайн-кэш для `ofps`; на выбор mrpack он не влияет.
        emb_home = next((p for p in (REPO_ROOT, WINE_DIR)
                         if p.is_dir() and any(p.glob("embeddium*.jar"))), None)
        if emb_home is not None:
            warn(f"Внимание: в {emb_home} лежит embeddium*.jar — это офлайн-кэш "
                 "для Forge-установки (`ofps`). Для Fabric-пути `fo` он не "
                 "нужен; файл можно удалить (см. .gitignore).")
    if local is not None:
        # Сеть не отдала манифест релиза, но есть локальный файл: сверяем MC
        # по его modrinth.index.json — чужой пак (другая версия) не берём.
        mc_v = _mrpack_version_from_manifest(local)
        if mc_v and mc_v != FO_1201_MC_VERSION:
            warn(f"Локальный .mrpack {local.name} — под MC {mc_v}, "
                 f"а нужен {FO_1201_MC_VERSION}; игнорирую его")
            local = None
    if local is not None:
        # Офлайн-переопределение: ставим локальный файл; mrpack_url — запас,
        # если локальный окажется битым (проверка в setup_fabulously_optimized).
        return {"tag": tag, "version": ver_no,
                "mc_versions": [FO_1201_MC_VERSION],
                "mrpack_url": url, "mrpack_name": name,
                "local_mrpack": str(local),
                "zip_url": None, "zip_name": None,
                "size": local.stat().st_size}
    return {"tag": tag, "version": ver_no,
            "mc_versions": [FO_1201_MC_VERSION],
            "mrpack_url": url, "mrpack_name": name,
            "zip_url": None, "zip_name": None, "size": 0}


def _pick_fo_release():
    """Ищет нужный релиз Fabulously Optimized.

    Порядок приоритетов:
      1) ПОЧИНЕНЫЙ FO v5.2.8 (см. _fo_fixed_release) — локальный .mrpack из
         репозитория либо наш GitHub-релиз (ванильный FO плохо работает с
         Luxium: устаревший Fabric/моды, конфликтующие Controlify/Iris);
      2) официальный неремонтированный FO v5.2.8 (GitHub Fabulously-Optimized);
      3) самый свежий официальный release (как раньше).

    Возвращает dict: tag, version, mrpack_url, mrpack_name, zip_url, zip_name,
    size (+ local_mrpack — путь к локальному файлу, если он есть).
    Только release-версии: prerelease=False, draft отброшен, плюс фильтр
    по имени тега (v14.1.0 годится, v15.0.0-alpha.5 / beta — нет).
    .zip-ассет ищется как запасной вариант (если .mrpack недоступен).
    """
    fixed = _fo_fixed_release()
    if fixed:
        return fixed
    warn("Починенный FO-модпак не найден — беру официальный FO v5.2.8")
    pinned = _fo_pinned_release()
    if pinned:
        return pinned
    warn(f"FO {FO_OFFICIAL_TAG} (1.20.1) не найден — беру самый свежий релиз")
    releases = _github_get_json(f"{FO_GITHUB_API}?per_page=100", timeout=30)
    best = None
    for r in releases:
        if r.get("draft") or r.get("prerelease"):
            continue
        tag = r.get("tag_name", "")
        if not _fo_is_release(tag):
            continue
        assets = [a for a in r.get("assets", []) if _fo_is_release(a["name"])]
        mrpack = next((a for a in assets if a["name"].endswith(".mrpack")), None)
        if not mrpack:
            continue
        zip_asset = next((a for a in assets if a["name"].endswith(".zip")), None)
        cand = {
            "tag": tag,
            "version": tag.lstrip("v"),
            "mc_versions": [],
            "mrpack_url": mrpack["browser_download_url"],
            "mrpack_name": mrpack["name"],
            "zip_url": zip_asset["browser_download_url"] if zip_asset else None,
            "zip_name": zip_asset["name"] if zip_asset else None,
            "size": mrpack.get("size", 0),
        }
        # сортировка по числовой части тега — берём самый свежий релиз
        nums = [int(x) for x in re.findall(r"\d+", tag)]
        key = tuple(nums + [0] * (6 - len(nums)))
        cand["_key"] = key
        if best is None or key > best["_key"]:
            best = cand
    if not best:
        raise RuntimeError("На GitHub нет ни одной release-версии FO")
    best.pop("_key")
    return best


def _validate_mrpack(path):
    """True, если файл — валидный .mrpack с modrinth.index.json и версией MC."""
    try:
        with zipfile.ZipFile(path, "r") as z:
            m = json.loads(z.read("modrinth.index.json").decode("utf-8"))
        return bool(m.get("dependencies", {}).get("minecraft"))
    except Exception:
        return False


def unpack_fo_zip_to_game(zip_path, game_dir, rel_version):
    """Запасной путь установки FO: официальный .zip с GitHub Releases.

    Внутри него modlist.html (ссылки на файлы CurseForge) + overrides/.
    Скачиваем перечисленные моды по ссылкам в mods/, кладём overrides.
    Профиль Fabric создаётся без Java через meta.fabricmc.net.
    Возвращает True/False.
    """
    game_dir = Path(game_dir)
    info(f"{BOLD}Установка из ZIP (запасной вариант){RESET}")
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            modlist_html = ""
            manifest_json = None
            for name in z.namelist():
                base = name.rsplit("/", 1)[-1]
                if base == "modlist.html":
                    modlist_html = z.read(name).decode("utf-8", errors="replace")
                elif base == "manifest.json":
                    try:
                        manifest_json = json.loads(z.read(name))
                    except Exception as e:
                        debug.dbg_exc(e, "minecraft")
            if not modlist_html:
                err("В zip нет modlist.html")
                return False
            urls = re.findall(
                r'href="(https://[^"]+/(?:download|files)/[^"]+\.jar)"', modlist_html)
            mc_version = None
            loader_version = None
            if manifest_json:
                mc_version = (manifest_json.get("minecraftVersion")
                              or manifest_json.get("version"))
                parts = str(manifest_json.get("manifestVersion", ""))
                debug.dbg(f"manifest.json keys: {list(manifest_json.keys())}, mc={mc_version}, mf={parts}")
            for name in z.namelist():
                if "overrides/" not in name or name.endswith("/"):
                    continue
                rel = name.split("overrides/", 1)[1]
                if not rel:
                    continue
                target = game_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            ok("Overrides из zip распакованы")
    except Exception as e:
        debug.dbg_exc(e, "unpack_fo_zip")
        err(f"Распаковка zip: {e}")
        return False

    if not urls:
        err("В modlist.html не нашлось ссылок на моды")
        return False
    # уникальные ссылки, сохраняя порядок
    seen_u = set()
    uniq_urls = [u for u in urls if not (u in seen_u or seen_u.add(u))]
    info(f"Скачиваю {len(uniq_urls)} модов с CurseForge...")
    mods_dir = game_dir / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    done = failed = 0
    for u in uniq_urls:
        fname = u.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        dest = mods_dir / fname
        if dest.exists() and dest.stat().st_size > 10_000:
            done += 1
            continue
        if download_file([("direct", u)], dest, fname, min_size_mb=0, silent=True):
            done += 1
        else:
            failed += 1
    ok(f"Модов: {done}, ошибок: {failed}")

    # профиль Fabric: пробуем определить версию загрузчика из первого конфига
    dep = game_dir / "config" / "fabric_loader_dependencies.json"
    if dep.exists():
        try:
            d = json.loads(dep.read_text(encoding="utf-8"))
            loader_version = (d.get("overrides", {}) or {}).get("java", {}).get("net.fabricmc.fabric-loader") \
                or d.get("v")
        except Exception as e:
            debug.dbg_exc(e, "minecraft")
    if not mc_version:
        warn("Не удалось определить версию Minecraft из zip — лаунчер сам подскажет.")
        hint(f"Профиль можно создать вручную: Fabric {loader_version or '?'} для нужной MC")
        return True
    if loader_version:
        install_fabric_profile(game_dir, mc_version, loader_version)
        finish_version_install(game_dir, mc_version, f"fabric-loader-{loader_version}-{mc_version}")
    hint(f"FO v{rel_version} установлен из zip-варианта")
    return True


def _finish_fo_luxium(game_dir):
    """Luxium (Fabric) поверх установленного Fabulously Optimized.

    Приоритетный модпак для Luxium 2.8.4+ (мод получил поддержку Fabric на
    1.20.1). Неудача не отменяет установку FO: предупреждаем и предлагаем
    запасной путь — `ofps` (Forge-вариант с Embeddium).
    Порядок поиска JAR: локальный файл (корень репо/кэш) -> скачивание с
    нашего GitHub-релиза через gh-прокси; отсутствие файла в git — норма,
    а не ошибка (см. .gitignore).
    """
    local_meta = luxium_supports_fabric()
    if not local_meta:
        info("Локальный Luxium JAR не найден — пробую скачать с GitHub-зеркала")
    if not install_luxium_fabric_mod(game_dir):
        if local_meta:
            warn("Локальный Luxium JAR без fabric.mod.json — шейдер-мод не "
                 "подложен; обнови 'Luxium*.jar' рядом с wine.py или в "
                 "~/wine-portable/ (2.8.4-pre-alpha и новее умеют Fabric)")
        hint("Запасной вариант: команда `ofps` (Optimized fps, Forge 1.20.1, без Oculus)")
        return False
    # Sodium — обязательная зависимость Luxium по fabric.mod.json; FO v5.2.8
    # обычно уже несёт её, поэтому это проверка/доустановка, а не блокировка.
    install_sodium_for_luxium(game_dir)
    hint("Шейдеры: команда `shaders` (Luxium читает обычные shaderpacks/*.zip)")
    return True


def setup_fabulously_optimized():
    info(f"{BOLD}Скачивание Fabulously Optimized{RESET}")
    hint(f"Приоритет: починенный FO v5.2.8 (MC {FO_1201_MC_VERSION}, Fabric, "
         "обновлённые моды, без Controlify/Iris) + Luxium; источник: релиз "
         f"{FO_MRPACK_OWNER}/{FO_MRPACK_REPO} на GitHub или локальный .mrpack")
    try:
        rel = _pick_fo_release()
    except Exception as e:
        debug.dbg_exc(e, "setup_fo/github")
        err(f"GitHub API: {e}")
        return False
    local_pack = rel.get("local_mrpack")
    if local_pack:
        info(f"Release: {rel['tag']} (локальный .mrpack: {Path(local_pack).name})")
    else:
        info(f"Release: {rel['tag']} ({rel['mrpack_name']}, {rel['size'] // 1024} КБ)")

    # ── Куда ставить: ищем реальные .minecraft / game на системе ──
    found = choose_minecraft_dir(auto=True)
    if found is None:
        warn("Папка .minecraft (или game у Legacy) не найдена на системе.")
        print()
        info("Варианты:")
        hint("1. Запусти любой лаунчер (legacy / Prism) один раз — он создаст папку игры")
        hint("2. Укажи путь вручную")
        try:
            manual = input(f"{YELLOW}Путь к .minecraft (Enter — отказаться): {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            manual = ""
        if manual and Path(manual).expanduser().is_dir():
            found = Path(manual).expanduser()
        elif manual:
            err("Такой папки нет")
            return False
        else:
            hint("Повтори `fo` после запуска лаунчера.")
            return True
    game_dir = Path(found)
    (game_dir / "versions").mkdir(parents=True, exist_ok=True)
    ok(f"Директория игры: {game_dir}")

    java_bin, java_home, java_status = find_java()
    if not java_bin:
        info("Java не найдена — использую установку БЕЗ Java (meta.fabricmc.net)")

    # ═══ Путь 0: офлайн-переопределение — локальный .mrpack (корень репо/кэш) ═══
    if local_pack:
        ok(f"Локальный .mrpack найден: {local_pack}")
        if _validate_mrpack(local_pack):
            unpack_mrpack_to_game(Path(local_pack), game_dir, java_bin or "java")
            _finish_fo_luxium(game_dir)
            hint("Готово! Версия Fabric появится в лаунчере")
            return True
        warn("Локальный .mrpack повреждён — автофикс: качаю из интернета")

    # ═══ Путь 1: основной — .mrpack (починенный релиз или официальный FO) ═══
    url = rel.get("mrpack_url")
    if not url:
        err("Нет ни рабочего .mrpack, ни ссылки на него")
        return False
    dest = WINE_DIR / rel["mrpack_name"]
    _autofix_clean_file(dest, min_bytes=10 * 1024)
    if dest.exists() and not _validate_mrpack(dest):
        warn("Кэш .mrpack повреждён — автофикс: перекачиваю")
        _autofix_remove(dest)
    mrpack_ok = False
    # fo_mrpack_mirrors: gh-прокси первыми (GitHub/CDN нестабильны в РФ),
    # прямой адрес — последняя попытка; WI_FO_MRPACK_MIRRORS даёт свои зеркала.
    if dest.exists() or download_file(
            fo_mrpack_mirrors(url),
            dest, "Fabulously Optimized (.mrpack)", min_size_mb=0):
        if _validate_mrpack(dest):
            mrpack_ok = True
        else:
            _autofix_remove(dest)
    if mrpack_ok:
        ok(f".mrpack скачан и проверен: {dest}")
        unpack_mrpack_to_game(dest, game_dir, java_bin or "java")
        _finish_fo_luxium(game_dir)
        hint("Готово! Версия Fabric появится в лаунчере")
        return True

    # ═══ Путь 1b: починенный mrpack недоступен — официальный FO v5.2.8 ═══
    if url != FO_OFFICIAL_MRPACK_URL:
        warn("Починенный .mrpack недоступен — автофикс: ставлю официальный "
             f"FO {FO_OFFICIAL_TAG} (его моды/шейдеры хуже работают с Luxium)")
        ver_no = FO_OFFICIAL_TAG.lstrip("v")
        rel = {"tag": FO_OFFICIAL_TAG, "version": ver_no,
               "mrpack_url": FO_OFFICIAL_MRPACK_URL,
               "mrpack_name": f"Fabulously.Optimized-{ver_no}.mrpack",
               "zip_url": FO_OFFICIAL_MRPACK_URL[:-len(".mrpack")] + ".zip",
               "zip_name": f"Fabulously.Optimized-{ver_no}.zip",
               "size": 61666}
        odest = WINE_DIR / rel["mrpack_name"]
        _autofix_clean_file(odest, min_bytes=10 * 1024)
        if ((odest.exists() or download_file(
                    fo_mrpack_mirrors(rel["mrpack_url"]),
                    odest, "Fabulously Optimized (официальный .mrpack)", min_size_mb=0))
                and _validate_mrpack(odest)):
            ok(f".mrpack скачан и проверен: {odest}")
            unpack_mrpack_to_game(odest, game_dir, java_bin or "java")
            _finish_fo_luxium(game_dir)
            hint("Готово! Версия Fabric появится в лаунчере")
            return True

    # ═══ Путь 2: запасной — официальный .zip релиза + авто-разархивация ═══
    if not rel.get("zip_url"):
        err("Не удалось скачать .mrpack, а zip-ассета в релизе нет")
        hint("Положи починенный .mrpack в корень репозитория (рядом с wine.py) "
             "или в ~/wine-portable/ и повтори `fo` — установка пройдёт офлайн")
        return False
    warn("Основной .mrpack недоступен — автофикс: ставлю из официального zip релиза")
    zpath = WINE_DIR / rel["zip_name"]
    _autofix_clean_file(zpath, min_bytes=10 * 1024)
    mirrors = fo_mrpack_mirrors(rel["zip_url"])
    if not (zpath.exists() and zipfile.is_zipfile(zpath)) and \
            not (download_file(mirrors, zpath, "Fabulously Optimized (.zip)", min_size_mb=0)
                 and zipfile.is_zipfile(zpath)):
        err("Ни .mrpack, ни .zip скачать не удалось")
        hint("Проверь сеть/VPN или скачай вручную с https://github.com/Fabulously-Optimized/fabulously-optimized/releases")
        return False
    unpack_fo_zip_to_game(zpath, game_dir, rel["version"])
    _finish_fo_luxium(game_dir)
    return True


# ═══════════════════════════════════════════════════════════════════
#  OPTIMIZED FPS (Forge-модпак) + LUXIUM (шейдер-мод для Forge)
#  ЗАПАСНОЙ вариант (приоритетный — Fabulously Optimized v5.2.8 + Luxium
#  Fabric, команда `fo`). Для Forge: скачиваем .mrpack с Modrinth, удаляем
#  Oculus (Iris-порт, не нужен — Luxium сам рисует шейдеры на Forge), ставим
#  профиль Forge без Java и добавляем Luxium в mods/.
# ═══════════════════════════════════════════════════════════════════

# promotions_slim.json — исторический адрес; на свежем maven Forge он
# может отсутствовать (404), поэтому фолбек — список версий из
# стандартного maven-metadata.xml (всегда доступен на Reposilite).
FORGE_PROMOTIONS_API = "https://maven.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"


def _forge_build_from_metadata(mc_version):
    """Свежая доступная сборка Forge для MC по maven-metadata.xml."""
    import xml.etree.ElementTree as ET
    prefix = f"{mc_version}-"
    try:
        with _http_get(MAVEN_FORGE + "net/minecraftforge/forge/maven-metadata.xml",
                       timeout=60, headers={"User-Agent": "wine-installer/2.5"}) as r:
            root = ET.fromstring(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        debug.dbg_exc(e, "forge_maven_metadata")
        return None
    builds = [v.text for v in root.iter("version")
              if v.text and v.text.startswith(prefix)]
    if not builds:
        return None

    def key(b):
        return tuple(int(x) if x.isdigit() else 0
                     for x in b[len(prefix):].replace("-", ".").split("."))
    return max(builds, key=key)[len(prefix):]


def _forge_build_for(mc_version):
    """Последний recommended build Forge для MC (fallback: latest/metadata)."""
    try:
        data = _http_get_json(FORGE_PROMOTIONS_API, timeout=30)
    except Exception as e:
        debug.dbg_exc(e, "forge_promotions")
        data = None
    promos = data.get("promos", {}) if isinstance(data, dict) else {}
    build = promos.get(f"{mc_version}-recommended") or promos.get(f"{mc_version}-latest")
    if not build:
        build = _forge_build_from_metadata(mc_version)
    return build


def forge_universal_urls(mc_version, forge_build):
    """Ссылки на universal jar (maven официальный + зеркала CurseForge CDN)."""
    long_id = f"{mc_version}-{forge_build}"
    urls = [("Maven Forge",
             f"{MAVEN_FORGE}net/minecraftforge/forge/{long_id}/forge-{long_id}-universal.jar")]
    for base in CF_EDGE_BASES:
        urls.append((base.split("//")[-1].split(".")[0],
                     f"{base}/files/2576/948/forge-{long_id}-universal.jar"))
    return urls


def _forge_profile_from_installer(mc_version, forge_build):
    """Извлекает готовый профиль Forge из installer.jar (без запуска Java).

    На maven.minecraftforge.net нет profile.json как отдельного артефакта —
    профиль лежит внутри installer'а в `version.json`. Берём его оттуда:
    так лаунчер получает корректные arguments/mainClass/logging вместо
    заглушки с пустым списком библиотек.
    """
    urls = [(f"{MAVEN_FORGE}net/minecraftforge/forge/{mc_version}-{forge_build}/"
             f"forge-{mc_version}-{forge_build}-installer.jar")] + \
           [f"{base}/files/2576/948/forge-{mc_version}-{forge_build}-installer.jar"
            for base in CF_EDGE_BASES]
    tmp = WINE_DIR / f"forge-{mc_version}-{forge_build}-installer.jar.tmp"
    # Первый URL — официальный maven Forge; зеркала CurseForge CDN добавлены
    # на случай его недоступности (в РФ он не блокируется, но лишний фолбек
    # не помешает). download_file сам проверит «бинарность» закачки.
    mirrors = [("Maven Forge", urls[0])] + \
              [("CF-зеркало", u) for u in urls[1:]]
    if not download_file(mirrors, tmp,
                         "Forge installer (для профиля)", min_size_mb=1, silent=True):
        return None
    try:
        with zipfile.ZipFile(tmp, "r") as z:
            raw = z.read("version.json")
        data = json.loads(raw.decode("utf-8"))
        if data.get("libraries"):
            return data
    except Exception as e:
        debug.dbg_exc(e, "forge_profile_from_installer")
    _autofix_remove(tmp)
    return None


def install_forge_profile(game_dir, mc_version, forge_build):
    """Ставит Forge БЕЗ Java installer'а: profile-v2 + libraries + universal jar.

    Лаунчер увидит версию `<mc>-forge-<build>` после finish_version_install.
    Возвращает version_id или None.
    """
    game_dir = Path(game_dir)
    version_id = f"{mc_version}-forge-{forge_build}"
    vdir = game_dir / "versions" / version_id
    prof_path = vdir / f"{version_id}.json"
    long_id = f"{mc_version}-{forge_build}"

    # 1. Профиль: installer.jar -> version.json (основной путь), затем
    #    исторические URL profile.json с maven — на случай если вернутся.
    profile = _forge_profile_from_installer(mc_version, forge_build)
    if profile is None:
        for url in (f"{MAVEN_FORGE}net/minecraftforge/forge/{long_id}/win/server/profile.json",
                    f"{MAVEN_FORGE}net/minecraftforge/forge/{long_id}/profile.json"):
            try:
                p = _http_get_json(url, timeout=60)
                if p.get("libraries"):
                    profile = p
                    break
            except Exception as e:
                debug.dbg_exc(e, "install_forge_profile/fetch")
    if profile is None:
        warn("profile.json Forge недоступен — минимальный профиль (лаунчер докачает сам)")
        profile = {"id": version_id, "inheritsFrom": mc_version, "jar": mc_version,
                   "mainClass": "net.minecraftforge.userdev.LaunchTesting",
                   "type": "release", "libraries": []}
    profile["id"] = version_id
    profile["inheritsFrom"] = mc_version
    profile["jar"] = mc_version
    if not profile.get("mainClass"):
        profile["mainClass"] = "cpw.mods.bootstraplauncher.BootstrapLauncher"
    vdir.mkdir(parents=True, exist_ok=True)
    prof_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    ok(f"Профиль Forge создан: versions/{version_id}/")

    # 2. Библиотеки профиля (Forge-артефакты — с maven.minecraftforge.net).
    #    В profile Forge у библиотек обычно нет downloads: путь считаем из
    #    координат group:artifact:version и качаем с maven-зеркал.
    libs = profile.get("libraries", [])
    info(f"Скачиваю библиотеки Forge ({len(libs)} шт.)...")
    done = failed = 0
    for l in libs:
        if _download_lib(l, game_dir, extra_mirrors=[MAVEN_FORGE]):
            done += 1
        else:
            failed += 1
    ok(f"Библиотек: {done}, ошибок: {failed}")

    # 2b. Maven-main jar: в arguments профиля это "<fmlonly-client-id>-sha..."
    #     (не maven-координаты), поэтому его не скачивает цикл библиотек.
    #     Извлекаем имя из profile и качаем с maven Forge по тому же пути,
    #     что кладёт официальный installer.
    args = profile.get("arguments", {})
    jvm_args = args.get("jvm", []) if isinstance(args, dict) else []
    main_id = None
    for a in jvm_args:
        if isinstance(a, str) and a.startswith("${") and "client.jar" not in a:
            continue
        if isinstance(a, str) and re.match(r"fmlonly-client-", a):
            main_id = a
            break
    if main_id is None:   # аргументы могут быть {"rules":..., "value":...}
        for a in jvm_args:
            val = a.get("value") if isinstance(a, dict) else a
            vals = val if isinstance(val, list) else [val]
            for x in vals:
                if isinstance(x, str) and x.startswith("fmlonly-client-"):
                    main_id = x
    if main_id:
        rel = f"net/minecraftforge/forge/{main_id}/{main_id}.jar"
        dest = game_dir / "libraries" / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            if download_file([("Maven Forge", MAVEN_FORGE + rel)],
                             dest, "Forge maven-main jar", min_size_mb=0, silent=True):
                ok("maven-main jar скачан")
            else:
                _autofix_remove(dest)

    # 3. Universal jar -> versions/<id>/<id>.jar (launchedVersionJar в profile.json)
    art_path = None
    for l in libs:
        nm = l.get("name", "")
        if nm.startswith("net.minecraftforge:forge:") and nm.endswith(":" + long_id):
            art_path = l.get("downloads", {}).get("artifact", {}).get("path")
            break
    dest_jar = vdir / f"{version_id}.jar"
    if not dest_jar.exists():
        rel = art_path or f"net/minecraftforge/forge/{long_id}/forge-{long_id}-universal.jar"
        got = download_file([("Maven Forge", MAVEN_FORGE + rel)] + forge_universal_urls(mc_version, forge_build),
                           dest_jar, "Forge universal", min_size_mb=1)
        ok("Forge universal jar скачан") if got else warn("Universal jar не скачан — лаунчер докачает сам")
    lp = game_dir / "launcher_profiles.json"
    if not lp.exists():
        lp.write_text(json.dumps({"profiles": {}, "clientToken": ""}, indent=2), encoding="utf-8")
    return version_id


# NOTE: проекта «luxium» на Modrinth нет (API отдаёт 404) — константа
# оставлена только как фолбек на случай появления зеркала мода там.
# Основной источник после локального JAR из репозитория — CurseForge API.
LUXIUM_FILES_API = "https://api.modrinth.com/v2/project/luxium/files"


def _luxium_fallback_urls():
    """Запасные прямые ссылки на Luxium .jar (CurseForge CDN, file 5208596).

    ВАЖНО: edge.forgecdn.net отдаёт 403/302 (редирект на подписанный URL),
    а urllib не следует 302 автоматически — эти зеркала работают лишь в том
    случае, если скачивание ведётся через curl-бэкенд download_file().
    Надёжный сетевой путь — _luxium_curseforge_api_url() (нужен ключ).
    """
    return [(base.split("//")[-1].split(".")[0],
             f"{base}/files/5208/596/Luxia-Shaders-forge-1.20.1-1.4.1.jar")
            for base in CF_EDGE_BASES]


def _luxium_curseforge_api_url():
    """Прямой download-URL с api.curseforge.com для файла Luxium 5208596.

    Возвращает None без API-ключа (CurseForge официально требует его);
    задаётся переменной окружения WI_CF_API_KEY.
    """
    key = os.environ.get("WI_CF_API_KEY", "")
    if not key:
        return None
    return (f"https://api.curseforge.com/v1/mods/864217/files/5208596/download-url"
            f"?token={key}")


def _luxium_curseforge_direct():
    """[(label, url)] по CurseForge API: JSON {data: url} -> реальная ссылка."""
    out = []
    api = _luxium_curseforge_api_url()
    if not api:
        return out
    try:
        data = _http_get_json(api, timeout=30)
        url = data.get("data") if isinstance(data, dict) else None
        if isinstance(url, str) and url.startswith("http"):
            out.append(("CurseForge API", url))
    except Exception as e:
        debug.dbg_exc(e, "luxium/curseforge_api")
    return out


def _luxium_local_jars():
    """Локальные .jar Luxium: корень репозитория (git clone) + кэш WINE_DIR.

    Порог размера — 5 МБ: релизные сборки Luxium весят ~7-20 МБ, а меньше —
    почти наверняка битая загрузка или заглушка.
    """
    found = []
    for base in (REPO_ROOT, WINE_DIR):
        try:
            if not base.is_dir():
                continue
            for p in sorted(base.glob("*uxi*.jar"),
                            key=lambda p: p.stat().st_mtime, reverse=True):
                # имя с подстрокой "luxi"/"luxia" (Luxium / Luxia-Shaders)
                low = p.name.lower()
                if ("luxi" not in low and "luxia" not in low) \
                        or p.stat().st_size <= 5 * 1024 * 1024 \
                        or not zipfile.is_zipfile(p):
                    continue
                if not any(p.name == q.name for q in found):
                    found.append(p)
        except Exception as e:
            debug.dbg_exc(e, "luxium/local")
    return found


def _luxium_github_fallbacks():
    """Luxium JAR через наш GitHub-релиз (тот же ассет, что и mrpack).

    Официальный источник Luxium — CurseForge, который в РФ работает через
    раз; поэтому держим зеркало: dual-loader JAR 2.8.4 прикреплён к нашему
    релизу `fo-5.2.8-fixed` (Godlike-Bagheera/wine-installer), скачивается
    через gh-прокси (fo_mrpack_mirrors). Имя переопределяется через
    WI_LUXIUM_JAR_NAME. Возвращает [(label, url)] (может быть пустым).
    """
    base = f"https://github.com/{FO_MRPACK_OWNER}/{FO_MRPACK_REPO}/releases/"
    tag = (FO_1201_PIN_TAG or "").strip() or "fo-5.2.8-fixed"
    jar_name = os.environ.get("WI_LUXIUM_JAR_NAME", "").strip()
    if not jar_name:
        # Точное имя ассета берём из GitHub API релиза: файлы в релизе могут
        # называться через точки («Luxium.Let.there.be.light-...forge-fabric.
        # jar»), а локальные копии Modrinth-экспорта — через пробелы; типовое
        # имя-догадка дало бы 404 на всех зеркалах. API недоступен — сначала
        # имя локального JAR, затем типовая догадка.
        try:
            rel = _github_get_json(
                f"https://api.github.com/repos/{FO_MRPACK_OWNER}/{FO_MRPACK_REPO}"
                f"/releases/tags/{urllib.parse.quote(tag)}", timeout=30)
            for a in rel.get("assets", []):
                n = a.get("name", "")
                if n.endswith(".jar") and ("luxi" in n.lower()):
                    jar_name = n
                    break
        except Exception as e:
            debug.dbg_exc(e, "luxium/github_name_api")
    if not jar_name:
        local = _luxium_local_jars()
        jar_name = local[0].name if local else \
            "Luxium Let there be light-2.8.4-pre-alpha forge-fabric.jar"
    return fo_mrpack_mirrors(base + f"download/{tag}/{urllib.parse.quote(jar_name)}")


def install_luxium_fabric_mod(game_dir):
    """Luxium 2.8.4+ (dual-loader Forge/Fabric JAR) -> mods/ для FO.

    Порядок: 1) уже стоит в mods/; 2) локальный JAR с fabric.mod.json внутри
    (корень репо / кэш — офлайн); 3) скачивание с нашего GitHub-релиза через
    gh-прокси (CurseForge в РФ работает через раз — не единственный путь).
    Старый forge-only JAR для Fabric не подходит — предупреждаем и предлагаем
    `ofps`. Возвращает True/False.
    """
    mods_dir = Path(game_dir) / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    for f in mods_dir.glob("*.jar"):
        if "luxium" in f.name.lower() or "luxia" in f.name.lower():
            ok(f"Luxium уже установлен: {f.name}")
            return True
    saw_forge_only = False
    for cand in _luxium_local_jars():
        try:
            with zipfile.ZipFile(cand) as z:
                has_fabric = "fabric.mod.json" in z.namelist()
        except Exception as e:
            debug.dbg_exc(e, "luxium/fabric_check")
            has_fabric = False
        if has_fabric:
            shutil.copy2(cand, mods_dir / cand.name)
            ok(f"Luxium (Fabric) из локального файла: {cand.name}")
            return True
        saw_forge_only = True
    # локального dual-JAR нет (или он forge-only) — пробуем скачать с нашего
    # GitHub-релиза (через gh-прокси), а не только полагаться на CurseForge
    WINE_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in _luxium_github_fallbacks():
        dest = WINE_DIR / url.rsplit("/", 1)[-1]
        _autofix_clean_file(dest, min_bytes=10 * 1024)
        if not (dest.exists() or download_file([(name, url)], dest,
                                               f"Luxium ({name})", min_size_mb=0)):
            continue
        try:
            with zipfile.ZipFile(dest) as z:
                has_fabric = "fabric.mod.json" in z.namelist()
        except Exception as e:
            debug.dbg_exc(e, "luxium/github_dl")
            continue
        if has_fabric:
            shutil.copy2(dest, mods_dir / dest.name)
            ok(f"Luxium (Fabric) скачан с GitHub-зеркала: {dest.name}")
            return True
    if saw_forge_only:
        warn("Локальный Luxium JAR без fabric.mod.json (forge-only) — "
             "для Fabulously Optimized он не подойдёт; используй команду `ofps` "
             "(Forge 1.20.1) или обнови файл Luxium*.jar в корне репозитория")
    else:
        warn("Luxium (Fabric) не найден локально и не скачался с GitHub-зеркала — "
             f"положи dual-loader Luxium*.jar в {REPO_ROOT} или {WINE_DIR} "
             "и повтори `fo`")
    return False


def install_sodium_for_luxium(game_dir):
    """Sodium — обязательная зависимость Luxium на Fabric (fabric.mod.json).

    FO v5.2.8 уже несёт Sodium, поэтому при неудаче только предупреждаем
    (в отличие от Embeddium в `ofps`, где без зависимости краш). Порядок:
      1) любой *sodium*.jar уже в mods/;
      2) локальный jar в корне репо/кэше (офлайн);
      3) Modrinth API: версия из depends Luxium (точно 0.5.13) + MC 1.20.1
         + fabric, скачивание по зеркалам modrinth_file_mirrors().
    Возвращает True/False.
    """
    mods_dir = Path(game_dir) / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    for f in mods_dir.glob("*.jar"):
        if "sodium" in f.name.lower():
            ok(f"Sodium уже установлен: {f.name}")
            return True
    for cand in _local_mod_jars("sodium"):
        shutil.copy2(cand, mods_dir / cand.name)
        ok(f"Sodium из локального файла: {cand.name}")
        return True
    # требуемая версия — из fabric.mod.json Luxium (дефолт — пин константы)
    want = luxium_fabric_dep("sodium") or SODIUM_PINNED_VERSION
    want_core = re.sub(r"^[^\d]*", "", want).split("-")[0] or SODIUM_PINNED_VERSION
    try:
        import urllib.parse as _up
        q = _up.urlencode({"game_versions": json.dumps([SODIUM_MC_VERSION]),
                           "loaders": json.dumps(["fabric"]), "limit": 100})
        versions = _http_get_json(f"{SODIUM_VERSIONS_API}?{q}", timeout=30)
    except Exception as e:
        debug.dbg_exc(e, "sodium/modrinth_api")
        versions = []
    def _core(v):
        m = re.search(r"\d+(?:\.\d+)*", str(v.get("version_number", "")))
        return m.group(0) if m else ""
    exact = [v for v in versions if isinstance(v, dict) and _core(v) == want_core]
    pool = exact or sorted((v for v in versions if isinstance(v, dict)),
                           key=lambda v: _ver_tuple(_core(v)), reverse=True)
    for v in pool:
        if (v.get("status") or "").lower() not in ("release", "listed"):
            continue
        files = v.get("files") or []
        if not files:
            continue
        fv = files[0]
        url = fv.get("url", "")
        filename = fv.get("filename") or f"sodium-{v.get('version_number','')}.jar"
        sha1 = (fv.get("hashes") or {}).get("sha1", "")
        dest = WINE_DIR / filename
        mirrors = modrinth_file_mirrors(url)
        if not mirrors:
            continue
        if not (dest.exists() and (not sha1 or verify_sha1(dest, sha1)[0])):
            if dest.exists():
                _autofix_remove(dest)
            if not download_file(mirrors, dest, filename, min_size_mb=0):
                continue
            if sha1 and not verify_sha1(dest, sha1)[0]:
                _autofix_remove(dest)
                continue
        shutil.copy2(dest, mods_dir / filename)
        ok(f"Sodium установлен в mods/: {filename}")
        return True
    warn("Sodium (зависимость Luxium для Fabric) скачать не удалось — "
         f"положи *sodium*.jar в {REPO_ROOT} рядом с wine.py и повтори `fo`; "
         f"нужна версия {want_core} для MC {SODIUM_MC_VERSION} "
         "(https://modrinth.com/mod/sodium)")
    return False


def install_luxium_mod(game_dir):
    """Luxium («Let there be light», шейдер-рендер для Forge 1.20.1) -> mods/.

    Порядок: 1) .jar рядом с кодом (корень репозитория после `git clone`);
    2) локальный кэш WINE_DIR; 3) Modrinth API; 4) зеркала CurseForge CDN.
    SHA-1 проверяется, если есть.
    """
    mods_dir = Path(game_dir) / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    # уже стоит?
    for f in mods_dir.glob("*.jar"):
        if "luxium" in f.name.lower() or "luxia" in f.name.lower():
            ok(f"Luxium уже установлен: {f.name}")
            return True

    # 1-2. Локальные файлы: сначала из клонированного репо, затем из кэша
    for cand in _luxium_local_jars():
        shutil.copy2(cand, mods_dir / cand.name)
        ok(f"Luxium из локального файла: {cand.name}")
        return True

    # 2. Modrinth: файлы проекта luxium, фильтр по side client + MC 1.20.1
    try:
        files = _http_get_json(LUXIUM_FILES_API + "?limit=100", timeout=30)
    except Exception as e:
        debug.dbg_exc(e, "luxium/modrinth")
        files = []
    candidates = []
    for fv in files if isinstance(files, list) else []:
        sides = fv.get("loads") or []
        if "client" not in sides:
            continue
        gvs = fv.get("game_versions") or []
        # Luxium привязан к сборкам Forge 47.x (= MC 1.20.1): фильтруем по side-номеру
        if "1.20.1" not in gvs and str(LUXIUM_MC_SIDE) not in "".join(map(str, gvs)):
            continue
        candidates.append(fv)
    for fv in candidates:
        url = fv.get("url")
        filename = fv.get("filename", "luxium.jar")
        sha1 = (fv.get("hashes") or {}).get("sha1", "")
        dest = WINE_DIR / filename
        mirrors = [("Modrinth", url)] + _luxium_fallback_urls()
        if not (dest.exists() and (not sha1 or verify_sha1(dest, sha1)[0])):
            _autofix_remove(dest) if dest.exists() else None
            if not download_file(mirrors, dest, filename, min_size_mb=0):
                continue
            if sha1 and not verify_sha1(dest, sha1)[0]:
                _autofix_remove(dest)
                continue
        shutil.copy2(dest, mods_dir / filename)
        ok(f"Luxium установлен в mods/: {filename}")
        return True

    # 3. CurseForge: сначала API (нужен WI_CF_API_KEY), затем CDN-зеркала
    cf_dest = WINE_DIR / "Luxia-Shaders-forge-1.20.1-1.4.1.jar"
    for name, url in _luxium_curseforge_direct() + _luxium_fallback_urls():
        if download_file([(name, url)], cf_dest, "Luxium (CurseForge)", min_size_mb=0) \
                and zipfile.is_zipfile(cf_dest):
            shutil.copy2(cf_dest, mods_dir / cf_dest.name)
            ok(f"Luxium установлен в mods/: {cf_dest.name}")
            return True
    warn("Luxium скачать не удалось — положи luxium_*.jar в "
         f"{WINE_DIR} или в корень репозитория и повтори (файл можно взять "
         "на https://www.curseforge.com/minecraft/mc-mods/luxium/files)")
    return False


def _local_mod_jars(sub, bases=None, min_kb=200):
    """Локальные .jar мода ЛЮБОГО имени: подстрока `sub` в имени файла.

    Ищем в корне репозитория (git clone, рядом с wine.py) и кэше WINE_DIR.
    Порог 200 КБ + проверка zip-магии отсекают HTML-заглушки и битые качалки.
    """
    found = []
    for base in (bases or (REPO_ROOT, WINE_DIR)):
        try:
            if not base.is_dir():
                continue
            for p in sorted(base.glob("*.jar"),
                            key=lambda p: p.stat().st_mtime, reverse=True):
                low = p.name.lower()
                if sub not in low or p.stat().st_size < min_kb * 1024 \
                        or not zipfile.is_zipfile(p):
                    continue
                if not any(p.name == q.name for q in found):
                    found.append(p)
        except Exception as e:
            debug.dbg_exc(e, "local_mod_jars")
    return found


def install_embeddium_mod(game_dir):
    """Embeddium (обязательная зависимость Luxium, mods.toml mandatory=true) -> mods/.

    Без него Luxium крашит игру на старте, а Modrinth CDN (cdn.modrinth.com)
    в РФ может блокироваться — поэтому порядок источников такой:
      1) уже стоит в mods/;
      2) локальный jar ЛЮБОГО имени (embeddium-0.3.31+mc1.20.1.jar,
         Embeddium.jar, ...) в корне репо рядом с wine.py или в кэше WINE_DIR
         — офлайн, без сети;
      3) Modrinth API: версия фильтруется по versionRange из mods.toml
         Luxium (embeddium_version_range()) + game_versions 1.20.1 + sha1;
         скачивание — прямой URL cdn.modrinth.com (рабочих публичных зеркал
         CDN не существует, см. MRCDN_PROXIES в config.py).
    Локальный jar из корня репозитория дополнительно сверяется с
    EMBEDDIUM_SHA1_PIN — закоммиченный файл нельзя подменить молча.
    При неудаче — честный False + подсказка, куда положить jar.
    """
    mods_dir = Path(game_dir) / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    # уже установлен?
    for f in mods_dir.glob("*.jar"):
        if "embeddium" in f.name.lower():
            ok(f"Embeddium уже установлен: {f.name}")
            return True

    # 1. Локальный jar ЛЮБОГО имени — полный офлайн (главный путь для РФ).
    #    Файл из корня репозитория (embeddium-*-mc1.20.1.jar закоммичен там)
    #    валидируется по EMBEDDIUM_SHA1_PIN; битая/чужая копия из REPO_ROOT
    #    отвергается с понятным warn, и установка продолжается через сеть.
    for cand in _local_mod_jars("embeddium"):
        if cand.parent == REPO_ROOT and EMBEDDIUM_SHA1_PIN \
                and not verify_sha1(cand, EMBEDDIUM_SHA1_PIN)[0]:
            warn(f"Локальный {cand.name} не совпадает с ожидаемым SHA-1 "
                 "(битая копия из git или другая версия) — пробую скачать")
            continue
        shutil.copy2(cand, mods_dir / cand.name)
        ok(f"Embeddium из локального файла: {cand.name}")
        return True

    # 1b. Наш GitHub-релиз через gh-прокси: embeddium закоммичен в релиз
    # fo-5.2.8-fixed — надёжный путь для РФ, когда cdn.modrinth.com блокируется,
    # а локального файла в git уже нет (см. .gitignore). Валидация по SHA1-пин.
    if EMBEDDIUM_SHA1_PIN:
        WINE_DIR.mkdir(parents=True, exist_ok=True)
        gh_dest = WINE_DIR / "embeddium-0.3.31+mc1.20.1.jar"
        _autofix_clean_file(gh_dest, min_bytes=10 * 1024)
        for name, url in fo_mrpack_mirrors(
                f"https://github.com/{FO_MRPACK_OWNER}/{FO_MRPACK_REPO}/releases/"
                f"download/{FO_1201_PIN_TAG or 'fo-5.2.8-fixed'}/"
                "embeddium-0.3.31%2Bmc1.20.1.jar"):
            if not (gh_dest.exists() or download_file([(name, url)], gh_dest,
                                                      f"Embeddium ({name})",
                                                      min_size_mb=0)):
                continue
            if verify_sha1(gh_dest, EMBEDDIUM_SHA1_PIN)[0]:
                shutil.copy2(gh_dest, mods_dir / gh_dest.name)
                ok(f"Embeddium с GitHub-зеркала: {gh_dest.name}")
                return True
            warn(f"Локальный {gh_dest.name} не совпадает с ожидаемым SHA-1 — "
                 "пробую скачать")
            _autofix_remove(gh_dest)
            break

    # 2. Modrinth API (сам api.modrinth.com обычно доступен даже когда
    #    cdn.modrinth.com блокируется — тогда сюда не доходит, см. шаг 1)
    lo, hi = embeddium_version_range()
    versions = []
    try:
        versions = _http_get_json(EMBEDDIUM_VERSIONS_API + "?limit=100",
                                  timeout=30)
    except Exception as e:
        debug.dbg_exc(e, "embeddium/modrinth_api")
    candidates = []
    for v_ in versions if isinstance(versions, list) else []:
        # status: "release" | "beta" | "alpha"; listed = обычные релизы (проверено
        # живым API: у embeddium 0.3.31+mc1.20.1 status="listed")
        if v_.get("status") not in (None, "release", "listed"):
            continue
        gvs = v_.get("game_versions") or []
        if EMBEDDIUM_MC_VERSION not in gvs:
            continue
        sides = v_.get("loaders") or []
        if "forge" not in sides:
            continue
        env = v_.get("env") or {}
        if env.get("client") == "unsupported":
            continue
        ver = v_.get("version_number", "")
        vt = _ver_tuple(ver)
        if vt < _ver_tuple(lo):
            continue
        if hi and vt >= _ver_tuple(hi):
            continue
        # берём client-файл (.jar), не .mrpack
        f = next((x for x in (v_.get("files") or [])
                  if (x.get("filename") or "").endswith(".jar")
                  and not x.get("filename", "").endswith("-slim.jar")), None)
        if f is None:
            continue
        candidates.append((v_, f))
    for v_, fv in candidates:   # API отдаёт свежие первыми
        url = fv.get("url")
        filename = fv.get("filename", "embeddium.jar")
        sha1 = (fv.get("hashes") or {}).get("sha1", "")
        dest = WINE_DIR / filename
        mirrors = modrinth_file_mirrors(url)
        if not mirrors:
            continue
        if dest.exists() and sha1 and not verify_sha1(dest, sha1)[0]:
            _autofix_remove(dest)
        elif dest.exists() and not zipfile.is_zipfile(dest):
            # битый кэш без sha1 (например, HTML-заглушка прокси) — иначе
            # download_file сочтёт его «уже скачанным» и цикл не сойдётся
            _autofix_remove(dest)
        if not (dest.exists() or download_file(mirrors, dest,
                                               f"Embeddium ({filename})",
                                               min_size_mb=0)):
            continue
        if not zipfile.is_zipfile(dest):
            _autofix_remove(dest)
            continue
        if sha1 and not verify_sha1(dest, sha1)[0]:
            _autofix_remove(dest)
            continue
        shutil.copy2(dest, mods_dir / filename)
        ok(f"Embeddium установлен в mods/: {filename}")
        return True

    warn("Embeddium скачать не удалось (зависимость Luxium обязательна!) — "
         f"положи embeddium*.jar (любого имени) в {REPO_ROOT} рядом с wine.py "
         f"или в {WINE_DIR} и повтори `ofps`. Файл: "
         "https://modrinth.com/mod/embeddium/version/"
         f"{lo}+mc{EMBEDDIUM_MC_VERSION}")
    return False


# Pin: Modrinth version id релиза Optimized fps для Forge 1.20.1 (v2.4.10) —
# ровно та сборка, с которой протестирован Luxium 2.8.0. Свежие релизы мода
# переехали на NeoForge (MC 26.x), где Luxium не работает, поэтому сначала
# пробуем этот id, и только потом ищем подходящий Forge-релиз по API.
OFPS_FORGE_VERSION_ID = os.environ.get("WI_OFPS_VERSION_ID", "bf2aWUzR")


def _ofps_release_from_version(v):
    """Версия Modrinth -> dict релиза ofps (или None, если .mrpack нет)."""
    mr = next((f for f in v.get("files", [])
               if f.get("filename", "").endswith(".mrpack")), None)
    if not mr:
        return None
    return {"version": v.get("version_number", ""),
            "mrpack_url": mr["url"],
            "mrpack_name": mr["filename"],
            "size": mr.get("filesize", 0)}


def _pick_ofps_release():
    """Релиз Optimized fps именно для Forge 1.20.1 (не NeoForge 26.x!).

    Luxium существует только под Forge 1.20.1, поэтому гонка за «самым
    свежим» релизом модпака ломает установку: актуальные версии FO-modpack
    ставятся на NeoForge/MC 26. Порядок: захардкоженный pinned id ->
    поиск самого свежего release c forge+1.20.1 в зависимостях.
    """
    # 1. Pinned-релиз (тот самый bf2aWUzR со страницы модпака)
    try:
        v = _http_get_json(
            f"https://api.modrinth.com/v2/version/{OFPS_FORGE_VERSION_ID}",
            timeout=30)
        if isinstance(v, dict) and v.get("files"):
            rel = _ofps_release_from_version(v)
            if rel:
                return rel
    except Exception as e:
        debug.dbg_exc(e, "ofps/pinned")
    # 2. Фолбек: среди релизов ищем тот, что на Forge 1.20.x
    try:
        versions = _http_get_json(OFPS_MODRINTH_API + "?limit=100", timeout=30)
    except Exception as e:
        debug.dbg_exc(e, "ofps/modrinth")
        raise RuntimeError(f"Modrinth API: {e}")
    best = None
    for v in versions if isinstance(versions, list) else []:
        if v.get("version_type") != "release":
            continue
        deps = {d.get("project_id"): d for d in v.get("dependencies", [])}
        # forge-проект на Modrinth = "nUc7rf2x"; neoforge не подходит (Luxium
        # требует именно MinecraftForge 47.x)
        forge_dep = deps.get("nUc7rf2x")
        gvs = v.get("game_versions", [])
        if not forge_dep or not any(g.startswith("1.20") for g in gvs):
            continue
        if _ofps_release_from_version(v) is None:
            continue
        best = v
        break   # API отдаёт свежие первыми — берём первый подходящий
    if best is None:
        raise RuntimeError("На Modrinth не найдено релиза Optimized fps для Forge 1.20.1")
    return _ofps_release_from_version(best)


def _ofps_skip_entry(entry):
    """True, если файл .mrpack не ставим: серверный или Oculus (его заменяет Luxium)."""
    env = entry.get("env", {})
    if env.get("client") == "unsupported":
        return True
    path = entry.get("path", "").lower()
    if not path.startswith("mods/"):
        return False
    fname = path.rsplit("/", 1)[-1]
    return fname.startswith("oculus") or "-oculus" in fname


def unpack_mrpack_forge(mrpack_path, game_dir):
    """Распаковка .mrpack в режиме Forge: без установки Fabric, с удалением Oculus.

    Возвращает (mc_version, forge_build) или (None, None).
    """
    try:
        with zipfile.ZipFile(mrpack_path, "r") as z:
            if "modrinth.index.json" not in z.namelist():
                err("modrinth.index.json не найден")
                return None, None
            manifest = json.loads(z.read("modrinth.index.json").decode("utf-8"))
    except Exception as e:
        debug.dbg_exc(e, "unpack_mrpack_forge/open")
        err(f"Открыть .mrpack: {e}")
        return None, None
    deps = manifest.get("dependencies", {})
    mc_version = deps.get("minecraft")
    forge_build = deps.get("forge")
    if not mc_version:
        err("Не найдена версия Minecraft в манифесте")
        return None, None
    if not forge_build:
        forge_build = _forge_build_for(mc_version)
        if forge_build:
            info(f"Версия Forge из promotions_slim: {forge_build}")
    if not forge_build:
        err("В модпаке нет зависимости forge и promotions_slim недоступен")
        return mc_version, None

    # ── список файлов: пропускаем Oculus (шейдер-мод на Iris — его заменяет Luxium)
    files = [e for e in manifest.get("files", []) if not _ofps_skip_entry(e)]
    skipped = len(manifest.get("files", [])) - len(files)
    info(f"MC {mc_version}, Forge {forge_build}; файлов: {len(files)} "
         f"(пропущено Oculus/серверных: {skipped})")
    game_dir = Path(game_dir)
    downloaded = failed = 0
    for entry in files:
        path = entry.get("path", "")
        dest = game_dir / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        hashes = entry.get("hashes", {})
        sha512 = hashes.get("sha512")
        if dest.exists() and sha512 and verify_sha512(dest, sha512)[0]:
            downloaded += 1
            continue
        urls = entry.get("downloads", [])
        if not urls:
            failed += 1
            continue
        mirrors = [("direct", u) for u in urls]
        if download_file(mirrors, dest, path.split("/")[-1], min_size_mb=0, silent=True):
            if not sha512 or verify_sha512(dest, sha512)[0]:
                downloaded += 1
                continue
            _autofix_remove(dest)
            if download_file(mirrors, dest, path.split("/")[-1], min_size_mb=0, silent=True) \
                    and verify_sha512(dest, sha512)[0]:
                downloaded += 1
                continue
        failed += 1
    ok(f"Модов: {downloaded}, ошибок: {failed}")
    if failed and _ask_retry("скачать недостающие моды"):
        again = unpack_mod_files({k: v for k, v in manifest.items()}, game_dir)
        downloaded += again
        failed = max(failed - again, 0)
        ok(f"После автофикса: модов {downloaded}, ошибок {failed}")

    # ── overrides (config, optionsshaders и т.п.)
    try:
        with zipfile.ZipFile(mrpack_path, "r") as z:
            for prefix in ["overrides/", "client-overrides/"]:
                for name in z.namelist():
                    if name.startswith(prefix) and not name.endswith("/"):
                        rel = name[len(prefix):]
                        if not rel:
                            continue
                        target = game_dir / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(name) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
        ok("Overrides распакованы")
    except Exception as e:
        debug.dbg_exc(e, "unpack_mrpack_forge/overrides")
        warn(f"Overrides: {e}")

    # ── на всякий случай удаляем Oculus, если он лежал в папке mods ранее
    removed = _remove_oculus_from_mods(game_dir)
    if removed:
        ok(f"Удалено старых копий Oculus: {removed}")
    return mc_version, forge_build


def _remove_oculus_from_mods(game_dir):
    n = 0
    for f in (Path(game_dir) / "mods").glob("*.jar"):
        name = f.name.lower()
        if name.startswith("oculus") or "-oculus" in name:
            _autofix_remove(f)
            n += 1
    return n


def setup_optimized_fps():
    info(f"{BOLD}Скачивание Optimized fps (Forge-модпак){RESET}")
    hint("Запасной вариант для Luxium; приоритетный модпак — `fo` "
         "(Fabulously Optimized v5.2.8, Fabric 1.20.1)")
    hint("Без мода Oculus — шейдеры рисует Luxium (Forge 1.20.1)")
    try:
        rel = _pick_ofps_release()
    except Exception as e:
        debug.dbg_exc(e, "setup_ofps")
        err(f"{e}")
        return False
    info(f"Релиз: {rel['version']} ({rel['mrpack_name']}, {rel['size'] // 1024} КБ)")

    found = choose_minecraft_dir(auto=True)
    if found is None:
        warn("Папка .minecraft (или game у Legacy) не найдена на системе.")
        print()
        hint("1. Запусти любой лаунчер (legacy / Prism) один раз — он создаст папку игры")
        hint("2. Укажи путь вручную")
        try:
            manual = input(f"{YELLOW}Путь к .minecraft (Enter — отказаться): {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            manual = ""
        if manual and Path(manual).expanduser().is_dir():
            found = Path(manual).expanduser()
        elif manual:
            err("Такой папки нет")
            return False
        else:
            hint("Повтори `ofps` после запуска лаунчера.")
            return True
    game_dir = Path(found)
    (game_dir / "versions").mkdir(parents=True, exist_ok=True)
    ok(f"Директория игры: {game_dir}")

    dest = WINE_DIR / rel["mrpack_name"]
    _autofix_clean_file(dest, min_bytes=10 * 1024)
    if dest.exists() and not _validate_mrpack(dest):
        warn("Кэш .mrpack повреждён — автофикс: перекачиваю")
        _autofix_remove(dest)
    mirrors = [("Modrinth", rel["mrpack_url"])]
    if not (dest.exists() or download_file(mirrors, dest, "Optimized fps (.mrpack)", min_size_mb=0)):
        err("Скачать .mrpack не удалось")
        hint("Проверь сеть/VPN или скачай вручную: https://modrinth.com/modpack/optimized-fps/versions")
        return False
    if not _validate_mrpack(dest):
        _autofix_remove(dest)
        err("Скачанный файл не похож на валидный .mrpack")
        return False
    ok(f".mrpack скачан и проверен: {dest}")

    mc_version, forge_build = unpack_mrpack_forge(dest, game_dir)
    if not mc_version or not forge_build:
        return False
    version_id = install_forge_profile(game_dir, mc_version, forge_build)
    if not version_id:
        return False
    if not install_luxium_mod(game_dir):
        err("Luxium не установлен — шейдер-рендер работать не будет")
        return False
    # Embeddium — mandatory-зависимость Luxium (mods.toml: versionRange
    # "[0.3.31,0.4)", mandatory=true): без неё Forge крашит игру на старте,
    # поэтому ставим строго после Luxium и при неудаче прерываем установку.
    if not install_embeddium_mod(game_dir):
        err("Embeddium не установлен — Luxium без него крашит игру")
        hint("Офлайн-фикс: положи embeddium-0.3.31+mc1.20.1.jar в корень "
             "репозитория рядом с wine.py и повтори `ofps`")
        return False
    info("Доводим установку версии до конца (vanilla jar + профиль)...")
    finish_version_install(game_dir, mc_version, version_id)
    ok(f"Готово! Версия {version_id} появится в лаунчере")
    hint("Шейдеры: команда `shaders` (Luxium читает обычные shaderpacks/*.zip)")
    return True


# ═══════════════════════════════════════════════════════════════════
#  OPTIFINE
# ═══════════════════════════════════════════════════════════════════

def _optifine_versions_from_html(html):
    """[(mc_ver, opti_ver), ...] в порядке страницы (свежие сверху)."""
    out = []
    seen = set()
    for m in re.finditer(r'OptiFine_(\d+\.\d+(?:\.\d+)?)_HD_U_([A-Z]\w+)\.jar', html):
        key = (m.group(1), m.group(2))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def setup_optifine():
    info(f"{BOLD}Скачивание OptiFine{RESET}")
    try:
        with _http_get(OPTIFINE_PAGE, timeout=30,
                       headers={"User-Agent": "Mozilla/5.0"}) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        debug.dbg_exc(e, "setup_optifine/page")
        err(f"Страница: {e}")
        return False
    versions = _optifine_versions_from_html(html)
    if not versions:
        err("На странице OptiFine не найдено ни одной версии")
        hint("Возможно, сайт отдал заглушку/антибот. Попробуй позже.")
        return False

    # ── Куда ставить: ищем реальные .minecraft / game на системе ──
    found = choose_minecraft_dir(auto=True)
    if found is None:
        warn("Папка .minecraft (или game у Legacy) не найдена на системе.")
        try:
            manual = input(f"{YELLOW}Путь к .minecraft (Enter — отказаться): {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            manual = ""
        if manual and Path(manual).expanduser().is_dir():
            found = Path(manual).expanduser()
        else:
            if manual:
                err("Такой папки нет")
            hint("Запусти сначала любой лаунчер — он создаст папку игры.")
            return False
    game_dir = Path(found)
    ok(f"Директория игры: {game_dir}")

    # ── Подбор версии под уже установленные в games/versions ──
    installed = []
    vdir = game_dir / "versions"
    if vdir.is_dir():
        installed = sorted((d.name for d in vdir.iterdir() if d.is_dir()), reverse=True)
    mc_in_installed = []
    for name in installed:
        for m in re.finditer(r"(\d+\.\d+(?:\.\d+)?)", name):
            if m.group(1) not in mc_in_installed:
                mc_in_installed.append(m.group(1))
    pick = None
    for mc_ver, opti_ver in versions:              # свежие релизы OptiFine сверху
        if mc_ver in mc_in_installed:
            pick = (mc_ver, opti_ver)
            break
    if pick:
        info(f"Подобрал версию под установленную в игре: MC {pick[0]}")
    else:
        pick = versions[0]
        if installed:
            warn("Точного совпадения с версиями в games/versions нет — беру свежий OptiFine")
        info(f"MC {pick[0]}, OptiFine HD U {pick[1]}")
        try:
            ans = input(f"{YELLOW}Сменить версию? [y/N]: {RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            ans = "n"
        if ans in ("y", "д", "да"):
            print("Доступные (первые 15):")
            shown = versions[:15]
            for i, (mv, ov) in enumerate(shown, 1):
                print(f"  {CYAN}{i}{RESET}) MC {mv}, HD U {ov}")
            try:
                c = int(input("Номер: ").strip()) - 1
                if 0 <= c < len(shown):
                    pick = shown[c]
            except (ValueError, IndexError, KeyboardInterrupt, EOFError):
                pass
    mc_ver, opti_ver = pick
    fname = f"OptiFine_{mc_ver}_HD_U_{opti_ver}.jar"
    url = f"http://optifine.net/adloadx?f={fname}"
    dest = WINE_DIR / fname
    # автофикс: битый кэш OptiFine (HTML вместо JAR) чистим заранее
    if _autofix_is_html_jar(dest):
        warn(f"Кэш {fname} — HTML-заглушка, автофикс: перекачиваю")
        _autofix_remove(dest)
    if not dest.exists():
        if not download_file([("direct", url)], dest, "OptiFine", min_size_mb=0):
            err("Не удалось скачать")
            return False
    # скачанный "jar" с adloadx иногда оказывается HTML-заглушкой
    head = b""
    try:
        with open(dest, "rb") as f:
            head = f.read(4)
    except Exception as e:
        debug.dbg_exc(e, "minecraft")
    if head != b"PK\x03\x04":
        err("Скачался не JAR (похоже на страницу-заглушку optifine.net)")
        hint("Открой браузером https://optifine.net/downloads, скачай вручную,")
        hint(f"положи файл в {WINE_DIR} и повтори команду — файл уже будет там.")
        _autofix_remove(dest)
        return False
    ok(f"Скачано: {dest}")
    java_bin, java_home, java_status = find_java()
    if not java_home:
        warn(f"Java: {java_status}")
        hint("Для OptiFine installer нужна Java с GUI (AWT).")
        try:
            answer = input(f"{MAGENTA}Скачать портативную JDK 17? [Y/n]: {RESET}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            answer = "n"
        if answer in ("", "y", "yes", "д", "да"):
            if not install_portable_java():
                return False
            java_bin, java_home, java_status = find_java()
        if not java_home:
            err("Java с GUI так и не найдена")
            return False
    if java_bin is None:
        err("Java не найдена")
        return False
    ok(f"Java: {java_home or java_bin} ({java_status})")
    game_path = str(game_dir)
    clipboard_ok = copy_to_clipboard(game_path)
    info("Запускаю установщик OptiFine...")
    print()
    print(f"{BOLD}В поле 'Folder' вставь путь:{RESET}")
    print(f"  {CYAN}{game_path}{RESET}")
    if clipboard_ok:
        ok("Путь уже в буфере обмена (Ctrl+V в поле Folder)")
    else:
        hint("Выдели и скопируй вручную")
    print()
    try:
        subprocess.run([java_bin, "-jar", str(dest)], check=False)
    except FileNotFoundError:
        err("Не запустить: битый путь к Java. Установи заново: install-java")
        return False
    except Exception as e:
        debug.dbg_exc(e, "setup_optifine/run")
        err(f"Не запустить: {e}")
        return False

    # ── Проверяем результат и добираем недостающее ──
    of_vdir = game_dir / "versions" / f"OptiFine_{mc_ver}"
    of_json = game_dir / "versions" / f"OptiFine_{mc_ver}_HD_U_{opti_ver}" / \
        f"OptiFine_{mc_ver}_HD_U_{opti_ver}.json"
    created = [p for p in (of_vdir, of_json) if p.exists()]
    # установщик мог создать профиль с другим именем — ищем любой новый OptiFine json
    if not created:
        cand = list((game_dir / "versions").glob("OptiFine*/*.json")) if (game_dir / "versions").is_dir() else []
        if cand:
            created = cand
    if created:
        prof_path = Path(created[-1]) if isinstance(created[-1], Path) else None
        ok(f"OptiFine установлен: {prof_path.parent.name if prof_path else 'профиль создан'}")
        version_id = prof_path.stem if prof_path else f"OptiFine_{mc_ver}_HD_U_{opti_ver}"
        finish_version_install(game_dir, mc_ver, version_id)
        hint("Готово! Выбери эту версию в лаунчере")
    else:
        warn("Похоже, установка в окне OptiFine не завершилась (профиль не создан).")
        if _ask_retry("запустить установщик OptiFine ещё раз"):
            try:
                subprocess.run([java_bin, "-jar", str(dest)], check=False)
            except Exception as e:
                debug.dbg_exc(e, "setup_optifine/retry")
            cand = list((game_dir / "versions").glob("OptiFine*/*.json")) \
                if (game_dir / "versions").is_dir() else []
            if cand:
                version_id = cand[-1].stem
                ok(f"На этот раз профиль создан: {version_id}")
                finish_version_install(game_dir, mc_ver, version_id)
                hint("Готово! Выбери эту версию в лаунчере")
                return True
        hint("Ручной вариант: запусти установщик сам (java -jar ...) и в поле Folder")
        hint("вставь путь выше; кнопка Install должна сказать 'installed successfully'.")
    return True


# ═══════════════════════════════════════════════════════════════════
#  МЕНЮ
# ═══════════════════════════════════════════════════════════════════

def cmd_fo_autofix():
    """Диагностика и автопочинка установленной игры (.minecraft)."""
    info(f"{BOLD}Автофикс Minecraft: диагностика{RESET}")
    found = choose_minecraft_dir(auto=True)
    if found is None:
        err("Папка .minecraft не найдена")
        hint("Запусти лаунчер или укажи путь вручную (пункт «Показать пути»).")
        return False
    game_dir = Path(found)
    ok(f"Проверяю: {game_dir}")
    problems = 0

    # 1) launcher_profiles.json
    lp = game_dir / "launcher_profiles.json"
    if not lp.exists():
        warn("Нет launcher_profiles.json — создаю")
        lp.write_text(json.dumps({"profiles": {}, "clientToken": ""}, indent=2), encoding="utf-8")
        problems += 1
    else:
        try:
            json.loads(lp.read_text(encoding="utf-8"))
        except Exception:
            warn("launcher_profiles.json повреждён — пересоздаю (автофикс)")
            _autofix_remove(lp)
            lp.write_text(json.dumps({"profiles": {}, "clientToken": ""}, indent=2), encoding="utf-8")
            problems += 1

    # 2) пустые/битые профили версий
    vdir = game_dir / "versions"
    if vdir.is_dir():
        for vd in sorted(vdir.iterdir()):
            if not vd.is_dir():
                continue
            pj = vd / f"{vd.name}.json"
            if not pj.exists():
                warn(f"Профиль {vd.name}: нет .json — удаляю пустую папку (автофикс)")
                _autofix_remove(vd)
                problems += 1
                continue
            try:
                json.loads(pj.read_text(encoding="utf-8"))
            except Exception:
                warn(f"Профиль {vd.name}: .json повреждён — удаляю (лаунчер пересоздаёт)")
                _autofix_remove(vd)
                problems += 1

    # 3) битые JAR-файлы в mods/ (HTML-заглушки вместо мода)
    mods = game_dir / "mods"
    if mods.is_dir():
        for jf in sorted(mods.glob("*.jar")):
            if _autofix_is_html_jar(jf):
                warn(f"Мод {jf.name} — HTML-заглушка, удаляю (перекачается через `fo`)")
                _autofix_remove(jf)
                problems += 1
            elif jf.stat().st_size == 0:
                warn(f"Мод {jf.name} пустой, удаляю")
                _autofix_remove(jf)
                problems += 1

    # 4) версии без client.jar -> доводим через finish_version_install
    if vdir.is_dir():
        for vd in sorted(vdir.iterdir()):
            pj = vd / f"{vd.name}.json" if vd.is_dir() else None
            if not pj or not pj.exists():
                continue
            try:
                prof = json.loads(pj.read_text(encoding="utf-8"))
            except Exception:
                continue
            inherits = prof.get("inheritsFrom")
            jar = vd / f"{vd.name}.jar"
            vanilla_jar = game_dir / "versions" / inherits / f"{inherits}.jar" if inherits else None
            if inherits and vanilla_jar and not vanilla_jar.exists() and not jar.exists():
                info(f"Версии {vd.name} не хватает vanilla jar — докачиваю (автофикс)")
                finish_version_install(game_dir, inherits, vd.name)
                problems += 1

    if problems:
        ok(f"Автофикс завершён: исправлено проблем: {problems}")
    else:
        ok("Проблем не найдено — игра в порядке")
    return True


def cmd_minecraft():
    while True:
        print(f"\n{BOLD}═══════ MINECRAFT ═══════{RESET}")
        print(f"  {CYAN}1{RESET}) Prism Launcher (Linux)")
        print(f"  {CYAN}2{RESET}) Legacy Launcher (Portable .jar)")
        print(f"  {CYAN}3{RESET}) Fabulously Optimized v5.2.8 (1.20.1) + Luxium — приоритет")
        print(f"  {CYAN}4{RESET}) OptiFine (скачать + установить)")
        print(f"  {CYAN}5{RESET}) Показать пути")
        print(f"  {CYAN}6{RESET}) Установить Java (для OptiFine)")
        print(f"  {CYAN}7{RESET}) Автофикс игры (проверка + починка ошибок)")
        print(f"  {CYAN}8{RESET}) Миры → Диск D / ← Диск D (RED OS)")
        print(f"  {CYAN}9{RESET}) Установить шейдеры (shaderpacks)")
        print(f"  {CYAN}10{RESET}) Optimized fps (Forge + Luxium, без Oculus) — запасной")
        print(f"  {CYAN}0{RESET}) Назад")
        try:
            choice = input(f"{YELLOW}Выбор: {RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return
        if choice == "1":
            setup_prism()
        elif choice == "2":
            setup_legacy()
        elif choice == "3":
            setup_fabulously_optimized()
        elif choice == "4":
            setup_optifine()
        elif choice == "5":
            from modules.prefix import show_minecraft_paths
            show_minecraft_paths()
        elif choice == "6":
            cmd_install_java()
        elif choice == "7":
            cmd_fo_autofix()
        elif choice == "8":
            from modules.worlds import cmd_worlds_menu
            cmd_worlds_menu()
        elif choice == "9":
            from modules.shaders import cmd_shaders
            cmd_shaders()
        elif choice == "10":
            setup_optimized_fps()
        elif choice == "0":
            return
        else:
            warn("Неверный выбор")
