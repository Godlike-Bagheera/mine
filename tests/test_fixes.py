#!/usr/bin/env python3
"""Интеграционные проверки ключевых фиксов (без сети, локально)."""
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# fake-HOME: всегда свежий tempdir (setdefault не сработал бы, если HOME
# уже задан в окружении — тесты начали бы трогать реальный ~/.minecraft).
# WI_FAKE_HOME=1 запрещает config._resolve_home фолбэк на passwd-дом (/root).
os.environ["HOME"] = tempfile.mkdtemp(prefix="fakehome_")
os.environ["WI_FAKE_HOME"] = "1"

from modules.download import (_looks_binary, download_file,        # noqa: E402
                              _is_tls_fail, _curl_get_probe_ok)
from modules.hash_utils import verify_sha1, verify_sha512          # noqa: E402
import modules.config as cfg                                       # noqa: E402
from modules.config import make_github_mirrors                     # noqa: E402
import modules.minecraft as mc                                     # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="fo_test_"))

# ── 1. _looks_binary: HTML-заглушка с расширением .jar/mrpack/gz ──
bad_jar = tmp / "fabric-installer.jar"
bad_jar.write_bytes(b"<html><body>proxy block</body></html>" * 100)
check("HTML под видом .jar отклоняется", not _looks_binary(bad_jar))

real_zip = tmp / "good.mrpack"
with zipfile.ZipFile(real_zip, "w") as z:
    z.writestr("modrinth.index.json", "{}")
check("настоящий zip/.mrpack принимается", _looks_binary(real_zip))

gz = tmp / "x.tar.gz"
gz.write_bytes(b"\x1f\x8b\x08\x00rest")
check("gzip принимается", _looks_binary(gz))
txt = tmp / "script.sh"
txt.write_text("#!/bin/sh\necho hi")
check(".sh не фальсифицируется проверкой", _looks_binary(txt))

# ── 2. download_file: битый кэш перекачивается, а не считается валидным ──
dest = tmp / "cached.jar"
dest.write_bytes(b"<!DOCTYPE html>not a jar " * 500)   # >1КБ, но HTML
res = download_file([("test-mirror", "http://127.0.0.1:9/none")], dest, "t", min_size_mb=0, silent=True)
check("битый кэш не принят как успешная закачка", res is False)

# ── 3. make_github_mirrors: api.github.com не оборачивается прокси ──
m = make_github_mirrors("https://api.github.com/repos/x/releases")
check("для API нет зеркал-прокси", m == [])
m2_url = "https://github.com/a/b/releases/download/v1/f.zip"
m2 = make_github_mirrors(m2_url)


def _expected_proxies():
    """Ожидаемый список зеркал — из самого источника истины, без хардкода.

    Сначала актуальный API gh_proxies() (учитывает overrides из
    settings.json); если его нет (старый код до ротации прокси) — модульный
    список _GH_PROXIES; в самом крайнем случае — легаси-тройка, которая
    жёстко зашита в старом make_github_mirrors. Тест не устаревает при
    добавлении/удалении зеркал и не падает ImportError на старом коде.
    """
    gp = getattr(cfg, "gh_proxies", None)
    if callable(gp):
        return gp()
    ghp = getattr(cfg, "_GH_PROXIES", None)
    if isinstance(ghp, list) and ghp:
        return list(ghp)
    return [("ghfast.top", "https://ghfast.top/"),
            ("ghproxy.net", "https://ghproxy.net/"),
            ("gh-proxy.com", "https://gh-proxy.com/")]


# Актуальные рабочие зеркала для РФ (см. _GH_PROXIES в modules/config.py):
# ghfast.top / ghproxy.net / gh.llkk.cc — проверены живыми запросами;
# gh-proxy.com оставлен как резерв (403 на релиз-ассеты).
proxies = _expected_proxies()
check(
    "для release-ассета зеркала есть",
    len(proxies) > 0
    and len(m2) == len(proxies)
    and [n for n, _ in m2] == [n for n, _ in proxies]
    and all(u == pfx + m2_url for (n, u), (_, pfx) in zip(m2, proxies)),
)

# ── 4. sha1/sha512 утилиты ──
h = tmp / "hashme.bin"
h.write_bytes(b"abc")
ok1, actual1 = verify_sha1(h, "a9993e364706816aba3e25717850c26c9cd0d89d")
check("verify_sha1 совпал", ok1)
ok512, _ = verify_sha512(h, "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f")
check("verify_sha512 совпал", ok512)
ok_bad, _ = verify_sha1(h, "00" * 20)
check("неверный sha1 отклонён", not ok_bad)

