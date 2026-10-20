"""Скачивание с мультизеркал, докачкой, slow-mode, aria2c, HEAD-проверкой."""
import http.client
import os
import socket
import ssl
import time
import subprocess
import urllib.parse
import urllib.request
import urllib.error       # <-- добавляем для HTTPError
from pathlib import Path
from modules import state, debug, net
from modules.colors import ok, info, warn, err, hint, CYAN, RESET
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


# Полностью браузерный набор заголовков. Часть CDN (в т.ч. Legacy Launcher)
# рвёт соединение (WinError 10054 / ECONNRESET) на запросы, где TLS-рукопожатие
# выдаёт «питоновский» фингерпринт и минимальный набор заголовков.
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "close",   # keep-alive не нужен: читаем один поток до конца
}


def _browser_headers(req):
    """Дописывает в запрос недостающие браузерные заголовки."""
    have = {k.lower() for k, _ in req.header_items()}
    for key, val in _BROWSER_HEADERS.items():
        if key.lower() not in have:
            req.add_header(key, val)
    return req


class _RawSSLOpenResponse:
    """Обёртка над http.client.HTTPResponse для ответа, полученного через
    прямой ssl-сокет (_open_raw). Интерфейс совпадает с тем, что использует
    try_download_manual/head_check: .status, .headers.get(), .read(), .close().
    """

    def __init__(self, resp, sock):
        self._resp = resp
        self._sock = sock
        self.status = resp.status
        self.headers = resp.headers

    def read(self, n=-1):
        return self._resp.read(n)

    def close(self):
        try:
            self._resp.close()
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


def _ssl_ctx_for(url_obj):
    """SSL-контекст для raw-сокета.

    По умолчанию строгий (системные CA). Но на Windows Python НЕ видит
    системное хранилище сертификатов, а школьные MITM-прокси подменяют цепочку
    своим корнем — тогда браузер «всё открывает», а любой питоновский TLS
    падает с bad_certificate / self-signed certificate. Для таких случаев
    есть второй, последний заход: контекст из хранилища Windows (_windows_ctx)
    с ослабленной проверкой hostname/IP. Это ровно тот же уровень доверия,
    что у браузера на этом компьютере; молча спускаться до CERT_NONE мы
    отказываемся — insecure включается только явно (WI_INSECURE_SSL=1).
    """
    host = (url_obj.hostname or "").lower()
    ctx = ssl.create_default_context()
    # IP-адрес вместо имени (например, при системном DNS-перехвате llaun.ch)
    # — сертификат почти наверняка выписан на домен, отключаем сверку адреса.
    try:
        import ipaddress
        if ipaddress.ip_address(host):
            ctx.check_hostname = False
    except ValueError:
        pass
    return ctx


def _open_raw(req, timeout, _depth=0, _ctx_stage=0):
    """Запасной путь: HTTPS-запрос через прямой ssl+http.client сокет.

    urllib.request прокидывает в ssl.SSLContext только ALPN (h2/h1.1) и SNI —
    всё остальное (CHROME-style перестановка эллиптических кривых,
    signature_algorithms, supported_versions) Python не позволяет задать без
    третьих библиотек. Поэтому даже идентичные User-Agent и заголовки иногда
    приводят к RST от CDN, а браузер в это же время качает нормально.
    Здесь мы хотя бы убираем ALPN (только HTTP/1.1) и Connection: close —
    иногда этого достаточно. Redirect (3xx + Location) отрабатывается
    рекурсией (до 5 раз).

    Этапы TLS (_ctx_stage), чтобы отличать «сервер рвёт соединение» от
    «нам не нравится его сертификат»:
      0 — строгий контекст; при ошибке верификации цепочки -> 1,
          при reset/timeout -> сразу 2 (тот же запрос ещё раз);
      1 — контекст из хранилища Windows / certifi, check_hostname=False;
      2 — повтор запроса (CDN-флэпы: первое RST, второе проходит).
    CERT_NONE здесь НЕ используется (см. _insecure_fallback_allowed).
    """
    url = urllib.parse.urlsplit(req.full_url)
    if url.scheme != "https":
        raise urllib.error.URLError("raw-fallback умеет только https")
    port = url.port or 443
    if _ctx_stage == 1:
        ctx = _windows_ctx()
        if ctx is None:
            ctx = ssl.create_default_context()
        try:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_REQUIRED
        except Exception as e:
            debug.dbg(f"_open_raw stage1 ctx: {e}", level="WARN")
    else:
        ctx = _ssl_ctx_for(url)
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except (NotImplementedError, AttributeError):
        pass
    host = url.hostname
    conn = None
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        ssock = ctx.wrap_socket(raw, server_hostname=host)
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.sock = ssock
        hdrs = {k: v for k, v in req.header_items()}
        hdrs.setdefault("Host", host)
        hdrs["Connection"] = "close"
        path = url.path or "/"
        if url.query:
            path += "?" + url.query
        method = "GET" if req.data is None else "POST"
        conn.request(method, path, body=req.data, headers=hdrs)
        resp = conn.getresponse()
        # редиректы: urllib их сам разворачивал, здесь делаем вручную
        if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
            loc = resp.getheader("Location")
            new_url = urllib.parse.urljoin(req.full_url, loc)
            debug.dbg(f"_open_raw: redirect {resp.status} -> {new_url}")
            try:
                resp.read()
            except Exception:
                pass
            resp.close()
            ssock.close()
            if _depth >= 5:
                raise urllib.error.URLError("слишком много редиректов (raw)")
            nreq = urllib.request.Request(new_url, data=req.data,
                                          method=method)
            for k, v in hdrs.items():
                if k.lower() not in ("host", "connection"):
                    nreq.add_header(k, v)
            return _open_raw(nreq, timeout, _depth + 1, _ctx_stage)
        return _RawSSLOpenResponse(resp, ssock)
    except Exception as e:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        # Если TLS-рукопожатие упало на НЕверифицированном сертификате
        # (школьный MITM / Python не видит хранилище Windows) — один раз
        # повторяем с контекстом из системного хранилища Windows.
        if _ctx_stage == 0 and _is_cert_error(e):
            debug.dbg("_open_raw: cert error, пробую хранилище Windows",
                      level="WARN")
            return _open_raw(req, timeout, _depth, _ctx_stage=1)
        # Reset/timeout на строгом контексте — ещё один заход (CDN-флэпы).
        if _ctx_stage in (0, 1) and _is_resetlike(e):
            debug.dbg(f"_open_raw: reset ({e!r}), повторяю запрос",
                      level="WARN")
            return _open_raw(req, timeout, _depth, _ctx_stage=2)
        reason = e if isinstance(e, OSError) else \
            urllib.error.URLError(e)
        raise reason


def save_response(r, dest, name="mirror", silent=False):
    """Сохраняет тело ответа (открытый объект с .read()/.headers) в файл.

    Используется фоллбеками download_file (системный прокси). Возвращает
    True, если записано >=1 КБ данных.
    """
    try:
        data = r.read()
    except Exception as e:
        debug.dbg(f"save_response read ({name}): {e}", level="WARN")
        return False
    finally:
        try:
            r.close()
        except Exception:
            pass
    if not data or len(data) < 1024:
        return False
    try:
        Path(dest).write_bytes(data)
    except OSError as e:
        debug.dbg(f"save_response write ({name}): {e}", level="WARN")
        return False
    if not silent:
        ok(f"  Готово (прокси): {len(data) / 1024 / 1024:.1f} МБ")
    return True


def open_system_proxy(req, timeout):
    """Третий путь скачивания: запрос через системный прокси (IE-настройки).

    В школьных/офисных сетях прямой TLS часто рвётся (WinError 10054), а
    браузер при этом всё качает — потому что идёт через корпоративный прокси,
    настройки которого Windows хранит в реестре. urllib.getproxies() читает
    именно их; если прокси настроен, пробуем через него перед тем, как
    сдаться. Возвращает ответ или None (прокси нет / не получилось) —
    raise ничего не выбрасывает, это чистый фоллбек.
    """
    import urllib.request as _ur
    try:
        proxies = _ur.getproxies()
    except Exception:
        return None
    proxy = proxies.get("https") or proxies.get("http")
    if not proxy:
        debug.dbg("open_system_proxy: системный прокси не настроен")
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    debug.dbg(f"open_system_proxy: пробую через прокси {proxy}")
    try:
        handler = _ur.ProxyHandler({"http": proxy, "https": proxy})
        opener = _ur.build_opener(handler)
        resp = opener.open(_browser_headers(req), timeout=timeout)
        ok(f"Скачиваю через системный прокси ({proxy.split('://', 1)[-1]})")
        return resp
    except Exception as e:
        debug.dbg(f"open_system_proxy failed: {e}", level="WARN")
        return None


