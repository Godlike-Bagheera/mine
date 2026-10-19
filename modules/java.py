"""Поиск и установка портативной JDK 17."""
import os
import subprocess
from pathlib import Path
from modules import debug, net
from modules.colors import ok, info, warn, err, hint, MAGENTA, BOLD, RESET
from modules.config import JAVA_DIR, JDK_MIRRORS, WINE_DIR
from modules.download import download_file
from modules.platform_utils import IS_WINDOWS


def _check_jdk(jdk_path):
    exe = "java.exe" if IS_WINDOWS else "java"
    java_bin = jdk_path / "bin" / exe
    if not java_bin.exists():
        return False
    # os.access(X_OK) на Windows для .exe всегда True; проверка запуском ниже
    try:
        kw = dict(capture_output=True, text=True, timeout=15)
        if IS_WINDOWS:
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run([str(java_bin), "-version"], **kw)
        out = r.stdout + r.stderr
        return r.returncode == 0 and "version" in out.lower()
    except Exception:
        return False


def find_java():
    """Найти рабочую Java. Возвращает (путь_к_java, home_jdk, статус)."""
    from modules.config import HOME
    exe = "java.exe" if IS_WINDOWS else "java"
    # 1. Наша портативная JDK (mc-portable/java или ~/java)
    if JAVA_DIR.exists():
        for jdk in sorted(JAVA_DIR.glob("jdk-*"), reverse=True):
            if _check_jdk(jdk):
                return str(jdk / "bin" / exe), str(jdk), "OK (портативная)"
    if IS_WINDOWS:
        # 2. Портативные JDK рядом с лаунчером (Legacy portable кладёт jre/)
        for cand in (WINE_DIR / "legacy-portable" / "jre",
                     WINE_DIR / "legacy-portable" / "java"):
            b = cand / "bin" / exe
            if b.exists() and _check_jdk(cand):
                return str(b), str(cand), "OK (portable JRE)"
        # 3. Системные установки (Program Files) + PATH
        import shutil as _sh
        java_path = _sh.which(exe) or _sh.which("java")
        if java_path:
            return java_path, None, "OK (системная)"
        for base in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                     Path(os.environ.get("ProgramFiles(x86)",
                                         r"C:\Program Files (x86)"))):
            for vendor in ("Eclipse Adoptium", "Java", "Microsoft",
                           "Zulu", "Temurin", "BellSoft"):
                p = base / vendor
                if not p.is_dir():
                    continue
                try:
                    for jd in sorted(p.rglob(exe), reverse=True):
                        root = jd.parent.parent
                        if _check_jdk(root):
                            return str(jd), str(root), "OK (системная)"
                except Exception:
                    continue
        return None, None, "не найдена"
    # ---- Linux-ветка (наследие РЕД ОС / тесты) ----
    for base in [HOME / ".jdks", HOME / "jdks"]:
        if base.exists():
            for jdk in base.iterdir():
                if jdk.is_dir() and _check_jdk(jdk):
                    return str(jdk / "bin" / "java"), str(jdk), "OK (.jdks)"
    for root in ["/usr/lib/jvm", "/opt", "/usr/lib64/jvm"]:
        p = Path(root)
        if not p.exists():
            continue
        try:
            for lib in p.rglob("libawt_xawt.so"):
                jdk = lib.parent.parent
                if _check_jdk(jdk):
                    return str(jdk / "bin" / "java"), str(jdk), "OK (системная)"
        except Exception:
            continue
    import shutil
    java_path = shutil.which("java")
    if java_path:
        return java_path, None, "headless (без GUI)"
    return None, None, "не найдена"


def install_portable_java():
    """Скачать и распаковать Temurin JDK 17 (zip на Win / tar.gz на Linux)."""
    info(f"{BOLD}Установка портативной JDK 17 (Temurin){RESET}")
    JAVA_DIR.mkdir(parents=True, exist_ok=True)
    for jdk in JAVA_DIR.glob("jdk-*"):
        if _check_jdk(jdk):
            ok(f"JDK уже есть: {jdk}")
            return True
    if IS_WINDOWS:
        archive = JAVA_DIR / "jdk17.zip"
    else:
        archive = JAVA_DIR / "jdk17.tar.gz"
    if not download_file(JDK_MIRRORS, archive, "JDK 17", min_size_mb=50):
        err("Не удалось скачать JDK")
        return False
    info("Распаковываю...")
    try:
        if archive.suffix == ".zip":
            net.safe_extract_zip(archive, JAVA_DIR)
        else:
            net.safe_extract_tar(archive, JAVA_DIR)
        archive.unlink()
    except Exception as e:
        debug.dbg_exc(e, "install_portable_java/extract")
        err(f"Распаковка: {e}")
        return False
    exe = "java.exe" if IS_WINDOWS else "java"
    for jdk in JAVA_DIR.glob("jdk-*"):
        if _check_jdk(jdk):
            ok(f"JDK установлена: {jdk}")
            hint(f"Java: {jdk / 'bin' / exe}")
            return True
    err("JDK распакована, но не прошла проверку")
    return False


def cmd_install_java():
    java_bin, java_home, status = find_java()
    if java_home:
        ok(f"Java с GUI уже есть: {java_home}")
        return
    warn(f"Java: {status}")
    print()
    info("Для OptiFine installer нужна Java с GUI (AWT).")
    hint("Скачаю портативную JDK 17 в ~/java (200 МБ)?")
    try:
        answer = input(f"{MAGENTA}Скачать? [Y/n]: {RESET}").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return
    if answer not in ("", "y", "yes", "д", "да"):
        info("Отменено.")
        return
    install_portable_java()