# ── 5. патч overrides из zip (главный баг: файлы НЕ распаковывались) ──
game = tmp / ".minecraft"
(game / "versions").mkdir(parents=True)
zip_path = tmp / "FO_v14.1.0_for_1.21.5.zip"
with zipfile.ZipFile(zip_path, "w") as z:
    z.writestr("modlist.html", '<a href="https://example.cf/mods/download/1/sodium.jar">s</a>')
    z.writestr("overrides/", "")                                    # dir-entry
    z.writestr("overrides/config/sodium-options.json", '{"a":1}')   # файл!
    z.writestr("overrides/config/fabric_loader_dependencies.json",
               json.dumps({"overrides": {"java": {"net.fabricmc.fabric-loader": "0.16.9"}}}))
    z.writestr("manifest.json", json.dumps({"manifestVersion": "2", "minecraftVersion": "1.21.5"}))

# перехватываем сеть: install_fabric_profile/finish_version_install не дёрнем без сети —
# они вызываются только при loader_version; сделаем их заглушками
called = {}
mc.install_fabric_profile = lambda gd, mcv, lv: called.update(profile=(mcv, lv)) or True
mc.finish_version_install = lambda gd, mcv, vid: called.update(finish=(mcv, vid)) or True

res = mc.unpack_fo_zip_to_game(zip_path, game, "14.1.0")
check("zip-путь установки FO вернул успех", res is True)
check("override-ФАЙЛ распакован в config/",
      (game / "config" / "sodium-options.json").exists())
mods = list((game / "mods").glob("*.jar")) if (game / "mods").is_dir() else []
check("папка mods создана (модалити распознаны)", (game / "mods").is_dir())
check("Fabric-профиль затребован с версиями из zip",
      called.get("profile") == ("1.21.5", "0.16.9"))
check("finish_version_install вызван",
      called.get("finish") == ("1.21.5", "fabric-loader-0.16.9-1.21.5"))

# ── 6. фильтр релизов FO ──
check("v14.1.0 — релиз", mc._fo_is_release("v14.1.0", "FabulouslyOptimized-14.1.0.mrpack"))
check("alpha отбраковывается", not mc._fo_is_release("v15.0.0-alpha.5"))
check("beta отбраковывается", not mc._fo_is_release("v15.0.0-beta.2"))
check("rc отбраковывается", not mc._fo_is_release("v14.2.0-rc1"))

# ── 7. интеграция UI: EXPECTED_COMMANDS, маршрутизация, справка без эмодзи ──
import inspect                                             # noqa: E402
import unicodedata                                        # noqa: E402
from modules.config import EXPECTED_COMMANDS              # noqa: E402
import modules.ui as ui                                   # noqa: E402

for c in ("gamestatus", "stopgame", "waitgame", "games", "shaders"):
    check(f"EXPECTED_COMMANDS содержит '{c}'", c in EXPECTED_COMMANDS)

src_pi = inspect.getsource(ui.process_input) + inspect.getsource(ui.resolve_command)
for c in EXPECTED_COMMANDS:
    # команда считается обработанной, если она ключ реестра (данные),
    # ключ выхода (exit-команды) или упоминается в коде маршрутизации
    check(f"process_input обрабатывает '{c}'",
          c in ui.COMMAND_REGISTRY or c in ui.ARGS_COMMANDS
          or c in ui.EXIT_COMMANDS or c in src_pi)

routed = {
    "gamestatus": "cmd_game_status",
    "состояниеигр": "cmd_game_status",
    "статусигр": "cmd_game_status",
    "games": "cmd_games_list",
    "игры": "cmd_games_list",
    "shaders": "cmd_shaders",
    "шейдеры": "cmd_shaders",
}
called = {}
_orig = {
    "cmd_game_status": ui.cmd_game_status,
    "cmd_games_list": ui.cmd_games_list,
    "cmd_shaders": ui.cmd_shaders,
    "cmd_stopgame": ui.cmd_stopgame,
    "cmd_waitgame": ui.cmd_waitgame,
}
ui.cmd_game_status = lambda: called.update(fn="cmd_game_status")
ui.cmd_games_list = lambda: called.update(fn="cmd_games_list")
ui.cmd_shaders = lambda arg="": called.update(fn="cmd_shaders", arg=arg)
ui.cmd_stopgame = lambda arg="": called.update(fn="cmd_stopgame", arg=arg)
ui.cmd_waitgame = lambda arg="": called.update(fn="cmd_waitgame", arg=arg)
try:
    for inp, expect_fn in routed.items():
        called.clear()
        last, cont = ui.process_input(inp, None)
        check(f"маршрутизация '{inp}' -> {expect_fn}",
              cont and called.get("fn") == expect_fn)
    for inp, expect_fn, expect_arg in [
        ("stopgame all", "cmd_stopgame", "all"),
        ("стопигра mine", "cmd_stopgame", "mine"),
        ("waitgame mine", "cmd_waitgame", "mine"),
        ("ждатьигру game#2", "cmd_waitgame", "game#2"),
    ]:
        called.clear()
        last, cont = ui.process_input(inp, None)
        check(f"маршрутизация с аргументом '{inp}'",
              cont and called.get("fn") == expect_fn and called.get("arg") == expect_arg)
