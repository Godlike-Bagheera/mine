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


def _run_curl(exe, url, tmp, timeout, extra=None):
    """Один запуск curl.exe -> tmp. Возвращает (True|False, текст_ошибки)."""
    cmd = [exe, "--fail", "--location-trusted", "--http1.1",
           "--retry", "2", "--retry-delay", "1",
           "--connect-timeout", str(timeout // 4 or 15),
           "--max-time", str(timeout),
           "--user-agent", CURL_BROWSER_UA,
           "--output", str(tmp)]
    if extra:
        cmd += list(extra)
    cmd.append(url)
    debug.dbg(f"curl: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, check=False, timeout=timeout + 90,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
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


def download_via_curl(url, dest, timeout=CONNECT_TIMEOUT * 4):
    """Запасная закачка системной утилитой curl (есть в Windows 10 1803+).

    curl.exe использует Schannel — системное хранилище сертификатов и, если
    он настроен, системный прокси, поэтому проходит там, где питоновский
    ssl падает (MITM-корень школы, RST на TLS-фингерпринт Python).
    True — файл скачан (размер проверяет вызывающий код).

    Варианты запуска (первый же успех прерывает цикл):
      1) как есть — curl сам подхватит системный прокси из реестра;
      2) --noproxy "*" — если корпоративный прокси мешает прямой закачке;
      3) insecure — только при WI_INSECURE_SSL=1 (школа с MITM-сертификатом);
      4) wget.exe — если вдруг curl отсутствует, но есть wget в PATH.
    """
    exe = shutil_which_curl()
    if not exe:
        return _download_via_wget(url, dest, timeout)
    tmp = Path(str(dest) + ".curlpart")
    attempts = [[], ["--noproxy", "*"]]
    if _insecure_fallback_allowed():
        attempts.append(["--insecure"])
        warn("curl: нестрогая проверка сертификатов "
             "(WI_INSECURE_SSL=1 включён пользователем)")
    last_err = ""
    for extra in attempts:
        ok_, errtxt = _run_curl(exe, url, tmp, timeout, extra)
        if ok_:
            os.replace(tmp, dest)
            return True
        last_err = errtxt
        debug.dbg(f"curl attempt failed ({extra}): {errtxt}", level="WARN")
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


def _curl_head_ok(exe, url, timeout=20):
    """Быстрая HEAD-проверка живости зеркала через curl (-I). True/False."""
    cmd = [exe, "-I", "-s", "-o", os.devnull, "--http1.1",
           "--connect-timeout", str(max(timeout // 3, 8)),
           "--max-time", str(timeout),
           "--user-agent", CURL_BROWSER_UA, url]
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
                "--write-out", "%{http_code}", "--output", os.devnull, url]
        r2 = subprocess.run(cmd2, check=False, timeout=timeout + 15,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
        code = (r2.stdout or b"").decode(errors="ignore").strip()
        return code.isdigit() and int(code) < 400
    except Exception as e:
        debug.dbg(f"curl -I {url}: {e}", level="WARN")
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
        else:
            debug.dbg(f"probe: {name} недоступен/медлит — отодвигаю")
            dead.append(item)
    return alive + dead


def download_queue_with_curl(mirrors, dest, min_bytes=1024, silent=False):
    """Прогоняет curl.exe по ВСЕЙ очереди зеркал.

    В школьных сетях DPI рвёт TLS-рукопожатие Python (WinError 10054) на
    всех доменах сразу, а curl/Schannel идёт через системное хранилище
    сертификатов и прокси — как браузер. Возвращает URL зеркала, с которого
    файл реально скачан (размер >= min_bytes), либо None. Используется как
    ПЕРВЫЙ путь на Windows: питоновские попытки при RST-блокировке только
    жгут время и выводят пользователя из себя длинными списками ошибок.

    Перед прогоном очередь переупорядочивается быстрым HEAD-пробом
    (probe_mirrors_with_curl), чтобы зависшее зеркало не блокировалось
    долгими таймаутами раньше живого.
    """
    if not shutil_which_curl():
        return None
    mirrors = probe_mirrors_with_curl(mirrors)
    for name, url in mirrors:
        try:
            if dest.exists() and dest.stat().st_size >= max(min_bytes, 1024):
                return url
        except OSError:
            pass
        if not silent:
            info(f"Пробую закачку через curl.exe: {CYAN}{name}{RESET}")
        timeout = CONNECT_TIMEOUT * 4
        if download_via_curl(url, dest, timeout=timeout):
            try:
                if dest.stat().st_size >= max(min_bytes, 1024):
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
            if download_via_curl(url, dest) and dest.exists() \
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
            if download_via_curl(url, dest) and dest.exists() \
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