CURL_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36")


_proxy_warned = False


def windows_ie_proxy():
    """Настроен ли в Windows прокси (параметр ProxyEnable в IE-настройках).

    Читает реестр напрямую, а не через urllib.getproxies(): последний вернёт
    пустой словарь и при ProxyEnable=1 с пустым ProxyServer, и наоборот —
    для проверки «выключен ли прокси» нужен именно флаг. Возвращает dict
    {"enabled": bool, "server": str}; на не-Windows или при любой ошибке —
    {"enabled": False, "server": ""} (никогда не бросает исключений).

    Смысл: включённый прокси (особенно MITM-фильтры вроде школьных) ломает
    TLS-рукопожатие Python («Missing Authority Key Identifier»), рвёт
    schannel-рукопожатие curl (rc=35) и делает Yandex-CDN заметно медленнее
    (прокси буферит весь ответ перед выдачей). curl без --noproxy сам
    подхватывает системный прокси из реестра — поэтому предупреждаем один раз
    перед каждой загрузкой. Отключить: Параметры → Сеть и Интернет → Прокси →
    «Использовать прокси-сервер» — ВЫКЛ; либо `netsh winhttp reset proxy`.
    """
    out = {"enabled": False, "server": ""}
    try:
        from modules.platform_utils import IS_WINDOWS as _IS_WIN
        if not _IS_WIN:
            return out
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        with key:
            try:
                out["enabled"] = bool(winreg.QueryValueEx(key, "ProxyEnable")[0])
            except OSError:
                pass
            try:
                out["server"] = str(winreg.QueryValueEx(key, "ProxyServer")[0])
            except OSError:
                pass
    except Exception as e:
        debug.dbg(f"windows_ie_proxy: {e}", level="WARN")
    return out


def warn_if_windows_proxy():
    """Однократное (за запуск скрипта) предупреждение о включённом прокси."""
    global _proxy_warned
    if _proxy_warned:
        return
    px = windows_ie_proxy()
    if not px["enabled"]:
        debug.dbg("proxy check: прокси в настройках Windows выключен")
        return
    _proxy_warned = True   # предупреждаем один раз, чтобы не спамить на зеркалах
    server = px["server"] or "(адрес не указан)"
    warn(f"В настройках Windows ВКЛЮЧЕН прокси ({server}).")
    hint("Прокси замедляет закачку (буферизует ответ целиком) и может рвать")
    hint("TLS-рукопожатия: 'SSL/TLS connection failed' у curl и 'Missing")
    hint("Authority Key Identifier' у Python — типичные его симптомы.")
    hint("Выключи: Параметры -> Сеть и Интернет -> Прокси ->")
    hint("'Использовать прокси-сервер' = ОТКЛ (или netsh winhttp reset proxy)")


# Типовые текстовые ответы вместо файла (HTML-заглушки фильтров/прокси).
_HTML_FAIL_MARKERS = (b"<!doctype", b"<html", b"<head", b"<?xml")


def _downloaded_text_stub(path):
    """True, если файл похож на HTML/XML-заглушку (а не на бинарник)."""
    try:
        with open(path, "rb") as f:
            head = f.read(64)
        low = head.lower().lstrip(b"\xef\xbb\xbf \t\r\n")
        return any(low.startswith(m) for m in _HTML_FAIL_MARKERS)
    except OSError:
        return False


def _is_tls_fail(errtxt):
    """curl rc=35 (SSL connect error) / TLS-фразы в stderr — рукопожатие
    не прошло (свежий Schannel + TLS 1.3 не дружат с Cloudflare и рядом
    CDN; лечится --tlsv1.2). rc=6 — DNS не резолвится, это НЕ TLS-ошибка."""
    t = (errtxt or "").lower()
    # «rc=35» — формат, которым сам код возвращает ошибку без текста stderr
    # (_curl_with_progressbar: f"rc={rc}"). Раньше эта ветка ловила только
    # фразы вроде "SSL connect error" из stderr, и при пустом stderr
    # («curl attempt failed ([]): rc=35») обходы --tlsv1.2/--http2 НЕ
    # добавлялись в очередь — все заходы падали с тем же rc=35, и установка
    # Legacy в школьной сети превращалась в простыню «curl не смог: rc=35».
    if "rc=35" in t or "rc=35:" in t or "code 35" in t:
        return True
    if "error 35" in t or "ssl connect error" in t \
            or "schannel" in t or "tls" in t:
        return True
    return False


# URL -> аргументы curl, с которыми HEAD-проба зеркала прошла, хотя без
# них падала (rc=35). Заполняется в probe_mirrors_with_curl, используется в
# download_queue_with_curl, чтобы не жечь таймауты на заведомо битом
# TLS-рукопожатии. Живёт весь запуск скрипта.
_tls_fix_cache = {}