finally:
    for k, v in _orig.items():
        setattr(ui, k, v)

help_src = inspect.getsource(ui.print_help)
emoji = [ch for ch in help_src if ord(ch) > 0x2B00 and
         unicodedata.category(ch) in ("So", "Sk")]
check("в print_help нет эмодзи", not emoji)
for c in ("gamestatus", "games", "stopgame", "waitgame", "shaders"):
    check(f"справка упоминает '{c}'", c in help_src)

# ── 8. main(): exit-guard и статус в приглашении; живой smoke gamestate ──
main_src = inspect.getsource(ui.main)
check("main вызывает stop_all_games()", "stop_all_games()" in main_src)
check("main показывает статус игр", "gamestate.running()" in main_src
      and "gamestate.status_lines()" in main_src)
mc_menu_src = inspect.getsource(mc.cmd_minecraft)
check("меню Minecraft имеет пункт 9 (шейдеры)",
      "9" in mc_menu_src and "cmd_shaders" in mc_menu_src)

import subprocess                                          # noqa: E402
from modules import gamestate                              # noqa: E402
proc = subprocess.Popen(["sleep", "30"])
gamestate.register("smoketest", proc, "/bin/sleep", kind="test")
check("gamestate: игра в реестре", "smoketest" in gamestate.running())
lines = gamestate.status_lines()
check("gamestate: status_lines непустой", any("smoketest" in l for l in lines))
stopped = gamestate.stop_one("smoketest")
check("gamestate: stop_one остановила", stopped is True)
check("gamestate: реестр пуст после остановки", not gamestate.running())

# ── 9. Prism-валидатор zip + curl TLS-обходы (баги из лога Windows 10) ──
def _mk_prism_zip(path, names):
    """Настоящий zip >10МБ (порог min_mb по умолчанию), чтобы тестировать
    именно содержимое, а не размерную заглушку."""
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, b"x" * (11 * 1024 * 1024))


mingw_zip = tmp / "prism-mingw.zip"
_mk_prism_zip(mingw_zip, ["prismlauncher.exe", "libicuuc78.dll"])
msvc_zip = tmp / "prism-msvc.zip"
_mk_prism_zip(msvc_zip, ["prismlauncher.exe", "icudt78.dll"])
nopicu_zip = tmp / "prism-nopicu.zip"
_mk_prism_zip(nopicu_zip, ["prismlauncher.exe", "Qt6Core.dll"])
junk_html = tmp / "prism-junk.zip"
junk_html.write_bytes(b"<html>proxy blocked</html>" + b" " * (20 * 1024 * 1024))

# Баг №1: проверка была startswith("icu") — рабочая MinGW-w64 Portable
# (libicuuc*.dll) отвергалась («не похож на Portable-архив, перекачай»)
# и качалась по кругу, хотя curl отдал валидные 41.7 МБ с GitHub.
check("MinGW-w64 Portable zip принимается (libicuuc78.dll)",
      mc._prism_zip_ok(mingw_zip) is True)
check("MSVC Portable zip принимается (icudt78.dll)",
      mc._prism_zip_ok(msvc_zip) is True)
check("zip без ICU-DLL отклоняется", mc._prism_zip_ok(nopicu_zip) is False)
check("HTML-заглушка под видом zip отклоняется",
      mc._prism_zip_ok(junk_html) is False)

# Баг №2: _is_tls_fail ловил только текстовые фразы stderr; собственный
# формат кода «rc=35» (пустой stderr) не распознавался — обходы
# --tlsv1.2/--http2 никогда не добавлялись, простыня «curl не смог: rc=35».
check("_is_tls_fail('rc=35') == True", _is_tls_fail("rc=35") is True)
check("_is_tls_fail('...([]): rc=35') == True",
      _is_tls_fail("curl attempt failed ([]): rc=35") is True)
check("_is_tls_fail('SSL connect error') == True",
      _is_tls_fail("SSL connect error") is True)
check("_is_tls_fail('HTTP 429') == False (не TLS)", _is_tls_fail("HTTP 429") is False)
check("_is_tls_fail('rc=6') == False (DNS, не TLS)", _is_tls_fail("rc=6") is False)

# GET-проба живости зеркала (fix для HEAD rc=35): rc=63 (--max-filesize
# сработал на большом файле) + HTTP 200 = зеркало живо; TLS-облом/4xx — нет.
class _FakeProc:
    def __init__(self, rc, out):
        self.returncode, self.stdout = rc, out


