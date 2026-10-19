"""Скачивание с мультизеркал, докачкой, slow-mode, aria2c, HEAD-проверкой."""
import os
import ssl
import time
import subprocess
import urllib.request
import urllib.error       # <-- добавляем для HTTPError
from pathlib import Path
from modules import state, debug, net
from modules.colors import ok, info, warn, err, CYAN, RESET
from modules.config import (
    MIN_SPEED_KB, SPEED_TEST_SECONDS, CONNECT_TIMEOUT,
    SPEED_CHECK_MIN_MB, SLOW_MODE_AFTER,
    WINE_DIR, CACHE_FILE, ARIA2C_BIN,
)


def load_mirror_cache():
    if not CACHE_FILE.exists():
        return {}
    try:
        import json
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_mirror_cache(cache):
    try:
        import json
        CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        debug.dbg_exc(e, "download")
def sort_mirrors_by_cache(mirrors):
    cache = load_mirror_cache()
    return sorted(mirrors, key=lambda item: -cache.get(item[0], {}).get("speed", 0))


def remember_mirror_speed(name, speed_kb):
    cache = load_mirror_cache()
    cache[name] = {"speed": speed_kb, "ts": time.time()}
    save_mirror_cache(cache)


def get_aria2c_path():
    if ARIA2C_BIN.exists() and os.access(ARIA2C_BIN, os.X_OK):
        return str(ARIA2C_BIN)
    import shutil
    return shutil.which("aria2c")


_BINARY_MAGICS = {
    b"PK\x03\x04": (".jar", ".zip", ".mrpack"),   # zip-контейнеры
    b"\x7fELF":    (),                              # бинарники (aria2c и т.п.)
    b"\x1f\x8b":   (".gz", ".tgz"),                 # gzip / tar.gz
}


def _looks_binary(path):
    """True, если файл для бинарного расширения начинается с корректной
    сигнатуры (PK/ELF/gzip). Текстовые (.sh/.bat/.txt), .msi (OLE) и файлы
    неизвестных расширений пропускаем — проверка только против HTML-заглушек."""
    p = Path(path)
    suffix = p.suffix.lower()
    expected = None
    for magic, exts in _BINARY_MAGICS.items():
        if suffix in exts:
            expected = magic
            break
    if expected is None:
        return True
    try:
        with open(p, "rb") as f:
            head = f.read(len(expected))
        return head == expected
    except Exception:
        return False


def _insecure_fallback_allowed():
    """Явно ли пользователь разрешил insecure-фолбек (WI_INSECURE_SSL=1).

    Значение по умолчанию — НЕ разрешать. Молча подменять строгую проверку
    сертификатов CERT_NONE нельзя: это превращает любую ошибку TLS (или
    MITM-атаку с неподписанным сертификатом) в тихий downgrade. Если
    школьный MITM-прокси реально мешает, решение осознанно принимает
    пользователь: WI_INSECURE_SSL=1 (см. README).
    """
    return os.environ.get("WI_INSECURE_SSL") == "1"


def open_checked(req, timeout):
    """urlopen с единым (строгим) SSL-контекстом net.ssl_ctx().

    Проверка сертификатов строгая всегда: стандартные CA + системные
    бандлы RED OS (корни Минцифры/школьного MITM-прокси, если они
    установлены в доверие системы). Автоматический фолбек на CERT_NONE
    убран как дыра в безопасности; вместо него при SSL ошибках печатается
    понятная подсказка. Insecure-режим включается ТОЛЬКО явно:
    WI_INSECURE_SSL=1 (тогда net.ssl_ctx сам строит CERT_NONE-контекст).
    """
    try:
        return urllib.request.urlopen(req, context=net.ssl_ctx(),
                                      timeout=timeout)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        cert_bad = isinstance(reason, ssl.SSLCertVerificationError) or \
            "CERTIFICATE_VERIFY_FAILED" in str(reason)
        if cert_bad and not _insecure_fallback_allowed():
            warn(f"SSL: сертификат не прошёл проверку ({getattr(reason, 'verify_message', reason)}). "
                 "Фолбек без проверки сертификатов ОТКЛЮЧён по соображениям безопасности. "
                 "Если сеть использует MITM-прокси с корнем Минцифры — установите его корневой "
                 "сертификат в доверие системы; крайний вариант: WI_INSECURE_SSL=1 (небезопасно).")
            debug.dbg(f"open-checked-cert-error (no fallback): {req.full_url}", level="WARN")
        raise


def head_check(url, timeout=15):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with open_checked(req, timeout) as r:
            size = int(r.headers.get("Content-Length", 0))
            return True, size
    except urllib.error.HTTPError as e:
        if e.code in (405, 501):
            return True, 0
        return False, 0
    except Exception:
        return True, 0


