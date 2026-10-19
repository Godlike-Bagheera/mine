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

from modules.download import _looks_binary, download_file          # noqa: E402
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

print()
if fails:
    print(f"ПРОВАЛОВ: {len(fails)} -> {fails}")
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
shutil.rmtree(tmp, ignore_errors=True)