import modules.download as dl_mod                          # noqa: E402
_orig_run = dl_mod.subprocess.run
try:
    dl_mod.subprocess.run = lambda *a, **k: _FakeProc(63, b"301\n200\n")
    check("GET-проба: rc=63 + HTTP 200 = зеркало живо",
          _curl_get_probe_ok("curl", "https://example/x") is True)
    dl_mod.subprocess.run = lambda *a, **k: _FakeProc(35, b"")
    check("GET-проба: TLS-облом = зеркало мертво",
          _curl_get_probe_ok("curl", "https://example/x") is False)
    dl_mod.subprocess.run = lambda *a, **k: _FakeProc(0, b"404")
    check("GET-проба: HTTP 404 = зеркало мертво",
          _curl_get_probe_ok("curl", "https://example/x") is False)
finally:
    dl_mod.subprocess.run = _orig_run

# ═══ 10. netcheck: диагностика «недоступные домены при живом интернете» ═══
import struct as _st                                # noqa: E402

# Кодирование имени в DNS-запросе (labels) и разбор ответа сcompression
_lbls = b"".join(bytes([len(p)]) + p.encode() for p in
                 "account.ely.by".split(".")) + b"\x00"
check("DNS labels кодируются по RFC",
      _lbls == b"\x07account\x03ely\x02by\x00")

_hdr = _st.pack(">HHHHHH", 0xBEEF, 0x8000, 1, 1, 0, 0)
_q = _lbls + _st.pack(">HH", 1, 1)
_ans = b"\xc0\x0c" + _st.pack(">HHIH", 1, 1, 300, 4) + bytes([93, 184, 88, 88])
_fake_dns = _hdr + _q + _ans


class _FakeSock:
    def settimeout(self, *a): pass
    def sendto(self, *a): pass
    def recvfrom(self, *a): return (_fake_dns, None)
    def close(self): pass


_real_sock = dl_mod.socket.socket
try:
    dl_mod.socket.socket = lambda *a, **k: _FakeSock()
    check("_dns_query_a парсит A-запись из ответа",
          dl_mod._dns_query_a("account.ely.by", "8.8.8.8") == ["93.184.88.88"])
finally:
    dl_mod.socket.socket = _real_sock

# Вердикты diagnose_dns: системный DNS молчит, публичный отвечает = dns_block
_orig_query = dl_mod._dns_query_a
_orig_gai = dl_mod.socket.getaddrinfo
try:
    dl_mod.socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(
        OSError("no dns"))
    dl_mod._dns_query_a = lambda h, s, timeout=4: ["1.2.3.4"]
    r = dl_mod.diagnose_dns(["blocked.example"])
    check("verdict=dns_block при пустом системном DNS",
          r["blocked.example"]["verdict"] == "dns_block")
    dl_mod._dns_query_a = lambda h, s, timeout=4: []
    r = dl_mod.diagnose_dns(["dead.example"])
    check("verdict=no_internet если и публичный DNS молчит",
          r["dead.example"]["verdict"] == "no_internet")
    # LOPHIBUR-заглушка (127.x / 128.0.101.x) тоже считается блокировкой
    dl_mod.socket.getaddrinfo = lambda *a, **k: [(2, 1, 0, "", ("127.0.0.1", 0))]
    dl_mod._dns_query_a = lambda h, s, timeout=4: ["1.2.3.4"]
    r = dl_mod.diagnose_dns(["lophi.example"])
    check("LOPHIBUR 127.x → dns_block, а не ok",
          r["lophi.example"]["verdict"] == "dns_block")
finally:
    dl_mod._dns_query_a = _orig_query
    dl_mod.socket.getaddrinfo = _orig_gai

# Команда зарегистрирована в реестре и в EXPECTED_COMMANDS
from modules.config import EXPECTED_COMMANDS   # noqa: E402
from modules.ui import COMMAND_REGISTRY        # noqa: E402
check("netcheck в EXPECTED_COMMANDS", "netcheck" in EXPECTED_COMMANDS)
check("netcheck зарегистрирован в COMMAND_REGISTRY",
      "netcheck" in COMMAND_REGISTRY)

# ═══ 11. Legacy: общий бюджет curl-попыток, честный валидатор, один круг ═══
import modules.download as dl_mod2              # noqa: E402

# 11.1 Бюджет попыток: очередь из 20 зеркал не даёт 20 заходов
check("_curl_attempt_budget ограничен MAX_CURL_QUEUE_ATTEMPTS",
      dl_mod2._curl_attempt_budget([("m", f"http://h{i}") for i in range(20)])
      == dl_mod2.MAX_CURL_QUEUE_ATTEMPTS)
check("_curl_attempt_budget на короткой очереди = len(mirrors)",
      dl_mod2._curl_attempt_budget([("a", "u1"), ("b", "u2")]) == 2)