def download_with_aria2(url, dest, silent=False):
    aria2c = get_aria2c_path()
    if not aria2c:
        return None
    debug.dbg(f"aria2c: {url}")
    try:
        cmd = [
            aria2c, "-x", "16", "-s", "16", "-k", "1M",
            "--summary-interval=5",
            "--console-log-level=warn" if not silent else "--console-log-level=error",
            "--allow-overwrite=true", "--continue=true",
            "--check-certificate=true" if os.environ.get("WI_INSECURE_SSL") != "1" else "--check-certificate=false",
            "-d", str(dest.parent), "-o", dest.name, url,
        ]
        r = subprocess.run(cmd, check=False)
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 1 * 1024 * 1024:
            if not silent:
                ok(f"Скачано {dest.stat().st_size / 1024 / 1024:.1f} МБ (aria2c)")
            return True
        return False
    except Exception as e:
        debug.dbg_exc(e, "download_with_aria2")
        if not silent:
            warn(f"aria2c: {e}")
        return None


def try_download_manual(name, url, dest, silent=False):
    debug.dbg(f"manual: {name}")
    if not silent:
        info(f"Пробую зеркало: {CYAN}{name}{RESET}")
    existing = dest.stat().st_size if dest.exists() else 0
    headers = {"User-Agent": "Mozilla/5.0"}
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"

    # --- Запрос с Range; при 416 — удаляем неполный/полный файл, качаем с нуля ---
    try:
        req = urllib.request.Request(url, headers=headers)
        r = open_checked(req, CONNECT_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 416 and existing > 0:
            # 416 = Range за пределами файла. Значит файл уже полный
            # (или битый). Удаляем и пробуем с нуля без Range.
            debug.dbg(f"416 на {name}, удаляю файл и начинаю с нуля")
            try:
                dest.unlink()
            except Exception as e:
                debug.dbg_exc(e, "download")
            existing = 0
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"}
                )
                r = open_checked(req, CONNECT_TIMEOUT)
            except Exception as e2:
                debug.dbg_exc(e2, f"connect-no-range/{name}")
                if not silent:
                    err(f"  Не подключиться: {e2}")
                return False
        else:
            debug.dbg_exc(e, f"connect/{name}")
            if not silent:
                err(f"  Не подключиться: {e}")
            return False
    except Exception as e:
        debug.dbg_exc(e, f"connect/{name}")
        if not silent:
            err(f"  Не подключиться: {e}")
        return False

    status = getattr(r, "status", 200)
    if status == 200 and existing > 0:
        existing = 0
        mode = "wb"
    else:
        mode = "ab" if existing > 0 else "wb"

    total_header = r.headers.get("Content-Length", 0)
    total = int(total_header) + existing if total_header else 0
    downloaded = existing
    chunk = 1024 * 128
    start_time = time.time()
    speed_checked = False
    do_speed_check = (total > SPEED_CHECK_MIN_MB * 1024 * 1024) and not state.slow_mode_active

    # Сколько байт зеркало ОБЕЩАЕТ отдать в этом ответе (Content-Length при
    # Range-докачке — это остаток, не весь файл).
    expected_bytes = int(total_header) if total_header else 0

    try:
        with open(dest, mode) as f:
            while True:
                try:
                    data = r.read(chunk)
                except Exception as e:
                    debug.dbg_exc(e, f"read/{name}")
                    if not silent:
                        print()
                        warn(f"  Обрыв на {downloaded/1024/1024:.1f} МБ")
                    return False
                if not data:
                    break
                f.write(data)
                downloaded += len(data)
                elapsed = time.time() - start_time
                speed_kb = ((downloaded - existing) / 1024) / elapsed if elapsed > 0 else 0
                if not silent and total > 0:
                    pct = downloaded * 100 // total
                    mb_done = downloaded / 1024 / 1024
                    mb_all = total / 1024 / 1024
                    bar = "█" * (pct // 2) + "░" * (50 - pct // 2)
                    eta_sec = int(((total - downloaded) / 1024) / speed_kb) if speed_kb > 0 else 0
                    eta = f"{eta_sec//60}:{eta_sec%60:02d}"
                    import sys
                    sys.stdout.write(
                        f"\r  {bar} {pct:3d}%  {mb_done:6.1f}/{mb_all:.1f} МБ  "
                        f"({speed_kb:6.0f} КБ/с, ETA {eta})"
                    )
                    sys.stdout.flush()
                if not speed_checked and elapsed >= SPEED_TEST_SECONDS and do_speed_check:
                    speed_checked = True
                    if speed_kb < MIN_SPEED_KB:
                        state.slow_mirror_count += 1
                        if not silent:
                            print()
                            warn(f"  Медленно ({speed_kb:.0f} КБ/с)")
                        r.close()
                        if state.slow_mirror_count >= SLOW_MODE_AFTER:
                            state.slow_mode_active = True
                        return False
        if not silent:
            print()
        # Обрыв без исключения: сервер закрыл соединение раньше, чем отдал
        # обещанные Content-Length байты. Раньше такой «успешный» конец
        # цикла молча считался завершённой закачкой — обрезанный файл
        # оставался на диске и в лучшем случае отсекался только проверкой
        # сигнатуры (а корректно начатый, но урезанный с хвоста jar/zip
        # проходил её). Возвращаем False: докачка продолжится по Range
        # при следующем запуске.
        if expected_bytes and downloaded < existing + expected_bytes:
            debug.dbg(f"truncated {name}: получено {downloaded} из "
                      f"{existing + expected_bytes} байт", level="WARN")
            if not silent:
                warn(f"  Ответ оборвался: {downloaded/1024/1024:.1f} МБ из "
                     f"{(existing + expected_bytes)/1024/1024:.1f} МБ — пробую другое зеркало")
            return False
        if not dest.exists() or dest.stat().st_size < 1024:
            return False
        size = dest.stat().st_size
        if not silent:
            ok(f"  Готово: {size/1024/1024:.1f} МБ")
        total_time = time.time() - start_time
        final_speed = ((size - existing) / 1024) / total_time if total_time > 0 else 0
        remember_mirror_speed(name, final_speed)
        return True
    except Exception as e:
        debug.dbg_exc(e, f"write/{name}")
        if not silent:
            print()
            err(f"  Обрыв: {e}")
        return False
    finally:
        try:
            r.close()
        except Exception as e:
            debug.dbg_exc(e, "download")
def download_file(mirrors, dest, label, min_size_mb=10, silent=False):
    WINE_DIR.mkdir(parents=True, exist_ok=True)
    # min_size_mb=0 раньше пропускал любую закачку, включая пустые/обрубки:
    # `st_size >= 0` истинно для любого файла. Ставим нижний порог 1 КБ —
    # реальные артефакты (jar, mrpack, tar.gz) всё равно больше.
    min_size = max(min_size_mb * 1024 * 1024, 1024)

    # Ранний выход: файл уже скачан полностью. Для бинарных архивов
    # (jar/zip/tar.gz/mrpack) проверяем «магические» байты — иначе
    # HTML-заглушка от прокси, лежащая в кэше, считалась бы успешной
    # закачкой и ломала установку на этапе распаковки.
    if dest.exists() and dest.stat().st_size >= min_size and _looks_binary(dest):
        if not silent:
            info(f"{label}: уже скачан ({dest.stat().st_size / 1024 / 1024:.1f} МБ)")
        return True
    if dest.exists() and not _looks_binary(dest):
        debug.dbg(f"download_file: {dest.name} — не бинарник (HTML?), перекачиваю")

    if not silent:
        info(f"Скачиваю {label}...\n")
    mirrors_sorted = sort_mirrors_by_cache(mirrors)
    if get_aria2c_path() and min_size_mb >= 5 and (not dest.exists() or dest.stat().st_size == 0):
        name, url = mirrors_sorted[0]
        ok_head, size = head_check(url)
        if not ok_head:
            if not silent:
                warn(f"HEAD-проверка {name} не удалась, пропускаю зеркало")
        elif size and size < min_size:
            if not silent:
                warn(f"HEAD сообщает размер {size/1024/1024:.1f} МБ < ожидаемого, пропускаю")
        else:
            start_time = time.time()
            result = download_with_aria2(url, dest, silent=silent)
            if result is True and dest.exists() and dest.stat().st_size >= min_size \
                    and _looks_binary(dest):
                elapsed = time.time() - start_time
                if elapsed > 0:
                    remember_mirror_speed(name, (dest.stat().st_size / 1024) / elapsed)
                return True
    for name, url in mirrors_sorted:
        if try_download_manual(name, url, dest, silent=silent):
            if dest.exists() and dest.stat().st_size >= min_size and _looks_binary(dest):
                return True
            if dest.exists() and not _looks_binary(dest):
                # зеркало отдало HTML вместо бинарника — чистим и идём дальше
                debug.dbg(f"download_file: зеркало {name} отдало HTML, удаляю {dest.name}")
                try:
                    dest.unlink()
                except Exception as e:
                    debug.dbg_exc(e, "download")
    if not silent:
        err(f"Не удалось скачать {label}.")
    return False


# ---------- ПАРАЛЛЕЛЬНЫЙ СТАТУС ----------
_parallel_lines = {}


def parallel_status(name, pct, mb_done=0, mb_all=0, speed_kb=0):
    if mb_all > 0:
        bar = "█" * (pct // 2) + "░" * (50 - pct // 2)
        line = f"  [{CYAN}{name:5}{RESET}] {bar} {pct:3d}%  {mb_done:5.1f}/{mb_all:.1f} МБ  ({speed_kb:5.0f} КБ/с)"
    else:
        line = f"  [{CYAN}{name:5}{RESET}] скачиваю..."
    _parallel_lines[name] = line
    _redraw_parallel()


def _redraw_parallel():
    import sys
    if _parallel_lines:
        sys.stdout.write("\r" + "\n".join(_parallel_lines.values()) + "\n")
        for _ in range(len(_parallel_lines)):
            sys.stdout.write("\033[F")
        sys.stdout.write("\r")
    sys.stdout.flush()
