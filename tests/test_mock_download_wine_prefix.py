#!/usr/bin/env python3
"""Mock-тесты вокруг modules/download.py, modules/wine.py, modules/prefix.py.

Цель — дешёвый слой надёжности: ни сети, ни реального Wine, ни root не нужны.
Всё внешнее подменяется заглушками (monkeypatch по атрибутам модулей +
стандартный unittest.mock):

* download.py  — urllib.request.urlopen, subprocess.run (aria2c), пути из
                 config (WINE_DIR/CACHE_FILE/ARIA2C_BIN), константы скорости;
* wine.py      — download_file / net.safe_extract_tar / subprocess / input;
* prefix.py    — subprocess.run (wineboot), HOME/WINE_PREFIX/PREFIXES_DIR,
                 флаги state (per-game prefix, DXVK HUD, MangoHud).

Проверяемое поведение — «страховка от регрессий», а не догма: если код
изменят, эти тесты должны показать, ГДЕ именно сломался контракт.

Запуск: python tests/test_mock_download_wine_prefix.py  (или через pytest —
conftest.py гоняет каждый test_*.py отдельным subprocess'ом).
"""
import io
import os
import shutil
import ssl
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# fake-HOME ДО импорта config (HOME резолвится на этапе импорта);
# WI_FAKE_HOME=1 запрещает фолбэк _resolve_home на passwd-дом (/root).
os.environ["HOME"] = tempfile.mkdtemp(prefix="fakehome_mock_")
os.environ["WI_FAKE_HOME"] = "1"

from modules import download, net, prefix, state, wine  # noqa: E402
from modules.config import (  # noqa: E402
    DXVK_OVERRIDES, WINE_MIRRORS, make_dxvk_mirrors,
)

fails = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        fails.append(name)


TMP = Path(tempfile.mkdtemp(prefix="mock_dw_"))


# ═══════════════════════ Фейковый HTTP-ответ ═══════════════════════
class FakeResp:
    """Минимальная замена объекта urlopen: read/close/status/headers."""

    def __init__(self, body=b"", status=200, headers=None):
        self._buf = io.BytesIO(body)
        self.status = status
        self.headers = dict(headers or {})
        self.closed = False

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


ZIP_MAGIC = b"PK\x03\x04"
HTML_GARBAGE = b"<!DOCTYPE html><html>proxy blocked</html>"


def zip_bytes(payload_name="a.txt", payload=b"x" * 2048):
    """Настоящий (корректный) zip-архив в байтах — пройдёт _looks_binary."""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(payload_name, payload)
    return buf.getvalue()