# 11.2 run_curl_queues: budget/truncated + разделение между очередями
_orig_dqwc = dl_mod2.download_queue_with_curl
_orig_rcq = dl_mod2.run_curl_queues
_orig_which = dl_mod2.shutil_which_curl
_seen_submits = []
try:
    dl_mod2.shutil_which_curl = lambda: "/usr/bin/curl"
    dl_mod2.probe_mirrors_with_curl = lambda m, budget_sec=25: list(m)
    dl_mod2.download_queue_with_curl = None  # placeholder, см. ниже

    def _fake_dqwc(mirrors, dest, min_bytes=1024, silent=False,
                   validator=None, max_attempts=None):
        # эмулируем «сеть рвёт всё»: ничего не скачиваем, помечаем заходы
        _seen_submits.append((len(mirrors), max_attempts))
        return None

    dl_mod2.download_queue_with_curl = _fake_dqwc
    q1 = [(f"jar{i}", f"http://j{i}") for i in range(6)]
    q2 = [("cdn-exe", "http://cdn/exe")]
    r = dl_mod2.run_curl_queues([q1, q2], tmp / "nope.bin", min_bytes=1)
    check("run_curl_queues: budget = min(total, MAX)",
          r["budget"] == min(7, dl_mod2.MAX_CURL_QUEUE_ATTEMPTS))
    check("run_curl_queues: без усечения при 7 зеркалах",
          r["truncated"] is False and r["ok"] is False)
    # 6+1=7 <= 8: обе очереди прогнаны целиком; хвостовая очередь НЕ съезжает
    check("run_curl_queues: последняя очередь не обрезается (запасной CDN жив)",
          _seen_submits and _seen_submits[-1][0] == 1)

    _seen_submits.clear()
    big = [(f"m{i}", f"http://m{i}") for i in range(12)]
    r2 = dl_mod2.run_curl_queues([big, big[:3]], tmp / "nope2.bin",
                                 min_bytes=1)
    check("run_curl_queues: truncated=True когда зеркал больше бюджета",
          r2["truncated"] is True and r2["budget"] == 8)
    total_tries = sum(n for n, _ in _seen_submits)
    check("run_curl_queues: суммарные заходы не превышают бюджет",
          total_tries <= 8)

    # allow_full — старое поведение полного перебора
    _seen_submits.clear()
    r3 = dl_mod2.run_curl_queues([big], tmp / "nope3.bin", min_bytes=1,
                                 allow_full=True)
    check("allow_full=True снимает усечение", r3["truncated"] is False
          and r3["budget"] == 12)
finally:
    dl_mod2.download_queue_with_curl = _orig_dqwc
    dl_mod2.shutil_which_curl = _orig_which

# 11.3 download_queue_with_curl с validator: .exe вместо .jar ОТВЕРГАЕТСЯ
# (раньше файл по размеру считался успехом, следующий этап его ломал —
# и весь круг загрузки повторялся заново: простыня «curl не смог»).
# Упрощение (проблема №2): теперь один тип файла — portable .jar;
# строгий валидатор PK-.jar, никаких «всеядных» PK|MZ-валидаторов и
# .exe-запасных очередей.
exe_jar = tmp / "FakeLauncher.jar"
exe_jar.write_bytes(b"MZ\x90\x00" + b"\x00" * (2 * 1024 * 1024))
_orig_dlvcurl = dl_mod2.download_via_curl
_orig_probe = dl_mod2.probe_mirrors_with_curl
_orig_which2 = dl_mod2.shutil_which_curl
try:
    dl_mod2.shutil_which_curl = lambda: "/usr/bin/curl"
    dl_mod2.probe_mirrors_with_curl = lambda m, budget_sec=25: list(m)

    def _fake_dlvcurl(url, dest, timeout=60, extra=None, return_error=False):
        # curl «успешно» скачал: MZ-установщик лежит в .jar-файле
        p = Path(str(dest) + ".part") if str(dest).endswith(".part") \
            else None
        tgt = p if (p and p.exists()) else Path(dest)
        try:
            tgt.write_bytes(exe_jar.read_bytes())
            return "" if return_error else True
        except OSError:
            return "disk error" if return_error else False

    dl_mod2.download_via_curl = _fake_dlvcurl
    got = dl_mod2.download_queue_with_curl(
        [("mirror1", "http://m1"), ("mirror2", "http://m2")], exe_jar,
        min_bytes=1024, silent=True, validator=mc._looks_like_jar)
    check("validator: .exe под видом .jar отклоняется (не success)",
          got is None)
    check("отвергнутый мусор удалён с диска", not exe_jar.exists())

    # Настоящий PK-.jar той же очередью принимается (строгий валидатор
    # не «перегибает»: валидный артефакт — успех с первого зеркала).
    def _fake_dlvcurl_jar(url, dest, timeout=60, extra=None,
                          return_error=False):
        p = Path(str(dest) + ".part") if str(dest).endswith(".part") \
            else None
        tgt = p if (p and p.exists()) else Path(dest)
        try:
            tgt.write_bytes(b"PK\x03\x04" + b"\x00" * (2 * 1024 * 1024))
            return "" if return_error else True
        except OSError:
            return "disk error" if return_error else False

    dl_mod2.download_via_curl = _fake_dlvcurl_jar
    good_dest = tmp / "GoodLauncher.jar"
    got2 = dl_mod2.download_queue_with_curl(
        [("mirrorX", "http://mx")], good_dest, min_bytes=1024, silent=True,
        validator=mc._looks_like_jar)
    check("validator: настоящий PK-.jar принимается",
          got2 == "http://mx")
    if good_dest.exists():
        good_dest.unlink()
    # max_attempts=0 — очередь не гоняется вообще (бюджет исчерпан)
    got3 = dl_mod2.download_queue_with_curl(
        [("mirrorY", "http://my")], tmp / "never.bin", min_bytes=1024,
        silent=True, max_attempts=0)
    check("max_attempts=0 не делает ни одной попытки", got3 is None)
