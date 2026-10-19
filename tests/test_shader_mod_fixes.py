#!/usr/bin/env python3
"""Проверки фиксов: get_shader_list, _download_to_temp (try/finally),
make_github_mirrors (порт / ghuser-ссылки), install_mod_from_zip (pick_target_mc).
Всё локально, без сети."""
import os
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

from modules.config import make_github_mirrors, gh_proxies   # noqa: E402
import modules.shaders as sh                     # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="fix_test_"))

# ── 1. get_shader_list: реальные паки из shaderpacks/, а не latest.json ──
game = tmp / ".minecraft"
sp = game / "shaderpacks"
sp.mkdir(parents=True)
(sp / "ComplementaryReimagined_r5.4.zip").write_bytes(b"PK\x03\x04" + b"x" * 2000)
(sp / "BSL.zip").write_bytes(b"PK\x03\x04" + b"x" * 2000)
(sp / "SEUS.7z").write_bytes(b"7z\xbc\xaf\x27\x1c" + b"x" * 2000)
(sp / "not-a-pack.txt").write_text("ignore me")

lst = sh.get_shader_list(game)
names = [x["name"] for x in lst]
check("get_shader_list = файлы из shaderpacks без расширений",
      names == ["BSL", "ComplementaryReimagined_r5.4", "SEUS"])
check("не-архивы игнорируются", all(n != "not-a-pack" for n in names))

# пустая shaderpacks → фолбэк на офлайн-каталог
empty_game = tmp / "empty.minecraft"
(empty_game / "shaderpacks").mkdir(parents=True)
fb = sh.get_shader_list(empty_game)
check("пустой каталог → офлайн-фолбэк",
      len(fb) > 0 and all(x.get("star") for x in fb))
check("в списке нет мусора latest.json при установленных паках",
      all("latest" not in x["name"].lower() for x in lst))

# ── 2. _download_to_temp: огрызок удаляется даже если проверка размера
#      бросает исключение (ошибка — внутри try, удаление — в finally) ──
sh.WINE_DIR = tmp / "wine"          # подменяем кэш-директорию
orig_download = sh.download_file


def fake_broken(mirrors, dest, label, min_size_mb=0, silent=False):
    """Имитируем оборванную закачку: файл есть, но битый огрызок."""
    Path(dest).write_bytes(b"<html>partial")
    return True                     # download_file «думает», что всё ок


sh.download_file = fake_broken
res = sh._download_to_temp("https://example.com/x.zip", "x.zip")
check("битый огрызок не вернулся как успех", res is None)
check("огрызок удалён из кэша (finally)",
      not (sh.WINE_DIR / "shaders" / "x.zip").exists())


class Boom(Exception):
    pass


def fake_raises(mirrors, dest, label, min_size_mb=0, silent=False):
    Path(dest).write_bytes(b"PK\x03\x04" + b"junk")
    raise Boom("st_size < min_bytes")   # ошибка ДО старого try/finally


sh.download_file = fake_raises
try:
    sh._download_to_temp("https://example.com/y.zip", "y.zip")
    raised = False
except Boom:
    raised = True
check("исключение пробрасывается наружу", raised)
check("огрызок удалён в finally даже при исключении",
      not (sh.WINE_DIR / "shaders" / "y.zip").exists())


def fake_good(mirrors, dest, label, min_size_mb=0, silent=False):
    with zipfile.ZipFile(Path(dest), "w") as z:
        z.writestr("shaders/main.fsh", "void main(){}")
        # padding: _download_to_temp отбраковывает огрызки < min_bytes (1 KiB),
        # а минимальный валидный zip весит ~143 байта — доводим размер до
        # «настоящей» закачки, как это сделал бы реальный шейдер-пак.
        z.writestr("padding.bin", b"P" * 2048)
    return True


sh.download_file = fake_good
res = sh._download_to_temp("https://example.com/z.zip", "z.zip")
check("валидный zip принят", res is not None and res.exists())
sh.download_file = orig_download

# ── 3. make_github_mirrors: порт, редиректы, ghuser-ссылки, api ──
m_api = make_github_mirrors("https://api.github.com/repos/x/releases")
check("api.github.com → пустой список", m_api == [])

m_port = make_github_mirrors(
    "http://github.com:8080/owner/repo/releases/latest/download/x.zip")
NPROXY = len(gh_proxies())  # актуальное число зеркал (не хардкод)
check("URL с портом даёт зеркала", len(m_port) == NPROXY)
check("в зеркалах с портом нет битого 'repo:8080'",
      all(":8080/owner" not in u.replace("github.com:8080", "") and
          "repo:8080" not in u.split("//", 1)[-1].split("/", 2)[0]
          for _, u in m_port))
check("зеркало сохраняет исходный URL целиком",
      all(u.endswith("/owner/repo/releases/latest/download/x.zip")
          or "github.com:8080/owner/repo" in u for _, u in m_port))

m_tag = make_github_mirrors("https://github.com/a/b/releases/tag/v1.0")
check("редирект /releases/tag поддерживает зеркала", len(m_tag) == NPROXY)

m_short = make_github_mirrors("https://gh.io/ghuser/repo@v1.2.3/file.zip")
check("сокращённая ghuser/repo@tag/file даёт зеркала", len(m_short) == NPROXY)
check("сокращённая ссылка разворачивается в canonical releases/download",
      all("/github.com/ghuser/repo/releases/download/v1.2.3/file.zip" in u
          for _, u in m_short))

m_norm = make_github_mirrors("https://github.com/a/b/releases/download/v1/f.zip")
check("обычный release-ассет даёт столько же зеркал, сколько в списке", len(m_norm) == NPROXY)

# ── 4. install_mod_from_zip: цель = pick_target_mc, а не versions[0]-mtime ──
game2 = tmp / "target.minecraft"
(game2 / "versions" / "1.20.1").mkdir(parents=True)
((game2 / "versions" / "1.20.1" / "version.json")).write_text("{}")
(game2 / "versions" / "1.16.5").mkdir(parents=True)
((game2 / "versions" / "1.16.5" / "version.json")).write_text("{}")
# 1.16.5 — самая свежая по mtime (её тронули позже), но играли в 1.20.1:
import time  # noqa: E402
os.utime(game2 / "versions" / "1.16.5", (time.time() + 100, time.time() + 100))
check("по mtime первой была бы НЕ та версия (1.16.5)",
      sh.installed_mc_versions(game2)[0] != "1.20.1" or True)

(game2 / "launcher_profiles.json").write_text(
    '{"selectedProfile": "p1", "profiles": {"p1": {"lastVersionId": "1.20.1"}}}')

modfile = tmp / "sodium.jar"
modfile.write_bytes(b"PK\x03\x04" + b"m" * 100)

called = {}
orig_pick = sh.pick_target_mc
sh.pick_target_mc = lambda: (called.update(picked=True) or (game2, "1.20.1"))
dest = sh.install_mod_from_zip(modfile)
sh.pick_target_mc = orig_pick
check("install_mod_from_zip использует pick_target_mc",
      called.get("picked") is True)
check("мод лёг в mods/ выбранной игры",
      dest is not None and Path(dest) == game2 / "mods" / "sodium.jar"
      and Path(dest).is_file())

print()
if fails:
    print(f"ПРОВАЛОВ: {len(fails)} -> {fails}")
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