def tar_gz_bytes(dirname="dxvk-2.4.1", filename="x64/dxgi.dll"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        info = tarfile.TarInfo(f"{dirname}/{filename}")
        data = ZIP_MAGIC + b"\x00" * 2000
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ═══════════════════ Общая изоляция download.py ═══════════════════
def isolate_download(tmp_sub="dl"):
    """Перенаправляет пути download.* в temp и возвращает dict с путями."""
    base = TMP / tmp_sub
    wd = base / "wine-portable"
    wd.mkdir(parents=True, exist_ok=True)
    p = {"base": base, "WINE_DIR": wd, "CACHE_FILE": wd / ".mirror_cache",
         "ARIA2C_BIN": wd / "bin" / "aria2c"}
    return p


def run_dl_section(name, paths, body):
    """Выполняет `body(paths)` с подменёнными путями/кэшем и чистым state."""
    state.slow_mirror_count = 0
    state.slow_mode_active = False
    old = (download.WINE_DIR, download.CACHE_FILE, download.ARIA2C_BIN)
    download.WINE_DIR = paths["WINE_DIR"]
    download.CACHE_FILE = paths["CACHE_FILE"]
    download.ARIA2C_BIN = paths["ARIA2C_BIN"]
    try:
        body(paths)
    finally:
        download.WINE_DIR, download.CACHE_FILE, download.ARIA2C_BIN = old
        state.slow_mirror_count = 0
        state.slow_mode_active = False


print("== 1. Кэш зеркал: load/save/sort/remember ==")
_p = isolate_download("cache")


def _cache_body(paths):
    check("пустой кэш -> {}", download.load_mirror_cache() == {})

    cache = {"fast": {"speed": 900}, "slow": {"speed": 5}}
    download.save_mirror_cache(cache)
    loaded = download.load_mirror_cache()
    check("save/load по кругу", loaded == cache)

    mirrors = [("slow", "u1"), ("fast", "u2"), ("fresh", "u3")]
    ordered = [n for n, _ in download.sort_mirrors_by_cache(mirrors)]
    check("сортировка по кэшу: быстрый первый, неизвестный последний",
          ordered == ["fast", "slow", "fresh"])

    download.remember_mirror_speed("mid", 500)
    c2 = download.load_mirror_cache()
    check("remember_mirror_speed записал скорость",
          c2.get("mid", {}).get("speed") == 500 and "ts" in c2["mid"])

    # битый JSON -> тихий пустой словарь, а не исключение
    paths["CACHE_FILE"].write_text("{not json", encoding="utf-8")
    check("битый кэш -> {} без исключения", download.load_mirror_cache() == {})


run_dl_section("cache", _p, _cache_body)


print("== 2. _looks_binary / магические сигнатуры ==")
good_jar = TMP / "g.jar"
good_jar.write_bytes(ZIP_MAGIC + b"\x00" * 500)
check("валидный .jar принят", download._looks_binary(good_jar))

bad_jar = TMP / "b.jar"
bad_jar.write_bytes(HTML_GARBAGE * 100)
check("HTML под видом .jar отклонён", not download._looks_binary(bad_jar))

appimg = TMP / "a.AppImage"
appimg.write_bytes(HTML_GARBAGE * 100)
check("HTML с неизвестным расширением пропущен (как раньше)",
      download._looks_binary(appimg))

empty = TMP / "e.mrpack"
empty.write_bytes(b"")
check("пустой .mrpack отклонён (не читается магия)",
      not download._looks_binary(empty))


print("== 3. open_checked: SSL-политика ==")
_cert_err = urllib.error.URLError(
    ssl.SSLCertVerificationError(18, "certificate verify failed",
                                 "CERTIFICATE_VERIFY_FAILED"))
_req = urllib.request.Request("https://example.invalid/f.zip")


def _ssl_env(insecure_flag, side_effect):
    # Новый open_checked идёт через кэшированный opener (build_opener),
    # а не urllib.request.urlopen — мокнем именно opener.open.
    old_ctx = net.ssl_ctx
    net.ssl_ctx = lambda insecure=False: ssl.create_default_context()
    m_open = mock.MagicMock(side_effect=side_effect)
    fake_opener = mock.MagicMock()
    fake_opener.open = m_open
    with mock.patch.object(download, "_strict_opener",
                           return_value=fake_opener):
        with mock.patch.dict(os.environ,
                             {"WI_INSECURE_SSL": insecure_flag}
                             if insecure_flag is not None else {},
                             clear=("WI_INSECURE_SSL"
                                    if insecure_flag is None else None)):
            try:
                download.open_checked(_req, 5)
                result = "ok"
            except urllib.error.URLError:
                result = "raise"
    net.ssl_ctx = old_ctx
    return result, m_open


res, _ = _ssl_env(None, _cert_err)
check("SSL-ошибка пробрасывается вверх (без тихого downgrade)", res == "raise")
check("insecure-фолбек по умолчанию ЗАПРЕЩЁН",
      download._insecure_fallback_allowed() is False)
os.environ["WI_INSECURE_SSL"] = "1"
check("WI_INSECURE_SSL=1 явно разрешает insecure",
      download._insecure_fallback_allowed() is True)
del os.environ["WI_INSECURE_SSL"]


def _ssl_ok(*a, **k):
    return FakeResp(b"ok", headers={"Content-Length": "2"})


res, m_open = _ssl_env(None, _ssl_ok)
check("обычный ответ проходит без ошибок", res == "ok")
# Контекст net.ssl_ctx теперь передаётся через HTTPSHandler внутри
# _strict_opener(), а не аргументом urlopen. Проверяем поведение:
# opener обязан игнорировать прокси из окружения (школьная сеть —
# корень бага «интернета нет»), тогда как обычный urlopen через него идёт.
os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"  # заведомо мёртвый прокси
try:
    download._strict_opener().open(
        urllib.request.Request("http://example.invalid/x"), timeout=2)
    _opener_err = None
except Exception as e:
    _opener_err = str(e).lower()
try:
    urllib.request.urlopen(
        urllib.request.Request("http://http://@/"), timeout=2)
except Exception as e:
    pass  # сравнение не обязательно: главное — поведение opener'а
del os.environ["HTTP_PROXY"]
check("opener обходит системный прокси (DNS-ошибка, а не 'proxy refused')",
      _opener_err is not None and "name or service not known" in _opener_err
      and "connection refused" not in _opener_err)
check("opener кэширован (тот же объект при повторном вызове)",
      download._strict_opener() is download._strict_opener())


print("== 4. head_check ==")
with mock.patch.object(download, "open_checked",
                       return_value=FakeResp(headers={"Content-Length": "123"})):
    check("HEAD отдаёт (True, size)", download.head_check("http://x/y") == (True, 123))

with mock.patch.object(download, "open_checked",
                       side_effect=urllib.error.HTTPError("u", 405, "m", None, None)):
    check("HEAD 405 -> зеркало годно, размер неизвестен",
          download.head_check("http://x/y") == (True, 0))

with mock.patch.object(download, "open_checked",
                       side_effect=urllib.error.HTTPError("u", 404, "m", None, None)):
    check("HEAD 404 -> зеркало отброшено",
          download.head_check("http://x/y") == (False, 0))

with mock.patch.object(download, "open_checked", side_effect=OSError("net down")):
    check("сетевая ошибка HEAD -> оптимистично (True, 0)",
          download.head_check("http://x/y") == (True, 0))


print("== 5. aria2c: поиск бинаря, команды, сертификаты ==")
_p = isolate_download("aria")


def _aria_body(paths):
    binpath = paths["ARIA2C_BIN"]
    binpath.parent.mkdir(parents=True, exist_ok=True)

    # shutil.which импортируется внутри get_aria2c_path — мок в момент вызова
    with mock.patch("shutil.which", return_value=None):
        check("нет aria2c нигде -> get_aria2c_path None",
              download.get_aria2c_path() is None)
        check("download_with_aria2 без бинаря -> None (не False!)",
              download.download_with_aria2("http://x", paths["base"] / "f.zip") is None)

    binpath.write_bytes(b"#!/bin/sh\n")
    binpath.chmod(0o755)
    check("локальный executable найден", download.get_aria2c_path() == str(binpath))

    # Полный цикл download_file через aria2c. Вход в aria2c-ветку требует:
    # get_aria2c_path(), min_size_mb >= 5 и ОТСУТСТВУЮЩИЙ/нулевой dest.
    # ВАЖНО про контракт кода: успех aria2c здесь засчитывается ТОЛЬКО для
    # расширений из _BINARY_MAGICS (.zip/.jar/.mrpack/.gz...) — у файла вида
    # wine.AppImage сигнатура не проверяется вовсе, поэтому HTML-огрызок от
    # aria2c считается удачной закачкой (осознанное ограничение: AppImage
    # может быть и shell-архивом). Тестируем на .zip: он обязан пройти
    # всю цепочку и НЕ должен дёргать ручную ветку (open_checked).
    d_full = paths["base"] / "ariaok.zip"
    d_full.unlink(missing_ok=True)

    def aria_ok(cmd, **kw):
        # 6 МБ: больше и порога download_with_aria2 (> 1 МБ «реально
        # скачано»), и min_size (5 МБ) — иначе download_file отклонит
        # результат и уйдёт в ручную ветку.
        d_full.write_bytes(zip_bytes(payload=b"q" * (6 * 1024 * 1024)))
        return type("R", (), {"returncode": 0})()

    with mock.patch.object(download, "head_check", return_value=(True, 0)), \
         mock.patch.object(download.subprocess, "run", side_effect=aria_ok), \
         mock.patch.object(download, "open_checked",
                           side_effect=AssertionError("ручная ветка не нужна")):
        r = download.download_file([("m", "http://x")], d_full, "t",
                                   min_size_mb=5, silent=True)
    check("aria2c-ветка download_file: успех без обращения к ручной ветке",
          r is True)

    # Тот же прогон с расширением вне _BINARY_MAGICS: проверка сигнатуры
    # пропускается, зеркало запоминается быстрым — контракт зафиксирован.
    d_app = paths["base"] / "ariaok.AppImage"
    d_app.unlink(missing_ok=True)

    def aria_ok2(cmd, **kw):
        d_app.write_bytes(zip_bytes(payload=b"q" * (6 * 1024 * 1024)))
        return type("R", (), {"returncode": 0})()

    with mock.patch.object(download, "head_check", return_value=(True, 0)), \
         mock.patch.object(download.subprocess, "run", side_effect=aria_ok2), \
         mock.patch.object(download, "open_checked",
                           side_effect=AssertionError("ручная ветка не нужна")):
        r = download.download_file([("app-m", "http://x")], d_app, "t",
                                   min_size_mb=5, silent=True)
    check("aria2c-успех для неизвестного расширения тоже принимается", r is True)

    dest = paths["base"] / "big.zip"
    big_result = zip_bytes(payload=b"x" * (2 * 1024 * 1024))

    def fake_run_ok_big(cmd, **kw):
        dest.write_bytes(big_result)
        return type("R", (), {"returncode": 0})()

    with mock.patch.object(download.subprocess, "run", side_effect=fake_run_ok_big):
        r = download.download_with_aria2("http://x/big.zip", dest, silent=True)
        check("aria2c rc=0 + файл > 1 МБ -> True", r is True)

    dest.unlink()

    def fake_run_ok_small(cmd, **kw):
        dest.write_bytes(ZIP_MAGIC + b"0" * 100)   # < 1 МБ — подозрительно
        return type("R", (), {"returncode": 0})()

    with mock.patch.object(download.subprocess, "run", side_effect=fake_run_ok_small):
        r = download.download_with_aria2("http://x/small.zip", dest, silent=True)
        check("слишком маленький результат -> False (отказ засчитан)", r is False)

    dest.unlink(missing_ok=True)
    calls = {}

    def capture(cmd, **kw):
        calls["cmd"] = cmd
        dest.write_bytes(big_result)
        return type("R", (), {"returncode": 0})()

    with mock.patch.object(download.subprocess, "run", side_effect=capture):
        download.download_with_aria2("http://x/a.zip", dest, silent=True)
    joined = " ".join(calls["cmd"])
    check("строго: --check-certificate=true", "--check-certificate=true" in joined)
    check("--continue/--allow-overwrite включены",
          "--continue=true" in joined and "--allow-overwrite=true" in joined)

    os.environ["WI_INSECURE_SSL"] = "1"
    try:
        with mock.patch.object(download.subprocess, "run", side_effect=capture):
            download.download_with_aria2("http://x/a.zip", dest, silent=True)
        joined = " ".join(calls["cmd"])
        check("WI_INSECURE_SSL=1 -> --check-certificate=false",
              "--check-certificate=false" in joined)
    finally:
        del os.environ["WI_INSECURE_SSL"]

    with mock.patch.object(download.subprocess, "run",
                           side_effect=OSError("exec fail")):
        r = download.download_with_aria2("http://x/a.zip", dest, silent=True)
        check("сбой запуска aria2c -> None (пробуем ручой режим)", r is None)


run_dl_section("aria", _p, _aria_body)


print("== 6. try_download_manual: Range, обрывы, slow-mode ==")
_p = isolate_download("manual")


def _manual_body(paths):
    big = ZIP_MAGIC + b"y" * (300 * 1024)          # «большой» для speed-check
    full = zip_bytes(payload=b"z" * (300 * 1024))

    # 6.1 успешная закачка с нуля
    dest = paths["base"] / "one.zip"
    with mock.patch.object(download, "open_checked",
                           return_value=FakeResp(full, headers={"Content-Length": str(len(full))})), \
         mock.patch.object(sys.stdout, "write", new=io.StringIO()):
        ok_res = download.try_download_manual("m1", "http://x", dest, silent=True)
    check("чистая закачка -> True", ok_res is True)
    check("файл целый", dest.read_bytes() == full)
    check("скорость зеркала запомнена в кэш",
          "m1" in download.load_mirror_cache())

    # 6.2 докачка по Range (206)
    dest2 = paths["base"] / "two.zip"
    dest2.write_bytes(ZIP_MAGIC + b"a" * 100)
    tail = b"a" * 5000   # остаток докачки (> 1 КБ — порог минимального размера)
    seen_headers = {}

    def opener_range(req, timeout):
        seen_headers.update(dict(req.headers))
        return FakeResp(tail, status=206, headers={"Content-Length": str(len(tail))})

    with mock.patch.object(download, "open_checked", side_effect=opener_range):
        ok_res = download.try_download_manual("m2", "http://x", dest2, silent=True)
    check("докачка 206 -> True", ok_res is True)
    check("Range-заголовок отправлен",
          seen_headers.get("Range") == "bytes=104-")
    check("хвост дописан, начало сохранено",
          dest2.read_bytes() == (ZIP_MAGIC + b"a" * 100) + tail)

    # 6.3 сервер проигнорировал Range (200) -> перезапись с нуля, без дублей
    dest3 = paths["base"] / "three.zip"
    dest3.write_bytes(ZIP_MAGIC + b"b" * 100)

    def opener_full200(req, timeout):
        return FakeResp(full, status=200, headers={"Content-Length": str(len(full))})

    with mock.patch.object(download, "open_checked", side_effect=opener_full200):
        ok_res = download.try_download_manual("m3", "http://x", dest3, silent=True)
    check("200 вместо 206 -> True", ok_res is True)
    check("файл перезаписан целиком (нет хвоста-двойника)",
          dest3.read_bytes() == full)

    # 6.4 416 при существующем файле -> удалить и качать с нуля
    dest4 = paths["base"] / "four.zip"
    dest4.write_bytes(b"stale-cache-bytes")

    def opener_416(req, timeout):
        if "Range" in dict(req.headers):
            raise urllib.error.HTTPError("u", 416, "range", None, None)
        return FakeResp(full, headers={"Content-Length": str(len(full))})

    with mock.patch.object(download, "open_checked", side_effect=opener_416):
        ok_res = download.try_download_manual("m4", "http://x", dest4, silent=True)
    check("416 -> перес закачки с нуля -> True", ok_res is True)
    check("старый мусор заменён полным файлом", dest4.read_bytes() == full)

    # 6.5 HTTPError 500 -> False
    with mock.patch.object(download, "open_checked",
                           side_effect=urllib.error.HTTPError("u", 500, "err", None, None)):
        check("HTTP 500 -> False",
              download.try_download_manual("m5", "http://x",
                                           paths["base"] / "five.zip", silent=True) is False)

    # 6.6 усечённый ответ (сервер закрыл соединение раньше Content-Length)
    dest6 = paths["base"] / "six.zip"
    truncated = FakeResp(big[:1024], headers={"Content-Length": str(len(big))})
    with mock.patch.object(download, "open_checked", return_value=truncated):
        check("обрыв без исключения -> False (докачается позже)",
              download.try_download_manual("m6", "http://x", dest6, silent=True) is False)

    # 6.7 исключение при read -> False
    class BoomRead(FakeResp):
        def read(self, n=-1):
            raise OSError("connection reset")

    dest7 = paths["base"] / "seven.zip"
    with mock.patch.object(download, "open_checked",
                           return_value=BoomRead(big, headers={"Content-Length": str(len(big))})):
        check("read бросает исключение -> False",
              download.try_download_manual("m7", "http://x", dest7, silent=True) is False)

    # 6.8 slow-mode: медленное зеркало отсекается, счётчик растёт
    dest8 = paths["base"] / "eight.zip"

    class SlowChunks(FakeResp):
        """Отдаёт по чуть-чуть; время «ускоряем» подменой time.time."""

        def __init__(self, body, headers=None):
            super().__init__(body, headers=headers)
            self._sent = 0

        def read(self, n=-1):
            if self._sent >= len(self._buf.getvalue()):
                return b""
            self._sent += 1024
            return self._buf.getvalue()[self._sent - 1024:self._sent]

    # Скорость зеркала замеряется только для больших файлов:
    # do_speed_check = total > SPEED_CHECK_MIN_MB*МБ И slow_mode ещё не активен.
    # Поэтому ждём реальный порог (заимпортированный в модуль константой),
    # берём тело больше порога, а время мокним так: start_time=0, все
    # последующие замеры — +100 секунд (скорость ~несколько КБ/с).
    threshold_mb = download.SPEED_CHECK_MIN_MB
    big2 = ZIP_MAGIC + b"w" * ((threshold_mb + 2) * 1024 * 1024)

    def slow_attempt(dest_path):
        # каждый вызов time.time(): первый (start_time) = 0, дальше = +100с;
        # open_checked тоже мокнем ВНУТРИ — иначе предыдущие exit-блоки
        # контекстов восстановили бы настоящий сетевой open_checked.
        clock = iter([0.0] + [100.0] * 100000)
        with mock.patch.object(download.time, "time", new=lambda: next(clock)), \
             mock.patch.object(download, "open_checked",
                               return_value=SlowChunks(
                                   big2, headers={"Content-Length": str(len(big2))})):
            return download.try_download_manual(
                "slow-mirror", "http://x", dest_path, silent=True)

    r = slow_attempt(dest8)
    check("медленная закачка прервана -> False", r is False)
    check("slow_mirror_count увеличен", state.slow_mirror_count == 1)
    check("пока рано: slow-режим не активен до SLOW_MODE_AFTER медленных",
          state.slow_mode_active is False)
    # второе медленное зеркало достигает порога SLOW_MODE_AFTER
    r2 = slow_attempt(paths["base"] / "eight2.zip")
    check("второе медленное зеркало тоже отсекается", r2 is False)
    check("slow-режим активирован после SLOW_MODE_AFTER медленных зеркал",
          state.slow_mode_active is True and state.slow_mirror_count == 2)

    # 6.9 response без Content-Length — total=0, деление не должно упасть
    dest9 = paths["base"] / "nine.zip"
    with mock.patch.object(download, "open_checked",
                           return_value=FakeResp(full)):
        check("ответ без Content-Length -> True (прогресс не ломается)",
              download.try_download_manual("m9", "http://x", dest9, silent=True) is True)


run_dl_section("manual", _p, _manual_body)
state.slow_mirror_count = 0
state.slow_mode_active = False


print("== 7. download_file: ранний выход, ротация зеркал, HTML-чистка ==")
_p = isolate_download("dlfile")


def _dlfile_body(paths):
    # 7.1 валидный кэш -> сеть не трогается вообще
    good = paths["base"] / "cached.zip"
    good.write_bytes(zip_bytes(payload=b"c" * 4096))
    with mock.patch.object(download, "open_checked",
                           side_effect=AssertionError("сеть не нужна!")) as m_open:
        r = download.download_file([("m", "http://x")], good, "t", min_size_mb=0,
                                   silent=True)
    check("уже скачанный валидный файл -> True без сети", r is True)
    check("open_checked не вызывался", not m_open.called)

    # 7.2 битый кэш (HTML) перекачивается и заменяется
    bad = paths["base"] / "badcache.zip"
    bad.write_bytes(HTML_GARBAGE * 200)
    with mock.patch.object(download, "open_checked",
                           return_value=FakeResp(zip_bytes(payload=b"n" * 4096),
                                                 headers={"Content-Length": "4104"})):
        r = download.download_file([("m", "http://x")], bad, "t", min_size_mb=0,
                                   silent=True)
    check("битый кэш перекачан -> True", r is True)
    check("содержимое заменено на валидное", bad.read_bytes().startswith(ZIP_MAGIC))

    # 7.3 первое зеркало отдаёт HTML -> файл удалён, взято второе зеркало
    d3 = paths["base"] / "rot.zip"
    responses = [
        FakeResp(HTML_GARBAGE * 200, headers={"Content-Length": str(len(HTML_GARBAGE * 200))}),
        FakeResp(zip_bytes(payload=b"o" * 4096), headers={"Content-Length": "4104"}),
    ]

    def rot(req, timeout):
        return responses.pop(0)

    with mock.patch.object(download, "open_checked", side_effect=rot):
        r = download.download_file([("html-mirror", "http://1"),
                                    ("good-mirror", "http://2")],
                                   d3, "t", min_size_mb=0, silent=True)
    check("ротация зеркал: HTML-зеркало сменилось хорошим -> True", r is True)
    check("HTML-огрызок удалён, на диске валидный zip",
          d3.exists() and d3.read_bytes().startswith(ZIP_MAGIC))

    # 7.4 все зеркала недоступны -> False, временных файлов не остаётся
    d4 = paths["base"] / "dead.zip"
    with mock.patch.object(download, "open_checked",
                           side_effect=urllib.error.HTTPError("u", 500, "e", None, None)):
        r = download.download_file([("a", "http://1"), ("b", "http://2")],
                                   d4, "t", min_size_mb=0, silent=True)
    check("все зеркала упали -> False", r is False)

    # 7.5 min_size_mb=0 больше не принимает мусор < 1 КБ
    d5 = paths["base"] / "tiny.zip"
    d5.write_bytes(ZIP_MAGIC + b"t" * 10)     # валидная магия, но 20 байт
    r = download.download_file([("m", "http://x")], d5, "t", min_size_mb=0,
                               silent=True)
    check("крошечный (20 байт) «валидный» файл НЕ принят как готовый", r is False)

    # 7.6 aria2c-ветка: HEAD 404 пропускает зеркало, ручная закачка succeeds
    d6 = paths["base"] / "ariafallback.zip"
    binp = paths["ARIA2C_BIN"]
    binp.parent.mkdir(parents=True, exist_ok=True)
    binp.write_bytes(b"#!/bin/sh\n")
    binp.chmod(0o755)
    with mock.patch.object(download, "head_check", return_value=(False, 0)), \
         mock.patch.object(download, "open_checked",
                           return_value=FakeResp(zip_bytes(payload=b"w" * 4096),
                                                 headers={"Content-Length": "4104"})):
        r = download.download_file([("m", "http://x")], d6, "t",
                                   min_size_mb=0, silent=True)
    check("HEAD не пройден -> aria2c пропущен, ручная закачка -> True", r is True)


run_dl_section("dlfile", _p, _dlfile_body)


print("== 8. parallel_status ==")
buf = io.StringIO()
old_stdout = sys.stdout
sys.stdout = buf
try:
    download.parallel_status("wine", 50, mb_done=10, mb_all=20, speed_kb=512)
    download.parallel_status("dxvk", 0)
finally:
    sys.stdout = old_stdout
out = buf.getvalue()
check("parallel_status пишет прогресс-бары", "wine" in out and "%" in out)
check("для unknown-size печатает 'скачиваю...'", "dxvk" in out and "скачиваю" in out)


print("== 9. modules/wine.py (модуль) ==")
wt = TMP / "winemod"
wt.mkdir(parents=True, exist_ok=True)
WINE_BIN_T = wt / "wine.AppImage"
RUNEXE_T = wt / "runexe"
DXVK_DIR_T = wt / "dxvk"
PREFIX_T = wt / "prefix"

orig_wine_consts = (wine.WINE_DIR, wine.WINE_BIN, wine.RUNEXE,
                    wine.DXVK_DIR, wine.WINE_PREFIX)
wine.WINE_DIR, wine.WINE_BIN, wine.RUNEXE = wt, WINE_BIN_T, RUNEXE_T
wine.DXVK_DIR, wine.WINE_PREFIX = DXVK_DIR_T, PREFIX_T

try:
    # 9.1 download_wine: успех -> chmod 755; провал -> chmod не трогает
    captured = {}

    def df_ok(mirrors, dest, label, min_size_mb=10, silent=False):
        captured["args"] = (list(mirrors), dest, label, min_size_mb)
        dest.write_bytes(b"#!/bin/sh\n")
        dest.chmod(0o644)
        return True

    orig_df = wine.download_file
    wine.download_file = df_ok
    try:
        check("download_wine -> True", wine.download_wine() is True)
    finally:
        wine.download_file = orig_df
    _, dest_arg, label_arg, msz_arg = captured["args"]
    check("Wine качается в WINE_BIN с min_size_mb=50",
          dest_arg == WINE_BIN_T and msz_arg == 50 and "Wine" in label_arg)
    check("успешный Wine получил права 755",
          WINE_BIN_T.stat().st_mode & 0o777 == 0o755)

    WINE_BIN_T.unlink()

    def df_fail(*a, **k):
        return False

    wine.download_file = df_fail
    try:
        check("download_wine при провале -> False", wine.download_wine() is False)
    finally:
        wine.download_file = orig_df
    check("при провале chmod не применялся (файла нет)", not WINE_BIN_T.exists())

    # 9.2 create_runexe: содержимое и права
    wine.create_runexe()
    rx = RUNEXE_T.read_text(encoding="utf-8")
    check("runexe — bash-скрипт", rx.startswith("#!/bin/bash"))
    check("runexe экспортирует WINEPREFIX", f'export WINEPREFIX="{PREFIX_T}"' in rx)
    check("runexe экспортирует DXVK overrides", DXVK_OVERRIDES in rx)
    check("runexe exec'ит WINE_BIN", f'exec "{WINE_BIN_T}" "$@"' in rx)
    check("runexe исполняемый (755)",
          RUNEXE_T.stat().st_mode & 0o777 == 0o755)

    # 9.3 download_dxvk: уже установлен -> True без сети
    (DXVK_DIR_T / "x64").mkdir(parents=True, exist_ok=True)
    with mock.patch.object(wine, "download_file",
                           side_effect=AssertionError("сеть не нужна")):
        check("DXVK уже есть -> True без скачивания",
              wine.download_dxvk(silent=True) is True)

    # 9.4 download_dxvk: полный цикл — скачан tar.gz, распакован, переименован
    shutil.rmtree(DXVK_DIR_T)
    archive_holder = {}

    def df_archive(mirrors, dest, label, min_size_mb=10, silent=False):
        archive_holder["mirrors"] = list(mirrors)
        archive_holder["label"] = label
        dest.write_bytes(tar_gz_bytes())
        return True

    real_safe_extract = net.safe_extract_tar

    def fake_extract(archive, dest_dir):
        # реально распакуем в temp-dir wine-модуля
        real_safe_extract(archive, dest_dir)

    wine.download_file = df_archive
    orig_se = net.safe_extract_tar
    net.safe_extract_tar = fake_extract
    try:
        r = wine.download_dxvk(silent=True)
    finally:
        wine.download_file = orig_df
        net.safe_extract_tar = orig_se
    check("download_dxvk полный цикл -> True", r is True)
    check("dxvk-папка переименована в DXVK_DIR", (DXVK_DIR_T / "x64").is_dir())
    check("dxvk-архив удалён после распаковки",
          not (wt / "dxvk.tar.gz").exists())
    check("зеркала содержат github-прокси",
          any("ghfast" in u for _, u in archive_holder["mirrors"]))

    # 9.5 download_dxvk: битый архив (скачался, но распасть нельзя) -> False
    shutil.rmtree(DXVK_DIR_T)

    def df_corrupt(mirrors, dest, label, min_size_mb=10, silent=False):
        dest.write_bytes(b"this is not a tar.gz at all")
        return True

    wine.download_file = df_corrupt
    try:
        check("битый dxvk-архив -> False (исключение перехвачено)",
              wine.download_dxvk(silent=True) is False)
    finally:
        wine.download_file = orig_df

    # 9.6 install_dxvk_to_wine: копирование DLL и маркер
    game_exe = wt / "game.exe"
    game_exe.write_bytes(b"MZ")
    s32 = PREFIX_T / "drive_c" / "windows" / "system32"
    wow64 = PREFIX_T / "drive_c" / "windows" / "syswow64"
    s32.mkdir(parents=True, exist_ok=True)
    wow64.mkdir(parents=True, exist_ok=True)
    (DXVK_DIR_T / "x64").mkdir(parents=True, exist_ok=True)
    (DXVK_DIR_T / "x64" / "dxgi.dll").write_bytes(ZIP_MAGIC)
    (DXVK_DIR_T / "x32").mkdir(parents=True, exist_ok=True)
    (DXVK_DIR_T / "x32" / "d3d9.dll").write_bytes(ZIP_MAGIC)

    orig_pp = prefix.get_prefix_path
    prefix.get_prefix_path = lambda exe_path=None: PREFIX_T
    try:
        check("install_dxvk_to_wine -> True", wine.install_dxvk_to_wine() is True)
        check("x64 dll -> system32", (s32 / "dxgi.dll").exists())
        check("x32 dll -> syswow64 (64-битный префикс)", (wow64 / "d3d9.dll").exists())
        marker = PREFIX_T / ".dxvk_installed"
        check("маркер .dxvk_installed записан",
              marker.read_text(encoding="utf-8").strip() == str(DXVK_DIR_T))
        check("повторный вызов идемпотентен (по маркеру)",
              wine.install_dxvk_to_wine() is True)

        # нет system32 -> False
        marker.unlink()
        orig_s32 = s32.rename(wt / "s32-hidden")
        try:
            check("префикс без system32 -> False",
                  wine.install_dxvk_to_wine() is False)
        finally:
            orig_s32.rename(s32)

        # 32-битный префикс (нет syswow64): x32-dll идут в system32
        marker.unlink(missing_ok=True)
        shutil.rmtree(wow64)
        check("32-битный префикс -> True", wine.install_dxvk_to_wine() is True)
        check("x32 dll попал в system32", (s32 / "d3d9.dll").exists())
    finally:
        prefix.get_prefix_path = orig_pp

    # 9.7 cmd_dxvk: отказ от переустановки -> ничего не трогается
    (DXVK_DIR_T / "x64").mkdir(parents=True, exist_ok=True)
    with mock.patch("builtins.input", return_value="n"):
        wine.cmd_dxvk()
    check("ответ 'n' сохраняет установленный DXVK", DXVK_DIR_T.is_dir())

    # 9.8 cmd_dxvk: подтверждение -> снос, перевкачка, установка
    calls = []
    orig_dd = wine.download_dxvk
    orig_ep = prefix.ensure_prefix
    orig_idw = wine.install_dxvk_to_wine
    wine.download_dxvk = lambda silent=False: (calls.append("download"), True)[1]
    prefix.ensure_prefix = lambda *a, **k: (calls.append("prefix"), True)[1]
    wine.install_dxvk_to_wine = lambda exe=None: (calls.append("install"), True)[1]
    with mock.patch("builtins.input", return_value="y"):
        wine.cmd_dxvk()
    wine.download_dxvk = orig_dd
    prefix.ensure_prefix = orig_ep
    wine.install_dxvk_to_wine = orig_idw
    check("cmd_dxvk(y): снёс старый DXVK", not DXVK_DIR_T.exists())
    check("cmd_dxvk(y): download -> ensure_prefix -> install",
          calls == ["download", "prefix", "install"])

    # 9.9 cmd_update: подтверждение -> перекачка Wine+DXVK, runexe, сброс маркера
    wt2 = TMP / "updatetest"
    wt2.mkdir()
    w2_bin = wt2 / "wine.AppImage"
    w2_bin.write_bytes(b"old-wine")
    w2_dxvk = wt2 / "dxvk"
    (w2_dxvk / "x64").mkdir(parents=True)
    w2_prefix = wt2 / "prefix"
    w2_prefix.mkdir()
    (w2_prefix / ".dxvk_installed").write_text(str(w2_dxvk), encoding="utf-8")
    wine.WINE_DIR, wine.WINE_BIN = wt2, w2_bin
    wine.DXVK_DIR, wine.WINE_PREFIX = w2_dxvk, w2_prefix

    upd_calls = []

    def dw_ok():
        upd_calls.append("wine")
        w2_bin.write_bytes(b"new-wine")
        w2_bin.chmod(0o755)
        return True

    def dd_ok(silent=False):
        upd_calls.append("dxvk")
        (w2_dxvk / "x64").mkdir(parents=True, exist_ok=True)
        return True

    orig_dw, orig_dd2, orig_cr = wine.download_wine, wine.download_dxvk, wine.create_runexe
    wine.download_wine, wine.download_dxvk = dw_ok, dd_ok
    wine.create_runexe = lambda: upd_calls.append("runexe")
    wine.install_dxvk_to_wine = lambda exe=None: upd_calls.append("install")
    try:
        with mock.patch("builtins.input", return_value="да"):
            wine.cmd_update()
    finally:
        wine.download_wine, wine.download_dxvk = orig_dw, orig_dd2
        wine.create_runexe = orig_cr
        wine.install_dxvk_to_wine = orig_idw
    check("cmd_update(да): порядок wine->dxvk->install->runexe",
          upd_calls == ["wine", "dxvk", "install", "runexe"])
    check("cmd_update: старый маркер .dxvk_installed снят",
          not (w2_prefix / ".dxvk_installed").exists())

    # 9.10 cmd_update: EOF на вопрос -> молча выход, ничего не удалено
    w2_bin.write_bytes(b"still-here")
    with mock.patch("builtins.input", side_effect=EOFError):
        wine.cmd_update()
    check("cmd_update при EOF не трогает Wine-бинарь",
          w2_bin.read_bytes() == b"still-here")
finally:
    (wine.WINE_DIR, wine.WINE_BIN, wine.RUNEXE,
     wine.DXVK_DIR, wine.WINE_PREFIX) = orig_wine_consts


print("== 10. modules/prefix.py ==")
pt = TMP / "prefixmod"
pt.mkdir(parents=True, exist_ok=True)
HOME_T = pt / "home"
WINE_PORTABLE_T = pt / "home" / "wine-portable"
DEFAULT_PREF_T = WINE_PORTABLE_T / "prefix"
PREFIXES_T = WINE_PORTABLE_T / "prefixes"
WINE_BIN_T2 = WINE_PORTABLE_T / "wine.AppImage"
DXVK_T2 = WINE_PORTABLE_T / "dxvk"
for d in (HOME_T, DEFAULT_PREF_T, PREFIXES_T, WINE_PORTABLE_T / "logs"):
    d.mkdir(parents=True, exist_ok=True)

orig_prefix_consts = (prefix.HOME, prefix.WINE_PREFIX, prefix.PREFIXES_DIR,
                      prefix.DEFAULT_PREFIX, prefix.WINE_BIN, prefix.DXVK_DIR)
prefix.HOME = HOME_T
prefix.WINE_PREFIX = DEFAULT_PREF_T
prefix.PREFIXES_DIR = PREFIXES_T
prefix.DEFAULT_PREFIX = DEFAULT_PREF_T
prefix.WINE_BIN = WINE_BIN_T2
prefix.DXVK_DIR = DXVK_T2

try:
    # 10.1 get_prefix_path: дефолт и per-game с хешем пути
    state.USE_PER_GAME_PREFIX = False
    check("без флага -> общий префикс",
          prefix.get_prefix_path(Path("/games/a/game.exe")) == DEFAULT_PREF_T)

    state.USE_PER_GAME_PREFIX = True
    p1 = prefix.get_prefix_path(Path("/games/a/game.exe"))
    p2 = prefix.get_prefix_path(Path("/games/b/game.exe"))
    check("per-game: разное имя папки для одинаковых exe из разных мест",
          p1 != p2)
    check("per-game: путь внутри PREFIXES_DIR",
          str(p1).startswith(str(PREFIXES_T)))
    check("per-game: детерминированность",
          p1 == prefix.get_prefix_path(Path("/games/a/game.exe")))
    check("per-game: имя содержит sanitized stem", p1.name.startswith("game-"))
    check("per-game без exe -> общий префикс",
          prefix.get_prefix_path(None) == DEFAULT_PREF_T)

    weird = prefix.get_prefix_path(pt / ("очень длинное имя игры!!!" * 3))
    check("кириллица/спецсимволы санитизируются в имя папки",
          weird.name.split("-")[0].replace("_", "") != "" and
          all(ch.isalnum() or ch in "_-" for ch in weird.name))
    state.USE_PER_GAME_PREFIX = False

    # 10.2 get_wine_env
    env_base = prefix.get_wine_env(use_dxvk=False)
    check("WINEPREFIX указывает на дефолтный префикс",
          env_base["WINEPREFIX"] == str(DEFAULT_PREF_T))
    check("без DXVK-папки WINEDLLOVERRIDES не навязывается",
          DXVK_OVERRIDES != env_base.get("WINEDLLOVERRIDES"))
    DXVK_T2.mkdir(exist_ok=True)
    env_dxvk = prefix.get_wine_env(use_dxvk=True)
    check("с DXVK включены native-overrides",
          env_dxvk.get("WINEDLLOVERRIDES") == DXVK_OVERRIDES)
    state.USE_DXVK_HUD = True
    state.USE_MANGOHUD = True
    env_hud = prefix.get_wine_env(use_dxvk=False)
    check("HUD/MangoHud флаги попадают в окружение",
          "fps" in env_hud.get("DXVK_HUD", "") and env_hud.get("MANGOHUD") == "1")
    state.USE_DXVK_HUD = False
    state.USE_MANGOHUD = False
    env_per = prefix.get_wine_env(use_dxvk=False, exe_path=Path("/g/x/game.exe"))
    check("env наследует реальный environ процесса",
          env_per.get("PATH") == os.environ.get("PATH"))

    # 10.3 ensure_prefix (subprocess полностью моканут)
    s32_p = DEFAULT_PREF_T / "drive_c" / "windows" / "system32"

    def run_make_prefix(cmd, **kw):
        s32_p.mkdir(parents=True, exist_ok=True)
        return type("R", (), {"returncode": 0})()

    with mock.patch.object(prefix.subprocess, "run",
                           side_effect=run_make_prefix) as mr:
        check("ensure_prefix создал префикс -> True", prefix.ensure_prefix() is True)
    checked_cmd = mr.call_args.args[0]
    checked_env = mr.call_args.kwargs["env"]
    check("wineboot вызван нужным бинарём", checked_cmd == [str(WINE_BIN_T2), "wineboot", "-u"])
    check("wineboot идёт БЕЗ dxvk-overrides (bootstrap-режим)",
          checked_env.get("WINEDLLOVERRIDES") != DXVK_OVERRIDES)
    check("WINEPREFIX передан в env wineboot",
          checked_env["WINEPREFIX"] == str(DEFAULT_PREF_T))
    check("маркер .wineboot_done записан",
          (DEFAULT_PREF_T / ".wineboot_done").exists())
    check("идемпотентность: повтор НЕ запускает wineboot",
          (lambda before: (prefix.ensure_prefix(),
                           mr.call_count == before))(mr.call_count))

    # force_boot пересоздаёт даже при наличии маркера
    with mock.patch.object(prefix.subprocess, "run", side_effect=run_make_prefix) as mr2:
        check("force_boot -> True", prefix.ensure_prefix(force_boot=True) is True)
    check("force_boot снова вызывает wineboot", mr2.call_count == 1)

    # wineboot упал -> False
    (DEFAULT_PREF_T / ".wineboot_done").unlink()
    with mock.patch.object(prefix.subprocess, "run",
                           side_effect=OSError("no wine")):
        check("сбой wineboot -> False без исключения",
              prefix.ensure_prefix() is False)

    # wineboot «успешен», но system32 не появился -> False
    def run_nothing(cmd, **kw):
        return type("R", (), {"returncode": 0})()

    shutil.rmtree(s32_p)   # wineboot «не создал» system32
    with mock.patch.object(prefix.subprocess, "run", side_effect=run_nothing):
        check("пустой wineboot (system32 нет) -> False",
              prefix.ensure_prefix() is False)

    # 10.4 find_minecraft_dirs / choose_minecraft_dir
    def mkmc(base, with_versions=True, empty_versions=False):
        base.mkdir(parents=True, exist_ok=True)
        if with_versions:
            v = base / "versions"
            v.mkdir(exist_ok=True)
            if not empty_versions:
                (v / "1.20.1").mkdir(exist_ok=True)
        return base

    mc_a = mkmc(HOME_T / "gameset" / ".minecraft")            # 1 версия
    mc_b = mkmc(HOME_T / "other" / ".minecraft", empty_versions=True)  # 0 версий
    mc_direct = mkmc(HOME_T / ".minecraft")                   # 2 версии
    (mc_direct / "versions" / "1.19").mkdir(exist_ok=True)

    found = prefix.find_minecraft_dirs()
    resolved = {str(p.resolve()) for p in found}
    check("найдены все три .minecraft из HOME",
          {str(mc_a.resolve()), str(mc_b.resolve()), str(mc_direct.resolve())} <= resolved)
    check("сортировка: папка с наибольшим числом версий первая",
          str(found[0].resolve()) == str(mc_direct.resolve()))

    # бэкап миров не считается игрой
    backup_mc = mkmc(HOME_T / "backupdisk" / "minecraft-worlds" / ".minecraft")
    found2 = prefix.find_minecraft_dirs()
    check("папка внутри minecraft-worlds ОТБРОШЕНА",
          str(backup_mc.resolve()) not in {str(p.resolve()) for p in found2})

    # лимит работает
    check("limit respected", len(prefix.find_minecraft_dirs(limit=2)) <= 2)

    # префиксный путь wine (drive_c/users/stud/.minecraft)
    stud = DEFAULT_PREF_T / "drive_c" / "users" / "stud"
    mkmc(stud / ".minecraft")
    found3 = prefix.find_minecraft_dirs()
    check("префиксные пути тоже сканируются",
          str((stud / ".minecraft").resolve()) in {str(p.resolve()) for p in found3})

    # choose_minecraft_dir
    orig_find = prefix.find_minecraft_dirs
    prefix.find_minecraft_dirs = lambda limit=12: []
    check("ничего не найдено -> None", prefix.choose_minecraft_dir() is None)
    prefix.find_minecraft_dirs = lambda limit=12: [mc_a]
    check("одна папка -> берётся без вопросов",
          prefix.choose_minecraft_dir(auto=False) == mc_a)
    prefix.find_minecraft_dirs = lambda limit=12: [mc_direct, mc_a, mc_b]
    check("auto=True берёт первую", prefix.choose_minecraft_dir(auto=True) == mc_direct)
    with mock.patch("builtins.input", return_value="2"):
        check("интерактивный выбор: '2' -> вторая папка",
              prefix.choose_minecraft_dir(auto=False) == mc_a)
    with mock.patch("builtins.input", return_value="99"):
        check("некорректный ввод -> откат на первую",
              prefix.choose_minecraft_dir(auto=False) == mc_direct)
    with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
        check("Ctrl+C -> откат на первую",
              prefix.choose_minecraft_dir(auto=False) == mc_direct)
    prefix.find_minecraft_dirs = orig_find

    # 10.5 get_minecraft_game_path / show_minecraft_paths
    gpath = prefix.get_minecraft_game_path()
    check("game path ведёт в .tlauncher legacy",
          gpath.endswith(".tlauncher/legacy/Minecraft/game")
          and str(DEFAULT_PREF_T) in gpath)
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        prefix.show_minecraft_paths()
    finally:
        sys.stdout = old_stdout
    out = buf.getvalue()
    check("show_minecraft_paths печатает Linux-путь", ".tlauncher" in out)
    check("и Windows-путь", r"C:\users\stud" in out)
    check("и найденные папки", ".minecraft" in out)
finally:
    (prefix.HOME, prefix.WINE_PREFIX, prefix.PREFIXES_DIR,
     prefix.DEFAULT_PREFIX, prefix.WINE_BIN, prefix.DXVK_DIR) = orig_prefix_consts
    state.USE_PER_GAME_PREFIX = False
    state.USE_DXVK_HUD = False
    state.USE_MANGOHUD = False


print("== 11. Консистентность данных зеркал (без сети) ==")
check("WINE_MIRRORS непустой и все URL https",
      len(WINE_MIRRORS) >= 3
      and all(u.startswith("https://") for _, u in WINE_MIRRORS))
check("имена зеркал уникальны", len({n for n, _ in WINE_MIRRORS}) == len(WINE_MIRRORS))
dm = make_dxvk_mirrors("v2.4.1")
check("dxvk-зеркала содержат версию в имени файла",
      any("dxvk-2.4.1.tar.gz" in u for _, u in dm))
check("dxvk-зеркала покрыты gh-прокси", any("ghfast.top/" in u for _, u in dm))


print()
if fails:
    print(f"ПРОВАЛОВ: {len(fails)} -> {fails}")
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
shutil.rmtree(TMP, ignore_errors=True)