finally:
    dl_mod2.download_via_curl = _orig_dlvcurl
    dl_mod2.probe_mirrors_with_curl = _orig_probe
    dl_mod2.shutil_which_curl = _orig_which2
    try:
        exe_jar.unlink()
    except OSError:
        pass

# 11.4 Кэш portable .jar: _cached_jar_ok принимает только живой PK-.jar
# достаточного размера; HTML-заглушка фильтра и огрызок докачки — нет.
ledir = tmp / "legacy-portable"
ledir.mkdir(exist_ok=True)
good_jar = ledir / "LegacyLauncher.jar"
good_jar.write_bytes(b"PK\x03\x04" + b"\x00" * (2 * 1024 * 1024))
check("_cached_jar_ok принимает настоящий PK-.jar", mc._cached_jar_ok(good_jar))
good_jar.write_bytes(b"<html>blocked</html>" * 100000)
check("_cached_jar_ok отвергает HTML-заглушку фильтра",
      not mc._cached_jar_ok(good_jar))
good_jar.write_bytes(b"PK\x03\x04" + b"\x00" * 1024)
check("_cached_jar_ok отвергает огрызок (< LEGACY_JAR_MIN_SIZE)",
      not mc._cached_jar_ok(good_jar))
# .exe-установщик больше нигде не считается валидным артефактом Legacy
check("всеядный PK|MZ-валидатор (.exe как успех) удалён",
      not hasattr(mc, "_is_exe_installer"))
check(".exe-запасные очереди/хелперы (.exe cached/fallback) удалены",
      not hasattr(mc, "_exe_cached_ok")
      and not hasattr(mc, "_exe_cached_path")
      and not hasattr(mc, "_exe_size_ok")
      and not hasattr(mc, "_legacy_exe_fallback"))

# 11.5 _legacy_curl_once: быстрый прямой заход canonical URL (llaun.ch/jar,
# как браузер), затем ОДИН прогон единой jar-очереди со строгим
# валидатором; битый огрызок на диске удаляется (не имитирует успех).
ledir2 = tmp / "lp2"
ledir2.mkdir(exist_ok=True)
cand2 = ledir2 / "LegacyLauncher.jar"
_seen_rq = []
_seen_direct = []


def _fake_run_curl_queues_fail(queues, dest, **kw):
    _seen_rq.append((queues, kw.get("validator")))
    cand2.write_bytes(b"MZ" + b"\x00" * (3 * 1024 * 1024))  # CDN отдал .exe
    return {"ok": False, "url": None, "attempts": 3, "budget": 3,
            "truncated": False, "mirrors_total": 3}


def _fake_dl_with_curl_fail(url, dest, **kw):
    # Прямой заход canonical URL не срабатывает (например, DNS перехвачен)
    # -> должен оставаться запасной путь: единая очередь зеркал.
    _seen_direct.append(url)
    return False


_orig_dlwc = dl_mod2.download_with_curl
dl_mod2.run_curl_queues = _fake_run_curl_queues_fail
try:
    # download_with_curl импортируется внутри функции — патчим модуль source.
    dl_mod2.download_with_curl = _fake_dl_with_curl_fail
    resume = mc._legacy_curl_once(cand2)
    check("_legacy_curl_once: сначала прямой заход canonical llaun.ch/jar",
          _seen_direct == [cfg.LEGACY_MANUAL_JAR_URL])
    q, val = _seen_rq[0]
    check("_legacy_curl_once: одна очередь (не jar+exe)", len(q) == 1)
    check("_legacy_curl_once: очередь = legacy_jar_mirrors(True)",
          q[0] == cfg.legacy_jar_mirrors(True))
    check("_legacy_curl_once: строгий PK-.jar валидатор",
          val is mc._looks_like_jar)
    check("_legacy_curl_once: .exe-огрызок удалён с диска",
          not cand2.exists())
    check("_legacy_curl_once возвращает резюме прогона",
          resume["ok"] is False and resume["attempts"] == 3)

    # Успех прямого пути: queue-зеркала вообще не нужны.
    _seen_rq.clear()
    _seen_direct.clear()

    def _fake_dl_with_curl_ok(url, dest, **kw):
        _seen_direct.append(url)
        Path(dest).write_bytes(b"PK\x03\x04" + b"\x00" * (2 * 1024 * 1024))
        return True

    dl_mod2.download_with_curl = _fake_dl_with_curl_ok
    resume2 = mc._legacy_curl_once(cand2)
    check("_legacy_curl_once: прямойcanonical-успех без очереди зеркал",
          _seen_rq == [] and resume2["ok"] is True
          and resume2["url"] == cfg.LEGACY_MANUAL_JAR_URL)