def _curl_base_args(timeout, extra=None):
    """Общие аргументы curl. При включённом прокси Windows добавляем
    --noproxy '*' — качаем напрямую, как того хочет пользователь.

    ВАЖНО (лечит «prism также выдает ошибку»): раньше здесь стояли
    `--fail` и `--retry 2`. На GitHub-редиректах (Prism, все gh-proxy
    зеркала) при обрыве соединения на середине загрузки curl с --retry
    ДОБИРАЕТ файл вторым запросом с Range, но сервер отдаёт новый
    полный поток без 206 — curl дописывает его к огрыжку и молча
    возвращает rc=0. Результат: «скачанный» zip битый (внутри нет
    prismlauncher.exe / icu*.dll) или HTML-заглушка прокси проходит
    проверку по размеру. Теперь: --proto-redir (HTTPS не деградит в
    HTTP), --continue-at - (доводит обрыв честным Range, без склейки
    двух полных потоков), без --fail/--retry (недокачка честно
    возвращается rc!=0, зеркало перепробуется с чистого tmp)."""
    args = ["--location-trusted", "--http1.1",
            "--proto-redir", "https",
            "--connect-timeout", str(timeout // 4 or 15),
            "--max-time", str(timeout),
            "--user-agent", CURL_BROWSER_UA]
    if windows_ie_proxy()["enabled"]:
        args += ["--noproxy", "*"]
    if extra:
        args += list(extra)
    return args


def _run_curl_resume(exe, url, tmp, timeout, extra=None):
    """Докачка оборванного файла: тот же curl + `-C -`.

    Если предыдущий запуск оставил .curlpart/.part меньшего размера,
    curl продолжит с конца (Range) вместо новой закачки с нуля. Возвращает
    (ok, errtext) — формат как у _run_curl. Не подходит для URL с
    одноразовыми подписями (?md5=&expires=...) — там повторный Range
    может получить 403, поэтому вызывающий код решает, можно ли докачивать.
    """
    if not Path(tmp).exists():
        return False, "нет частичного файла для докачки"
    cmd = [exe, "-C", "-"] + _curl_base_args(timeout, extra) \
        + ["--output", str(tmp), url]
    try:
        r = subprocess.run(cmd, check=False, timeout=timeout + 90,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE)
        if r.returncode == 0 and Path(tmp).exists() \
                and Path(tmp).stat().st_size > 0:
            return True, ""
        # rc=33 — сервер не поддерживает Range: считаем недокачкой,
        # вызывающий код начнёт заново с чистого tmp.
        errtxt = ""
        try:
            lines = (r.stderr or b"").decode("utf-8", errors="ignore") \
                .strip().splitlines()
            errtxt = lines[-1] if lines else ""
        except Exception:
            pass
        return False, f"rc={r.returncode}: {errtxt}"
    except Exception as e:
        return False, str(e)


def _fmt_speed(bps):
    """Красивый вывод скорости."""
    if bps < 1024:
        return f"{bps:.0f} Б/с"
    elif bps < 1024 * 1024:
        return f"{bps / 1024:.1f} КБ/с"
    else:
        return f"{bps / 1024 / 1024:.1f} МБ/с"


def _curl_head_size(exe, url, timeout=15, extra=None):
    """Быстрый HEAD-запрос для получения размера файла. Возвращает байты или None."""
    try:
        cmd = [exe, "-I", "--silent", "--show-error",
               "--connect-timeout", str(timeout // 3),
               "--max-time", str(timeout),
               "--location-trusted",
               "--user-agent", CURL_BROWSER_UA] \
            + list(extra or []) + [url]
        r = subprocess.run(cmd, check=False, timeout=timeout + 5,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for line in (r.stdout or b"").decode("utf-8", errors="ignore").splitlines():
            low = line.lower().strip()
            if low.startswith("content-length:"):
                val = low.split(":", 1)[1].strip()
                if val.isdigit():
                    return int(val)
    except Exception as e:
        debug.dbg(f"_curl_head_size: {e}", level="WARN")
    return None


def _curl_http_code(exe, url, timeout=15):
    """Реальный HTTP-код ответа curl после следования редиректам (-w %{http_code}).

    Нужен потому, что без --fail curl при rc=0 молча сохраняет и 403, и
    HTML-заглушку зеркала — «успешная» закачка мусора давала битые архивы
    Prism/Legacy. Возвращает int или None (сеть/ошибка).
    """
    try:
        cmd = [exe, "-o", os.devnull, "--silent", "--show-error",
               "--location-trusted", "--http1.1",
               "--connect-timeout", str(max(timeout // 3, 5)),
               "--max-time", str(timeout),
               "--user-agent", CURL_BROWSER_UA,
               "-w", "%{http_code}", url]
        if windows_ie_proxy()["enabled"]:
            cmd += ["--noproxy", "*"]
        r = subprocess.run(cmd, check=False, timeout=timeout + 10,
                           stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL)
        out = (r.stdout or b"").decode("ascii", errors="ignore").strip()
        if out.isdigit():
            return int(out)
    except Exception as e:
        debug.dbg(f"_curl_http_code: {e}", level="WARN")
    return None


def _curl_http_ok(errtxt):
    """errtxt из _run_curl/_curl_with_progressbar: пустая строка — всё ок;
    'NNN' — HTTP-код ошибки (rc был 0). True, если ошибок нет."""
    return errtxt == ""


_CURL_OK_CODES = {"200", "206", "301", "302", "303", "307", "308"}


def _downloaded_file_plausible(dest_tmp, url=""):
    """Здравый смысл скачанного файла до переноса на место назначения.

    Ловит два типовых мусора, которые раньше проходили по размеру:
      1) HTML-заглушка прокси/зеркала (gh-proxy отдаёт страницу вместо
         zip'а) — начинается с '<';
      2) .zip/.jar/.mrpack без магических байтов PK\\x03\\x04 (обрывок,
         склейка двух потоков после --retry).
    Прочие расширения (.exe и т.п.) проверяем только на HTML.
    """
    try:
        p = Path(dest_tmp)
        if not p.is_file() or p.stat().st_size < 512:
            return False
        with open(p, "rb") as f:
            head = f.read(16)
        low = head.lower().lstrip(b"\xef\xbb\xbf \t\r\n")
        if low[:1] == b"<":           # <!DOCTYPE / <html — заглушка
            return False
        suffix = p.suffix.lower()
        if suffix in (".zip", ".jar", ".mrpack"):
            return head[:4] == b"PK\x03\x04"
        return True
    except OSError:
        return False


def _curl_with_progressbar(exe, url, tmp, timeout, extra=None, expected_size=None):
    """curl с live progress bar (для больших файлов)."""
    base_cmd = ([exe] + _curl_base_args(timeout, extra)
                + ["--output", str(tmp)])
    base_cmd.append(url)

    debug.dbg(f"curl: {' '.join(base_cmd)}")

    proc = None
    last_bytes = 0
    start_time = time.time()
    total_known = expected_size

    try:
        import re
        cmd = base_cmd.copy()
        cmd.insert(1, "--progress-bar")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=1,
            universal_newlines=True,
            encoding='utf-8',
            errors='replace'
        )

        # Читаем stderr построчно (curl пишет прогресс туда)
        for line in proc.stderr:
            parts = line.split('\r')
            status = parts[-1].strip()

            # Парсим формат curl --progress-bar:
            # "####---  45%  1.2M  0:00:05  0:00:03  0:00:02"
            m = re.search(r'(\d+)%.*?([\d.]+)([KMGT]?i?B)', status)
            if m:
                pct = int(m.group(1))
                num = float(m.group(2))
                unit = m.group(3)

                mult = {'K': 1024, 'M': 1024**2, 'G': 1024**3,
                        'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3}
                bytes_dl = int(num * mult.get(unit, 1))

                elapsed = time.time() - start_time
                speed = bytes_dl / max(elapsed, 0.1)

                eta_sec = ((100 - pct) / max(pct, 1)) * elapsed if pct > 0 else 0

                bar_len = 30
                filled = int(bar_len * pct / 100)
                bar = '█' * filled + '░' * (bar_len - filled)

                mb_dl = bytes_dl / 1024 / 1024
                if total_known and total_known > 0:
                    mb_tot = total_known / 1024 / 1024
                    size_info = f"{mb_dl:.1f}/{mb_tot:.1f} МБ"
                else:
                    size_info = f"{mb_dl:.1f} МБ"

                msg = (f"\r  {CYAN}[{bar}]{RESET} {pct:3d}% "
                       f"{size_info} ({_fmt_speed(speed)}) ETA:{eta_sec:.0f}s   ")
                print(msg, end='', flush=True)
                last_bytes = bytes_dl

        proc.wait()
        rc = proc.returncode

        if last_bytes > 0:
            print()

        if rc == 0 and Path(str(tmp)).exists() \
                and Path(str(tmp)).stat().st_size > 0:
            # Без --fail curl вернёт 0 и на HTTP 403/503 с HTML-заглушкой.
            # Быстрая проверка фактического кода после редиректов; ошибку
            # возвращаем в формате "HTTP NNN" (download_via_curl отличает
            # её от сетевой по непустой строке).
            code = _curl_http_code(exe, url, timeout=min(15, max(timeout // 4, 8)))
            if code is not None and str(code) not in _CURL_OK_CODES:
                return False, f"HTTP {code}"
            return True, ""

        return False, f"rc={rc}"

    except Exception as e:
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        return False, str(e)


def _run_curl(exe, url, tmp, timeout, extra=None, head_extra=None):
    """Один запуск curl.exe -> tmp. Возвращает (True|False, текст_ошибки).

    Автоматически выбирает режим: обычный для быстрых скачиваний,
    с прогресс-баром для больших файлов (>3 МБ ожидаемо).
    head_extra — доп. аргументы только для HEAD-пробы размера (TLS-фиксы
    --tlsv1.2/--http2 из очереди attempts; без них проба падает с rc=35
    и тянет таймаут на недоступном рукопожатии).
    """
    # Пробуем узнать размер через HEAD (быстро, до 10 сек)
    head_timeout = min(10, timeout // 6)
    expected_size = _curl_head_size(exe, url, timeout=head_timeout,
                                    extra=head_extra)

    debug.dbg(f"_run_curl: expected_size={expected_size}")

    # Если файл большой (>3МБ) или неизвестный размер — используем прогресс-бар
    use_progress = (expected_size is None or expected_size > 3 * 1024 * 1024)

    if use_progress:
        return _curl_with_progressbar(exe, url, tmp, timeout, extra, expected_size)

    # Обычный режим для маленьких файлов
    cmd = [exe] + _curl_base_args(timeout, extra) + ["--output", str(tmp)]
    cmd.append(url)
    debug.dbg(f"curl (small): {' '.join(cmd)}")

    try:
        r = subprocess.run(cmd, check=False, timeout=timeout + 90,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            # Как в прогресс-режиме: без --fail rc=0 бывает и на 403/503.
            code = _curl_http_code(exe, url, timeout=min(15, max(timeout // 4, 8)))
            if code is not None and str(code) not in _CURL_OK_CODES:
                return False, f"HTTP {code}"
            return True, ""
        errtxt = ""
        try:
            lines = (r.stderr or b"").decode("utf-8", errors="ignore") \
                .strip().splitlines()
            errtxt = lines[-1] if lines else ""
        except Exception:
            pass
        return False, f"rc={r.returncode}: {errtxt}"
    except Exception as e:
        return False, str(e)


def download_via_curl(url, dest, timeout=CONNECT_TIMEOUT * 4, extra=None):
    """Запасная закачка системной утилитой curl (есть в Windows 10 1803+).

    extra — доп. аргументы curl для всех заходов (например ["--tlsv1.2"],
    который HEAD-проба зеркала подобрала через _tls_fix_cache).

    curl.exe использует Schannel — системное хранилище сертификатов и, если
    он настроен, системный прокси, поэтому проходит там, где питоновский
    ssl падает (MITM-корень школы, RST на TLS-фингерпринт Python).
    True — файл скачан (размер проверяет вызывающий код).

    Варианты запуска (первый же успех прерывает цикл):
      1) как есть — curl сам подхватит системный прокси из реестра;
      2) --noproxy "*" — если корпоративный прокси мешает прямой закачке;
      3) --tlsv1.2 / --http2 — см. ниже (лечит rc=35);
      4) insecure — только при WI_INSECURE_SSL=1 (школа с MITM-сертификатом);
      5) wget.exe — если вдруг curl отсутствует, но есть wget в PATH.

    ЛЕЧИМ rc=35 («SSL connect error», curl: (35)): на Windows 10 22H2+ и 11
    встроенный curl работает через Schannel, и его TLS 1.3 ClientHello не
    проходят с Cloudflare (llaun.ch / dl.llaun.ch), а иногда и с другими CDN
    — рукопожатие рвётся ещё до сертификата. Браузер в это время качает,
    потому что использует другие наборы параметров. Проверенные обходы:
      * --tlsv1.2 — принудительно старый добрый TLS 1.2;
      * --http2   — другой ClientHello (HTTP/2 ALPN), тоже часто проходит.
    Поэтому при ошибке TLS добавляем эти заходы в конец очереди вариантов.
    """
    exe = shutil_which_curl()
    if not exe:
        return _download_via_wget(url, dest, timeout)
    # Если HEAD-проба (probe_mirrors_with_curl) уже выяснила, что зеркало
    # живо только с TLS-фиксом, а вызывающий код не передал extra —
    # подхватим фикс из кэша сами. Иначе первый заход снова падает с
    # rc=35 (~20 с таймаута на зеркало), и пользователь видит простыню
    # «curl не смог: rc=35» по всем зеркалам Legacy.
    if not extra:
        extra = _tls_fix_cache.get(url)
    tmp = Path(str(dest) + ".curlpart")
    # Докачка с прошлого запуска: если остался .curlpart и URL не с
    # одноразовой подписью (Yandex CDN md5/expires может отдать 403 на
    # повторном Range), сначала честно продолжаем -C -.
    try:
        if tmp.exists() and tmp.stat().st_size > 0 \
                and "expires=" not in url.lower():
            r_ok, r_err = _run_curl_resume(exe, url, tmp, timeout)
            if r_ok and _curl_http_ok(r_err) \
                    and _downloaded_file_plausible(tmp):
                os.replace(tmp, dest)
                debug.dbg("curl: докачали оборванный файл (-C -)")
                return True
            #Range не поддержан или мусор — начинаем заново с чистого tmp
            try:
                tmp.unlink()
            except OSError:
                pass
    except Exception as e:
        debug.dbg(f"curl resume skip: {e}", level="WARN")
    # Если в Windows включён прокси — обходим его напрямую (_curl_base_args
    # добавит --noproxy '*'); отдельный --noproxy-заход тогда не нужен.
    attempts = [list(extra or [])]
    if not windows_ie_proxy()["enabled"]:
        attempts.append(list(extra or []) + ["--noproxy", "*"])
    if _insecure_fallback_allowed():
        attempts.append(["--insecure"])
        warn("curl: нестрогая проверка сертификатов "
             "(WI_INSECURE_SSL=1 включён пользователем)")
    last_err = ""
    tls_note_shown = False
    for extra in list(attempts):
        # Если базовый заход упал с rc=35, последующие TLS-фиксы
        # (--tlsv1.2 / --http2) должны унаследовать их в HEAD-пробе тоже,
        # иначе _run_curl зависнет на недоступном рукопожатии ещё до GET.
        head_extra = [a for a in extra if a != "--noproxy" and a != "*"] \
            if any(a in ("--tlsv1.2", "--http2") for a in extra) else None
        ok_, errtxt = _run_curl(exe, url, tmp, timeout, extra,
                                head_extra=head_extra)
        if ok_ and _curl_http_ok(errtxt):
            # Проверка содержимого до переноса: gh-proxy/HTML-заглушка или
            # склеенный при обрыве zip раньше проходили по размеру и
            # «успешно устанавливались» битком — отсюда ошибки Prism после
            # «удачной» закачки.
            if not _downloaded_file_plausible(dest_tmp=tmp, url=url):
                warn("  curl скачал мусор (HTML/битый архив) — пробую "
                     "дальше")
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
                continue
            os.replace(tmp, dest)
            return True
        if ok_:
            # rc=0, но в выводе есть HTTP-код ошибки (без --fail curl так
            # молча сохраняет 404/503-страницы как «успех»)
            errtxt = f"HTTP {errtxt}"
        last_err = errtxt
        debug.dbg(f"curl attempt failed ({extra}): {errtxt}", level="WARN")
        # rc=35 (SSL connect error): пробуем те же запросы, но с другим
        # вариантом TLS — Schannel + TLS 1.3 падает на Cloudflare-доменах
        # (llaun.ch / dl.llaun.ch), а с --tlsv1.2 или --http2 проходит.
        if _is_tls_fail(errtxt):
            if not tls_note_shown:
                tls_note_shown = True
                info("  curl: TLS-рукопожатие не прошло (rc=35) — "
                     "пробую обходные варианты (--tlsv1.2 / --http2)")
            for tlsfix in (["--tlsv1.2"], ["--http2"]):
                combo = list(extra) + tlsfix
                if combo in attempts:
                    continue
                attempts.append(combo)
    if last_err:
        info(f"  curl не смог: {last_err[:120]}")
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass
    return False


def _download_via_wget(url, dest, timeout):
    """Последний внешний инструмент: wget.exe (если стоит в PATH)."""
    import shutil as _sh
    exe = _sh.which("wget") or _sh.which("wget.exe")
    if not exe:
        return False
    tmp = Path(str(dest) + ".wgetpart")
    cmd = [exe, "-q", "--tries=2", "--timeout=20",
           "--user-agent=" + CURL_BROWSER_UA,
           "-O", str(tmp), url]
    try:
        r = subprocess.run(cmd, check=False, timeout=timeout + 90)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            os.replace(tmp, dest)
            ok(f"  Готово (wget): {dest.stat().st_size / 1024 / 1024:.1f} МБ")
            return True
    except Exception as e:
        debug.dbg(f"wget failed: {e}", level="WARN")
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass
    return False


def _curl_noproxy_args():
    """--noproxy '*' если в Windows включён системный прокси — HEAD-пробы и
    закачки должны идти напрямую (прокси буферит ответ и рвёт TLS)."""
    return ["--noproxy", "*"] if windows_ie_proxy()["enabled"] else []


def _curl_head_ok(exe, url, timeout=20, extra=None):
    """Быстрая HEAD-проверка живости зеркала через curl (-I). True/False."""
    cmd = [exe, "-I", "-s", "-o", os.devnull, "--http1.1",
           "--connect-timeout", str(max(timeout // 3, 8)),
           "--max-time", str(timeout),
           "--user-agent", CURL_BROWSER_UA] \
        + _curl_noproxy_args() + list(extra or []) + [url]
    try:
        r = subprocess.run(cmd, check=False, timeout=timeout + 15,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        # rc==0 => HTTP < 400 (curl -I считает 4xx/5xx ошибкой только с --fail;
        # без --fail rc=0 даже для 404, поэтому проверяем вывод заголовков)
        if r.returncode != 0:
            return False
        # без --fail код ответа надо смотреть в заголовках — делаем второй
        # проход с --write-out (дёшево, HEAD уже прогрел соединение)
        cmd2 = [exe, "-I", "-s", "--http1.1",
                "--connect-timeout", str(max(timeout // 3, 8)),
                "--max-time", str(timeout),
                "--user-agent", CURL_BROWSER_UA,
                "--write-out", "%{http_code}", "--output", os.devnull] \
            + _curl_noproxy_args() + list(extra or []) + [url]
        r2 = subprocess.run(cmd2, check=False, timeout=timeout + 15,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
        code = (r2.stdout or b"").decode(errors="ignore").strip()
        return code.isdigit() and int(code) < 400
    except Exception as e:
        debug.dbg(f"curl -I {url}: {e}", level="WARN")
        return False


def _curl_head_rc(exe, url, timeout=10, extra=None):
    """curl -I без --fail: возвращает returncode (0/35/6/28...), None при сбое.

    Нужен чтобы отличать TLS-облом HEAD (rc=35 — зеркало живо для GET'а,
    рвётся только рукопожатие на -I) от мёртвых rc=6 (DNS) / rc=7 / таймаута.
    """
    cmd = [exe, "-I", "-s", "-o", os.devnull, "--http1.1",
           "--connect-timeout", str(max(timeout // 3, 4)),
           "--max-time", str(timeout),
           "--user-agent", CURL_BROWSER_UA] \
        + _curl_noproxy_args() + list(extra or []) + [url]
    try:
        r = subprocess.run(cmd, check=False, timeout=timeout + 10,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        return r.returncode
    except Exception as e:
        debug.dbg(f"curl -I(rc) {url}: {e}", level="WARN")
        return None


def _curl_get_probe_ok(exe, url, timeout=15, extra=None):
    """Живость зеркала коротким GET'ом первых байт (--max-filesize).

    Некоторые фильтры рвут именно HEAD (-I), а GET проходит; HEAD-проба
    тогда ошибочно отправляла рабочее зеркало в конец очереди. Скачиваем
    максимум ~64 КБ и проверяем HTTP-код после редиректов.
    """
    cmd = [exe, "-sS", "-o", os.devnull, "--http1.1",
           "--location-trusted", "--proto-redir", "https",
           "--max-filesize", "65536",
           "--connect-timeout", str(max(timeout // 3, 4)),
           "--max-time", str(timeout),
           "--user-agent", CURL_BROWSER_UA,
           "-w", "%{http_code}"] \
        + _curl_noproxy_args() + list(extra or []) + [url]
    try:
        r = subprocess.run(cmd, check=False, timeout=timeout + 10,
                           stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL)
        out = (r.stdout or b"").decode("ascii", errors="ignore").strip()
        code = out.splitlines()[-1] if out else ""
        # --max-filesize отдаёт rc=63, когда файл больше лимита — это
        # как раз нормальный ответ живого зеркала (файл есть, он большой).
        if r.returncode in (0, 63) and code.isdigit() \
                and int(code) < 400:
            return True
    except Exception as e:
        debug.dbg(f"curl GET-probe {url}: {e}", level="WARN")
    return False


def probe_mirrors_with_curl(mirrors, budget_sec=25):
    """Проверяет живость зеркал быстрым curl -I и возвращает новый список.

    Смысл: на Windows curl — основной путь закачки, но раньше он слепо шёл
    по порядку очереди. Если первое зеркало зависает/постоянно RST'ится,
    пользователь наблюдал вечное «Пробую закачку через curl.exe: ...» вместо
    рабочего второго зеркала. Здесь каждое зеркало проверяется коротким
    HEAD-запросом (общий бюджет ~budget_sec секунд); живые ставятся в начало
    в исходном порядке, мёртвые — в конец. Ошибки проверки НЕ удаляют
    зеркало (пробовали же HEAD, а GET работает иначе) — просто отодвигают.

    Битый по TLS зеркало (rc=35 у Schannel) перепроверяется с --tlsv1.2:
    если так проходит — зеркало живое, просто требует TLS 1.2; ставим его
    в начало и запоминаем флаг, чтобы download_queue_with_curl передал
    фикс в саму закачку (_tls_fix_cache).
    """
    from modules.platform_utils import IS_WINDOWS as _IS_WIN
    if not _IS_WIN or len(mirrors) <= 1:
        return list(mirrors)
    exe = shutil_which_curl()
    if not exe:
        return list(mirrors)
    alive, dead = [], []
    deadline = time.time() + budget_sec
    for item in mirrors:
        name, url = item[0], item[1]
        if time.time() >= deadline:
            dead.append(item)   # бюджет истёк — остаток в конец очереди
            continue
        per = max(int(deadline - time.time()), 5)
        if _curl_head_ok(exe, url, timeout=min(15, per)):
            alive.append(item)
        elif _curl_head_ok(exe, url, timeout=min(15, per),
                           extra=["--tlsv1.2"]):
            debug.dbg(f"probe: {name} жив только с TLS 1.2")
            _tls_fix_cache[url] = ["--tlsv1.2"]
            alive.insert(0, item)
        else:
            # Сетевые фильтры (DPI/прокси) иногда рвут ТОЛЬКО HEAD-запросы,
            # а обычный GET через curl проходит нормально. Раньше такое
            # зеркало безоговорочно отодвигалось в конец, и рабочее зеркало
            # (например llaun.ch) оказывалось заведомо мёртвым — очередь
            # закачки перепробовала мусор первым. Теперь даём второй шанс:
            # если HEAD упал именно по TLS (rc=35), пробуем GET с фиксами
            # --tlsv1.2 / --http2; при успехе запоминаем рабочий фикс.
            tlsfix_ok = False
            if _curl_head_rc(exe, url, timeout=min(10, per)) == 35:
                for tlsfix in (["--tlsv1.2"], ["--http2"]):
                    if _curl_get_probe_ok(exe, url, timeout=min(15, per),
                                          extra=tlsfix):
                        debug.dbg(f"probe: {name} — HEAD рвётся TLS (rc=35), "
                                  f"GET жив с {tlsfix[0]}")
                        _tls_fix_cache[url] = list(tlsfix)
                        tlsfix_ok = True
                        break
            if tlsfix_ok:
                alive.append(item)
            else:
                debug.dbg(f"probe: {name} недоступен/медлит — отодвигаю")
                dead.append(item)
    return alive + dead


#: Ограничитель длины очереди сетевых попыток внутри одной команды.
#: Каждый заход на мёртвое/блокируемое зеркало стоит десятки секунд
#: (connect-timeout + ретраи TLS-фиксов), а в школьной сети RST/сброс
#: прилетает ВСЕМ доменам сразу. Без лимита одна команда `legacy`
#: превращалась в два подряд полных круга по всем зеркалам (~7 минут
#: молчания с простынёй ошибок) — см. legacy_try_queues().
MAX_CURL_QUEUE_ATTEMPTS = 8


def _curl_attempt_budget(mirrors):
    """Сколько curl-заходов реально имеет смысл сделать для этой очереди."""
    return min(len(mirrors), MAX_CURL_QUEUE_ATTEMPTS)


def run_curl_queues(queues, dest, min_bytes=1024,
                    validator=None, silent=False, allow_full=False):
    """Прогоняет curl.exe по НЕСКОЛЬКИМ очередям зеркал с общим лимитом.

    Лечит симптом «legacy качается бесконечно»: у Legacy Launcher две
    очереди (.jar-portable и .exe-установщик), и раньше они исполнялись
    ДРУГ ЗА ДРУГОМ целиком. В сети, которая рвёт все домены лаунчера
    (curl rc=35 + WinError 10054), это два полных круга по 6~8 зеркалам
    с таймаутами и TLS-ретраями — сотни секунд простыни «curl не смог»,
    после которых всё равно предлагалось ручное скачивание. Здесь общий
    бюджет попыток (MAX_CURL_QUEUE_ATTEMPTS) делится между очередями:
    первые заходы — в основной очереди, остаток — в запасной; лишние
    заходы НЕ выполняются, но живой файл находится так же быстро.

    queues    — список очередей, каждая [(имя, url), ...]; пустые — ок;
    validator — вызываемый валидатор скачанного файла (например
                _looks_like_jar); без него достаточно размера;
    silent    — не печатать прогресс.
    Возвращает dict-резюме: {"ok": bool, "url": str|None, "attempts": int,
    "budget": int, "truncated": bool, "mirrors_total": int}.
    """
    cleaned = [list(q) for q in queues if q]
    total = sum(len(q) for q in cleaned)
    # allow_full=True — поведение «как раньше»: каждая очередь прогоняется
    # целиком (для мест, где одна очередь уже является полным перебором и
    # второй круга всё равно нет). По умолчанию — жёсткий общий бюджет.
    budget = total if allow_full else min(total, MAX_CURL_QUEUE_ATTEMPTS)
    resume = {"ok": False, "url": None, "attempts": 0, "budget": budget,
              "truncated": budget < total, "mirrors_total": total}
    if not cleaned or budget <= 0:
        return resume

    def _valid(p):
        try:
            if not p.exists() or p.stat().st_size < max(min_bytes, 1024):
                return False
            if validator is not None:
                return bool(validator(p))
            return _downloaded_file_plausible(p)
        except OSError:
            return False

    exe = shutil_which_curl()
    per_queue = max(budget // len(cleaned), 1) if len(cleaned) > 1 else budget
    spent = 0
    for qi, queue in enumerate(cleaned):
        remaining = budget - spent
        if remaining <= 0:
            break
        # Хвост очереди не режем: запасное зеркало (например Yandex CDN,
        # отдающий .exe-установщик) обычно стоит последним элементом, и
        # обрезка по per_queue выбрасывала именно его — «запасной путь»
        # молча исчезал. Последняя очередь берётся вся, но упирается в
        # оставшийся бюджет внутри download_queue_with_curl.
        take = len(queue) if qi == len(cleaned) - 1 \
            else min(len(queue), per_queue)
        sub = queue[:take]
        if exe:
            got = download_queue_with_curl(sub, dest, min_bytes=min_bytes,
                                           validator=validator,
                                           silent=silent,
                                           max_attempts=remaining)
            spent += _curl_attempt_budget(sub)
        else:
            # curl.exe нет (Windows < 1803 или вырезан) — тот же обход
            # очередей штатным urllib (open_checked умеет raw-сокет).
            got = _python_queue_fallback(sub, dest, min_bytes, validator,
                                         silent)
            spent += len(sub)
        if got:
            resume.update(ok=True, url=got, attempts=spent)
            return resume
    resume["attempts"] = spent
    return resume


def _python_queue_fallback(mirrors, dest, min_bytes, validator, silent):
    """Закачка очереди зеркал чистым Python (когда curl.exe недоступен)."""
    for name, url in mirrors:
        if not silent:
            info(f"Пробую закачку через Python: {CYAN}{name}{RESET}")
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": CURL_BROWSER_UA})
            r = open_checked(req, CONNECT_TIMEOUT)
        except Exception as e:
            debug.dbg(f"python-fallback {name}: {e}", level="WARN")
            if not silent:
                err(f"  Не подключиться: {e}")
            continue
        try:
            if not save_response(r, dest, name, silent=silent):
                continue
        finally:
            try:
                r.close()
            except Exception:
                pass
        try:
            if dest.stat().st_size >= max(min_bytes, 1024) \
                    and (validator(dest) if validator
                         else _downloaded_file_plausible(dest)):
                ok(f"  Готово (Python): "
                   f"{dest.stat().st_size / 1024 / 1024:.1f} МБ")
                return url
        except OSError:
            pass
    return None


def download_queue_with_curl(mirrors, dest, min_bytes=1024, silent=False,
                             validator=None, max_attempts=None):
    """Прогоняет curl.exe по ВСЕЙ очереди зеркал.

    В школьных сетях DPI рвёт TLS-рукопожатие Python (WinError 10054) на
    всех доменах сразу, а curl/Schannel идёт через системное хранилище
    сертификатов и прокси — как браузер. Возвращает URL зеркала, с которого
    файл реально скачан (размер >= min_bytes), либо None. Используется как
    ПЕРВЫЙ путь на Windows: питоновские попытки при RST-блокировке только
    жгут время и выводят пользователя из себя длинными списками ошибок.

    Перед прогоном проверяем настройки Windows: включённый прокси замедляет
    закачку и ломает TLS — предупреждаем и обходим его (--noproxy).
    Затем очередь переупорядочивается быстрым HEAD-пробом
    (probe_mirrors_with_curl), чтобы зависшее зеркало не блокировалось
    долгими таймаутами раньше живого.

    validator   — дополнительная проверка содержимого скачанного файла
                  (например _looks_like_jar для Legacy .jar). Раньше
                  считалось достаточным «размер >= min_bytes», поэтому
                  официальный CDN, отдающий вместо portable .jar свой
                  .exe-установщик, проходил как успех, а следующий этап
                  (_looks_like_jar) этот файл отвергал — и весь круг
                  загрузки повторялся заново уже другим кодом. Теперь
                  неподходящий файл честно помечается неверным и очередь
                  продолжается/завершается сразу.
    max_attempts — сколько зеркал успеть прогнать (бюджет времени;
                  см. run_curl_queues).
    """
    if not shutil_which_curl():
        return None
    warn_if_windows_proxy()
    mirrors = probe_mirrors_with_curl(mirrors)
    if max_attempts is not None:
        mirrors = mirrors[:max(max_attempts, 0)]
    for name, url in mirrors:
        try:
            if dest.exists() and dest.stat().st_size >= max(min_bytes, 1024):
                # Раньше здесь был просто «размер подошёл -> успех». На
                # gh-proxy-зеркала это означало: HTML-заглушка > min_bytes
                # засчитывалась скачанной, и Prism «успешно устанавливался»
                # битым. Проверяем содержимое; мусор удаляем и идём дальше.
                content_ok = (_downloaded_file_plausible(dest)
                              and (validator(dest) if validator else True))
                if content_ok:
                    return url
                warn(f"  {name}: в кэше мусор (HTML/битый архив), "
                     f"перекачиваю")
                try:
                    dest.unlink()
                except OSError:
                    pass
        except OSError:
            pass
        if not silent:
            info(f"Пробую закачку через curl.exe: {CYAN}{name}{RESET}")
        timeout = CONNECT_TIMEOUT * 4
        if download_via_curl(url, dest, timeout=timeout,
                              extra=_tls_fix_cache.get(url)):
            try:
                if dest.stat().st_size >= max(min_bytes, 1024):
                    if validator is not None and not validator(dest):
                        # Формально скачалось, но это НЕ тот артефакт,
                        # который мы просили (CDN отдал .exe вместо .jar).
                        # Молча возвращать успех нельзя — следующий этап
                        # отвергнет файл и качает всё по кругу.
                        warn("  curl скачал файл, но он не проходит "
                             f"проверку содержимого ({name}) — пропускаю")
                        try:
                            dest.unlink()
                        except OSError:
                            pass
                        continue
                    ok(f"  Готово (curl): "
                       f"{dest.stat().st_size / 1024 / 1024:.1f} МБ")
                    return url
                warn("  curl скачал слишком мало — пробую следующее зеркало")
            except OSError:
                pass
    return None


def shutil_which_curl():
    import shutil
    p = shutil.which("curl") or shutil.which("curl.exe")
    if p and Path(p).exists():
        return p
    # стандартное место в Windows 10 1803+
    sys32 = Path(os.environ.get("SystemRoot", r"C:\\Windows")) / "System32" / "curl.exe"
    return str(sys32) if sys32.exists() else None


def _is_cert_error(e):
    """True, если ошибка — про верификацию цепочки сертификатов."""
    if isinstance(e, ssl.SSLCertVerificationError):
        return True
    s = str(e).lower()
    return ("certificate verify failed" in s
            or "self signed certificate" in s
            or "unable to get local issuer certificate" in s)


def _is_resetlike(e):
    """Reset/обрыв соединения (в т.ч. внутри URLError)."""
    if isinstance(e, (ConnectionResetError, BrokenPipeError, socket.timeout,
                      http.client.RemoteDisconnected)):
        return True
    r = getattr(e, "reason", None)
    if isinstance(r, (ConnectionResetError, BrokenPipeError, socket.timeout,
                      http.client.RemoteDisconnected)):
        return True
    s = str(e).lower()
    return "10054" in s or "reset by peer" in s or "timed out" in s


def open_checked(req, timeout):
    """urlopen с единым (строгим) SSL-контекстом net.ssl_ctx().

    Проверка сертификатов строгая всегда: стандартные CA + системные
    бандлы RED OS (корни Минцифры/школьного MITM-прокси, если они
    установлены в доверие системы). Автоматический фолбек на CERT_NONE
    убран как дыра в безопасности; вместо него при SSL ошибках печатается
    понятная подсказка. Insecure-режим включается ТОЛЬКО явно:
    WI_INSECURE_SSL=1 (тогда net.ssl_ctx сам строит CERT_NONE-контекст).

    Если обычное соединение разорвано (WinError 10054 / ECONNRESET /
    RemoteDisconnected — так ведут себя CDN против «небраузерных» клиентов),
    автоматически делается вторая попытка: браузерные заголовки + прямой
    ssl-сокет без ALPN (_open_raw).
    """
    _browser_headers(req)
    try:
        return urllib.request.urlopen(req, context=net.ssl_ctx(),
                                      timeout=timeout)
    except (ConnectionResetError, http.client.RemoteDisconnected,
            BrokenPipeError, socket.timeout) as e:
        debug.dbg(f"open-checked reset ({e!r}), пробую raw-сокет: "
                  f"{req.full_url}", level="WARN")
        try:
            return _open_raw(req, timeout)
        except Exception as e2:
            raise urllib.error.URLError(e2) from e2
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        # Сброс соединения, завёрнутый urllib в URLError (на Windows это
        # типично выглядит как WinError 10054) — тоже уводим в raw-сокет.
        if isinstance(reason, (ConnectionResetError, BrokenPipeError,
                               socket.timeout)) or _is_resetlike(e):
            debug.dbg(f"open-checked reset ({e!r}), пробую raw-сокет: "
                      f"{req.full_url}", level="WARN")
            try:
                return _open_raw(req, timeout)
            except Exception as e2:
                raise urllib.error.URLError(e2) from e2
        cert_bad = isinstance(reason, ssl.SSLCertVerificationError) or \
            "CERTIFICATE_VERIFY_FAILED" in str(reason)
        if cert_bad:
            from modules.platform_utils import IS_WINDOWS
            if IS_WINDOWS:
                # На Windows Python 3.12+ использует OpenSSL и НЕ видит
                # системное хранилище сертификатов Windows — любое HTTPS
                # падает с CERTIFICATE_VERIFY_FAILED, даже когда браузер
                # всё открывает нормально. Подхватываем корни из реестра
                # Windows через ssl.create_default_context(cafile=
                # 'winstore:///ROOT') (Python 3.12+) либо certifi.
                hint("SSL: сертификат не прошёл проверку — пробую системное "
                     "хранилище Windows / certifi...")
                ctx = _windows_ctx()
                if ctx is not None:
                    try:
                        return urllib.request.urlopen(req, context=ctx,
                                                      timeout=timeout)
                    except urllib.error.URLError:
                        pass  # падаем в стандартное сообщение ниже
            if not _insecure_fallback_allowed():
                warn(f"SSL: сертификат не прошёл проверку ({getattr(reason, 'verify_message', reason)}). "
                     "Фолбек без проверки сертификатов ОТКЛЮЧён по соображениям безопасности. "
                     "Если сеть использует MITM-прокси — добавьте его корень "
                     "в доверие системы; крайний вариант: WI_INSECURE_SSL=1 (небезопасно).")
                debug.dbg(f"open-checked-cert-error (no fallback): {req.full_url}", level="WARN")
        raise


def _windows_ctx():
    """SSL-контекст с корнями из хранилища Windows (или certifi)."""
    for kwargs in ({"cafile": "winstore:///ROOT"}, {}):
        try:
            ctx = ssl.create_default_context(**kwargs)
            return ctx
        except Exception as e:
            debug.dbg(f"_windows_ctx({kwargs}): {e}", level="WARN")
    try:  # последний разумный фолбек — пакет certifi (строгий!)
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception as e:
        debug.dbg(f"_windows_ctx/certifi: {e}", level="WARN")
    return None


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
    except urllib.error.URLError as e:
        # HTTPError — подкласс URLError, у него есть .code; у обычного
        # URLError (DNS, reset, timeout) кода нет — берём через getattr.
        code = getattr(e, "code", None)
        if isinstance(getattr(e, "reason", None), ConnectionResetError) \
                or _is_resetlike(e):
            # open_checked уже сделал raw-попытку и она тоже упала в reset —
            # ещё один заход с тем же URL иногда проходит (CDN-флэпы).
            debug.dbg(f"reset на {name}, повторяю запрос")
            time.sleep(1)
            try:
                req = urllib.request.Request(url, headers=headers)
                r = open_checked(req, CONNECT_TIMEOUT)
            except Exception as e2:
                debug.dbg_exc(e2, f"connect-retry/{name}")
                if not silent:
                    err(f"  Не подключиться: {e2}")
                return False
        elif code == 416 and existing > 0:
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
        # Сюда долетают НЕ-mock-ошибки (на Windows — OSError с WinError
        # 10054/10061 внутри), поэтому и здесь нужен raw-фолбек, иначе
        # «Не подключиться» печатается до всякой второй попытки.
        if _is_resetlike(e):
            debug.dbg(f"reset на {name} (не-URLError), пробую raw-сокет")
            try:
                req = urllib.request.Request(url, headers=headers)
                r = _open_raw(_browser_headers(req), CONNECT_TIMEOUT)
            except Exception as e2:
                debug.dbg_exc(e2, f"connect-raw/{name}")
                if not silent:
                    err(f"  Не подключиться: {e2}")
                return False
        else:
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
def download_file(mirrors, dest, label, min_size_mb=10, silent=False,
                  exact_size=None):
    WINE_DIR.mkdir(parents=True, exist_ok=True)
    # Проверка прокси Windows ПЕРЕД любой загрузкой: включённый прокси —
    # частая причина «долго думает, но качает» (буферизация) и TLS-ошибок.
    from modules.platform_utils import IS_WINDOWS as _IS_WIN_ALL
    if _IS_WIN_ALL:
        warn_if_windows_proxy()
    # min_size_mb=0 раньше пропускал любую закачку, включая пустые/обрубки:
    # `st_size >= 0` истинно для любого файла. Ставим нижний порог 1 КБ —
    # реальные артефакты (jar, mrpack, tar.gz) всё равно больше.
    min_size = max(min_size_mb * 1024 * 1024, 1024)

    def _size_ok(p):
        """exact_size (байт) — жёсткая проверка для больших бинарников:
        HTML-заглушка или мусор от прокси-зеркала могут быть больше порога
        min_size_mb; точный размер отсекает их надёжно."""
        if exact_size:
            return p.stat().st_size == exact_size
        return p.stat().st_size >= min_size

    # Ранний выход: файл уже скачан полностью. Для бинарных архивов
    # (jar/zip/tar.gz/mrpack) проверяем «магические» байты — иначе
    # HTML-заглушка от прокси, лежащая в кэше, считалась бы успешной
    # закачкой и ломала установку на этапе распаковки.
    if dest.exists() and _size_ok(dest) and _looks_binary(dest):
        if not silent:
            info(f"{label}: уже скачан ({dest.stat().st_size / 1024 / 1024:.1f} МБ)")
        return True
    if dest.exists() and not _looks_binary(dest):
        debug.dbg(f"download_file: {dest.name} — не бинарник (HTML?), перекачиваю")

    if not silent:
        info(f"Скачиваю {label}...\n")
    mirrors_sorted = sort_mirrors_by_cache(mirrors)
    # Windows: сначала curl.exe по всей очереди. В сетях с RST-блокировкой
    # Python-TLS питоновские попытки падают на ВСЕХ зеркалах и печатают
    # простыню ошибок, а curl (Schannel/системный прокси) качает как браузер.
    # Если curl нет или не смог — идем дальше стандартным путем (Python).
    from modules.platform_utils import IS_WINDOWS as _IS_WIN_EARLY
    if _IS_WIN_EARLY and (not dest.exists() or not _size_ok(dest)):
        dl_url = download_queue_with_curl(mirrors_sorted, dest,
                                          min_bytes=min_size, silent=silent)
        if dl_url and dest.exists() and _size_ok(dest) \
                and _looks_binary(dest):
            return True
        if dest.exists() and not _looks_binary(dest):
            try:
                dest.unlink()
            except Exception:
                pass
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
            if result is True and dest.exists() and _size_ok(dest) \
                    and _looks_binary(dest):
                elapsed = time.time() - start_time
                if elapsed > 0:
                    remember_mirror_speed(name, (dest.stat().st_size / 1024) / elapsed)
                return True
    for name, url in mirrors_sorted:
        if try_download_manual(name, url, dest, silent=silent):
            if dest.exists() and _size_ok(dest) and _looks_binary(dest):
                return True
            if dest.exists() and not _looks_binary(dest):
                # зеркало отдало HTML вместо бинарника — чистим и идём дальше
                debug.dbg(f"download_file: зеркало {name} отдало HTML, удаляю {dest.name}")
                try:
                    dest.unlink()
                except Exception as e:
                    debug.dbg_exc(e, "download")
    # Все зеркала упали по сети (типично для школьных фильтров: RST на весь
    # TLS Python). Последние две попытки идут в обход питоновского ssl:
    #  A) через системный прокси Windows (реестр/IE — браузер же качает!);
    #  B) через штатный curl.exe (Schannel + системное хранилище сертификатов).
    from modules.platform_utils import IS_WINDOWS as _IS_WIN
    for name, url in mirrors_sorted[:2]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            r = open_system_proxy(req, CONNECT_TIMEOUT)
        except Exception as e:
            debug.dbg(f"proxy attempt {name}: {e}", level="WARN")
            r = None
        if r is not None:
            if not silent:
                info(f"Пробую зеркало через системный прокси: {CYAN}{name}{RESET}")
            if save_response(r, dest, name, silent=silent):
                if dest.exists() and _size_ok(dest) \
                        and _looks_binary(dest):
                    return True
                try:
                    dest.unlink()
                except Exception:
                    pass
        if _IS_WIN:
            if not silent:
                info(f"Пробую закачку через curl.exe: {CYAN}{name}{RESET}")
            if download_via_curl(url, dest, extra=_tls_fix_cache.get(url)) and dest.exists() \
                    and _size_ok(dest) and _looks_binary(dest):
                ok(f"  Готово (curl): {dest.stat().st_size / 1024 / 1024:.1f} МБ")
                return True
    # Раньше curl-фолбек применялся только к первым двум зеркалам очереди.
    # На практике в школьной сети RST прилетает ВСЕМ Python-запросам, а
    # curl/Schannel проходит — но именно на зеркала дальше второй позиции
    # (например, gh-proxy.com для Prism или archive.org для Legacy). Теперь
    # прогоняем curl по всей очереди; как только файл скачан корректно —
    # выходим. Ошибка одного URL не должна обрывать всю установку.
    if _IS_WIN:
        for name, url in mirrors_sorted[2:]:
            if dest.exists() and _size_ok(dest) and _looks_binary(dest):
                return True
            if not silent:
                info(f"Пробую закачку через curl.exe: {CYAN}{name}{RESET}")
            if download_via_curl(url, dest, extra=_tls_fix_cache.get(url)) and dest.exists() \
                    and _size_ok(dest) and _looks_binary(dest):
                ok(f"  Готово (curl): {dest.stat().st_size / 1024 / 1024:.1f} МБ")
                return True
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


# ═══════════════════════════════════════════════════════════════════
#  ДИАГНОСТИКА СЕТИ (netcheck)
#  Лечит симптом «похоже, что у ПК выключен интернет, хотя он включён»:
#  когда Legacy Launcher показывает «Список недоступных доменов»
#  (account.ely.by / launchermeta.mojang.com / proxy.legacylauncher.ru /
#  proxy.llaun.ch), чаще всего дело не в отсутствии интернета, а в
#  битом/перехваченном DNS провайдера или в DPI-блокировке конкретных
#  доменов. Функции ниже это различают.
# ═══════════════════════════════════════════════════════════════════

#: Домены, которые сам лаунчер Java обычно не может достать в проблемных
#: сетях — их список пользователь видит в окне «Список недоступных доменов».
NETCHECK_DOMAINS = [
    "account.ely.by",
    "launchermeta.mojang.com",
    "piston-meta.mojang.com",
    "proxy.legacylauncher.ru",
    "proxy.llaun.ch",
    "llaun.ch",
]

#: Публичные DNS для контрольного резолвинга (Яндекс и Google).
PUBLIC_DNS = [("77.88.8.8", "Яндекс DNS"), ("8.8.8.8", "Google DNS")]


def _dns_query_a(hostname, server, timeout=4):
    """Минимальный DNS A-запрос (прямые сокеты, без внешних зависимостей).

    Возвращает список IPv4-адресов; при таймауте/битом ответе — пустой
    список. Нужен, чтобы сравнить: домен резолвится через системный DNS
    или только через публичный (признак перехвата/блокировки провайдером).
    """
    import struct
    try:
        labels = b"".join(bytes([len(p)]) + p.encode("ascii", "ignore")
                          for p in hostname.rstrip(".").split(".")) + b"\x00"
        # заголовок: id, flags (standard query, RD=1), QDCOUNT=1, остальные 0
        header = struct.pack(">HHHHHH", 0xBEEF, 0x0100, 1, 0, 0, 0)
        query = header + labels + struct.pack(">HH", 1, 1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(query, (server, 53))
            data, _ = sock.recvfrom(4096)
        finally:
            sock.close()
        if len(data) < 12:
            return []
        qdcount, ancount = struct.unpack(">HH", data[4:8])
        # пропускаем заголовок и вопросы
        pos = 12
        for _ in range(qdcount):
            while pos < len(data):
                ln = data[pos]
                if ln == 0:
                    pos += 1
                    break
                if ln & 0xC0:          # сжатое имя — указатель 2 байта
                    pos += 2
                    break
                pos += 1 + ln
            pos += 4                   # QTYPE + QCLASS
        ips = []
        for _ in range(ancount):
            if pos + 10 > len(data):
                break
            # имя (сжатое или метки)
            if data[pos] & 0xC0:
                pos += 2
            else:
                while pos < len(data) and data[pos]:
                    if data[pos] & 0xC0:
                        pos += 2
                        break
                    pos += 1 + data[pos]
                pos += 1
            rtype, rclass, _ttl, rdlen = struct.unpack(">HHIH",
                                                       data[pos:pos + 10])
            pos += 10
            rdata = data[pos:pos + rdlen]
            pos += rdlen
            if rtype == 1 and rclass == 1 and rdlen == 4:
                ips.append(".".join(str(b) for b in rdata))
        return ips
    except Exception as e:
        debug.dbg(f"dns_query({hostname}@{server}): {e}", level="WARN")
        return []


def diagnose_dns(domains=None):
    """Сравнивает системный DNS с публичным по списку доменов.

    Возвращает dict: {domain: {"system": [ip...], "public": {server: [ip...]},
    "verdict": str}}. verdict:
      ok            — резолвится везде более-менее одинаково
      dns_block     — системный DNS не отдаёт ничего/LOPHIBUR, публичный — да
                      (перехват/блокировка провайдером)
      no_internet   — не резолвится НИЧЕГДА, даже через 8.8.8.8
    """
    domains = domains or NETCHECK_DOMAINS
    results = {}
    for dom in domains:
        entry = {"system": [], "public": {}, "verdict": ""}
        try:
            infos = socket.getaddrinfo(dom, None, socket.AF_INET)
            entry["system"] = sorted({i[4][0] for i in infos})
        except Exception as e:
            debug.dbg(f"system dns {dom}: {e}", level="WARN")
        for server, _name in PUBLIC_DNS:
            entry["public"][server] = _dns_query_a(dom, server)
        pub_ok = any(entry["public"].values())
        sys_ip = entry["system"]
        sys_lophi = bool(sys_ip) and all(ip.startswith("127.")
                                         or ip.startswith("128.0.101.")
                                         for ip in sys_ip)
        if not sys_ip or sys_lophi:
            entry["verdict"] = "dns_block" if pub_ok else "no_internet"
        else:
            entry["verdict"] = "ok"
        results[dom] = entry
    return results


def check_reachable(url, timeout=8):
    """Быстрая HEAD/GET-проверка доступности HTTPS-URL. True/False."""
    try:
        req = urllib.request.Request(
            url, method="HEAD",
            headers={"User-Agent": CURL_BROWSER_UA})
        with open_checked(req, timeout) as r:
            return 200 <= getattr(r, "status", 200) < 400
    except Exception as e:
        debug.dbg(f"reachable {url}: {e}", level="WARN")
        return False


def cmd_netcheck():
    """Команда netcheck: диагностика «интернет есть, а домены недоступны»."""
    info("=== Диагностика сети (netcheck) ===")
    res = diagnose_dns()
    blocked, dead = [], []
    for dom, entry in res.items():
        mark = {"ok": "✅", "dns_block": "🚫", "no_internet": "❌"}[entry["verdict"]]
        sysv = ", ".join(entry["system"]) or "—(не резолвится)"
        print(f"  {mark} {dom:28} system: {sysv}")
        if entry["verdict"] == "dns_block":
            blocked.append(dom)
            good = next((ips for ips in entry["public"].values() if ips), [])
            print(f"      ⚠️  перехвачен/заблокирован системным DNS; "
                  f"через публичный: {', '.join(good)}")
        elif entry["verdict"] == "no_internet":
            dead.append(dom)
    if blocked:
        warn(f"DNS-перехват затронул {len(blocked)} домен(ов): "
             f"{', '.join(blocked)}")
        hint("Решение: поставь DNS 77.88.8.8 / 8.8.8.8 "
             "(Параметры сети → IPv4 → использовать следующие серверы)")
    if dead and len(dead) == len(res):
        err("Похоже, интернет действительно отсутствует "
            "(не резолвятся даже через 8.8.8.8).")
        hint("Проверь кабель/Wi-Fi, роутер и что другие сайты открываются.")
    elif not blocked and not dead:
        ok("DNS работает корректно — все домены резолвятся.")
    # Проверка реальной доступности части сервисов (HTTPS)
    for url, name in (("https://launchermeta.mojang.com/mc/game/"
                       "version_manifest.json", "Mojang meta"),
                      ("https://llaun.ch/jar", "llaun.ch")):
        state_txt = "доступен" if check_reachable(url) else "НЕДОСТУПЕН"
        print(f"  {'✅' if state_txt == 'доступен' else '⚠️ '} {name:28} {state_txt}")
    info("Если здесь всё зелёное, а Legacy Launcher пишет «недоступные "
         "домены» — блокируется сама Java (брандмауэр/антивирус/DPI): "
         "разреши javaw.exe в брандмауэре Windows.")