finally:
    dl_mod2.run_curl_queues = _orig_rcq
    dl_mod2.download_with_curl = _orig_dlwc

# 11.6 setup_legacy(): Windows-ветка делает ОДИН curl-прогон (не три круга),
# только portable .jar, без смешения типов файлов.
# Проверяем исходник функции статически: форсировать IS_WINDOWS нельзя —
# `from ... import IS_WINDOWS` связан при импорте модуля.
seg_src = open(ROOT / "modules" / "minecraft.py", encoding="utf-8").read()
i0 = seg_src.index("def setup_legacy():")
i1 = seg_src.index("# ═", i0 + 10)
seg = seg_src[i0:i1]
check("setup_legacy: единая точка curl-загрузки (_legacy_curl_once)",
      "_legacy_curl_once(cand)" in seg
      and "download_queue_with_curl(" not in seg)
check("setup_legacy: второй полный круг jar-зеркал убран "
      "(нет повторного legacy_jar_mirrors внутри)",
      seg.count("legacy_jar_mirrors") == 0)
# Срез «кода» функции: строки с # (комментарии) и docstring — в них
# исторические упоминания .exe допустимы; проверяем именно исполняемый код.
seg_code = "\n".join(ln for ln in seg.splitlines()
                     if not ln.strip().startswith("#"))
i_doc = seg_code.index('"""')
i_doc2 = seg_code.index('"""', i_doc + 3)
seg_code = seg_code[:i_doc] + seg_code[i_doc2 + 3:]
check("setup_legacy: .exe-загрузка выметена из Windows-ветки "
      "(один тип файла — portable .jar, без запасных exe-очередей)",
      "_exe" not in seg_code and ".exe" not in seg_code
      and "download_via_curl" not in seg_code)
check("_legacy_curl_once прогоняет ЕДИНУЮ jar-очередь через run_curl_queues",
      "run_curl_queues([legacy_jar_mirrors(True)]," in seg_src)

# ═══ 12. TLauncher: portable bootstrap .jar, одна цепочка зеркал ═══
import modules.minecraft as mc_mod

# 12.1 Константы конфигурации
check("конфиг: TLAUNCHER_* заданы",
      cfg.TLAUNCHER_DIR_NAME == "tlauncher"
      and cfg.TLAUNCHER_JAR_NAME.endswith(".jar")
      and cfg.TLAUNCHER_MIN_SIZE >= 1024 * 1024
      and len(cfg.TLAUNCHER_API_URLS) >= 1)
check("конфиг: зеркала TLauncher — только https .jar (без .exe)",
      all(u.startswith("https://") and u.endswith(".jar")
          for _n, u in cfg.TLAUNCHER_API_URLS))
check("команда tlauncher в EXPECTED_COMMANDS", "tlauncher" in cfg.EXPECTED_COMMANDS)

# 12.2 Валидатор кэша: принимается только PK-.jar достаточного размера;
# HTML-заглушка и .exe-огрызок отвергаются (тот же строгий подход, что legacy).
tl_dir = tmp / "tlauncher"
tl_dir.mkdir(parents=True, exist_ok=True)
good_tl = tl_dir / cfg.TLAUNCHER_JAR_NAME
good_tl.write_bytes(b"PK\x03\x04" + b"\x00" * cfg.TLAUNCHER_MIN_SIZE)
bad_tl = tl_dir / "TLauncher-html.jar"
bad_tl.write_text("<html>blocked</html>", encoding="utf-8")
exe_tl = tl_dir / "TLauncher-installer.exe"
exe_tl.write_bytes(b"MZ\x90\x00" + b"\x00" * cfg.TLAUNCHER_MIN_SIZE)
_orig_wd = mc_mod.WINE_DIR
mc_mod.WINE_DIR = tmp                     # _tlauncher_dir -> tmp/tlauncher
try:
    found = mc_mod._find_local_tlauncher_jar()
    check("_find_local_tlauncher_jar находит живой PK-.jar из папки tlauncher/",
          found is not None and found.name == cfg.TLAUNCHER_JAR_NAME)
    good_tl.unlink()                        # остаётся только мусор
    import shutil as _sh
    _sh.copy(str(bad_tl), str(tl_dir / cfg.TLAUNCHER_JAR_NAME))
    found2 = mc_mod._find_local_tlauncher_jar()
    check("_find_local_tlauncher_jar ОТВЕРГАЕТ HTML-заглушку под именем кэша",
          found2 is None)
    (tl_dir / cfg.TLAUNCHER_JAR_NAME).unlink()
    # .exe даже гигантский не принимается (установщики не поддерживаем)
    big_exe = tl_dir / cfg.TLAUNCHER_JAR_NAME
    big_exe.write_bytes(b"MZ\x90\x00" + b"\x00" * cfg.TLAUNCHER_MIN_SIZE)
    found3 = mc_mod._find_local_tlauncher_jar()
    check("_find_local_tlauncher_jar ОТВЕРГАЕТ MZ-огрызок/установщик",
          found3 is None)
finally:
    mc_mod.WINE_DIR = _orig_wd

# 12.3 _download_tlauncher_jar: сначала прямой curl (как браузер), затем
# очередь download_file; HTML-огрызок после неудачи удаляется с диска.
seen_tl = {"curl": [], "df": []}


def _tl_curl_fail(url, dest, timeout=60):
    seen_tl["curl"].append(url)
    return False


def _tl_df_ok(mirrors, dest, label, **kw):
    seen_tl["df"].append([m[1] for m in mirrors])
    Path(dest).write_bytes(b"PK\x03\x04" + b"\x00" * cfg.TLAUNCHER_MIN_SIZE)
    return True


dest_tl = tmp / "tl-dl" / cfg.TLAUNCHER_JAR_NAME
_orig_dwc = dl_mod.download_with_curl
_orig_df_tl = mc_mod.download_file
dl_mod.download_with_curl = _tl_curl_fail
mc_mod.download_file = _tl_df_ok
try:
    got = mc_mod._download_tlauncher_jar(dest_tl)
    check("_download_tlauncher_jar: при отказе прямого curl качает "
          "по ЕДИНОЙ очереди зеркал TLAUNCHER_API_URLS",
          got is not None and seen_tl["curl"] == [cfg.TLAUNCHER_API_URLS[0][1]]
          and seen_tl["df"][0] == [u for _n, u in cfg.TLAUNCHER_API_URLS])
    dest_tl.unlink()

    # Прямой curl успешен → зеркала вообще не трогаются
    def _tl_curl_ok(url, dest, timeout=60):
        seen_tl["curl"].append(url)
        Path(dest).write_bytes(b"PK\x03\x04" + b"\x00" * cfg.TLAUNCHER_MIN_SIZE)
        return True
    dl_mod.download_with_curl = _tl_curl_ok
    seen_tl["curl"], seen_tl["df"] = [], []
    got2 = mc_mod._download_tlauncher_jar(dest_tl)
    check("_download_tlauncher_jar: прямая закачка curl = успех без очереди",
          got2 is not None and seen_tl["df"] == [])
    dest_tl.unlink()

    # Полный провал: HTML-огрызок не оставляем на диске
    def _tl_df_html(mirrors, dest, label, **kw):
        Path(dest).write_text("<html>403</html>", encoding="utf-8")
        return True     # download_file «думает», что скачал — решает валидатор
    dl_mod.download_with_curl = _tl_curl_fail
    mc_mod.download_file = _tl_df_html
    got3 = mc_mod._download_tlauncher_jar(dest_tl)
    check("_download_tlauncher_jar: HTML-огрызок отвергнут и удалён",
          got3 is None and not dest_tl.exists())
finally:
    dl_mod.download_with_curl = _orig_dwc
    mc_mod.download_file = _orig_df_tl

# 12.4 Интеграция в UI: команда ведёт в setup_tlauncher, запуск переиспользует
# исправленный _run_legacy_jar (-D свойства ДО -jar).
src_ui = open(ROOT / "modules" / "ui.py", encoding="utf-8").read()
check("ui: команда tlauncher привязана к setup_tlauncher",
      '"tlauncher": lambda a="": setup_tlauncher()' in src_ui
      and "setup_tlauncher," in src_ui)
seg_tl_src = seg_src[seg_src.index("def setup_tlauncher():"):]
seg_tl_src = seg_tl_src[:seg_tl_src.index("\n\n\n")]
check("setup_tlauncher: запуск через _run_legacy_jar (правильный порядок java-аргументов)",
      "_run_legacy_jar(jar_path)" in seg_tl_src)
check("setup_tlauncher: Java проверяется до сетевых операций",
      seg_tl_src.index("find_java()") < seg_tl_src.index("_download_tlauncher_jar"))

print()
if fails:
    print(f"ПРОВАЛОВ: {len(fails)} -> {fails}")
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
shutil.rmtree(tmp, ignore_errors=True)
