#!/usr/bin/env python3
"""Mock-тесты установки Optimized fps (Forge) и Luxium — без сети.

Подменяются только сетевые точки входа modules.minecraft
(_http_get_json / download_file) — проверяется ЛОГИКА выбора релиза,
извлечения профиля Forge из installer.jar, фильтра Oculus и порядка
источников Luxium (локальный JAR из репозитория важнее интернета).

Запуск: python tests/test_mock_ofps_luxium.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


os.environ["HOME"] = tempfile.mkdtemp(prefix="fakehome_ofps_")
os.environ["WI_FAKE_HOME"] = "1"
# Без ключа CurseForge API — тест фиксирует поведение «ключа нет»
os.environ.pop("WI_CF_API_KEY", None)

from modules import minecraft as mc  # noqa: E402
from modules.hash_utils import verify_sha1  # noqa: E402


fails = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        fails.append(name)


TMP = Path(tempfile.mkdtemp(prefix="mock_ofps_"))

# В тестовом HOME нет Luxium-файлов; в реальном репозитории они есть —
# изолируем поиск локальных jar, перенаправляя REPO_ROOT/WINE_DIR в TMP.
LUX_HOME = TMP / "luxhome"
LUX_HOME.mkdir()
mc.REPO_ROOT = LUX_HOME
mc.WINE_DIR = LUX_HOME

FORGE_DEP = {"project_id": "nUc7rf2x", "version_id": None}
NEO_FORGE_DEP = {"project_id": "gc8mA4Gz", "version_id": None}


def mrpack_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("modrinth.index.json", "{}")
    return buf.getvalue()


def v_file(url="https://example.com/pack.mrpack"):
    return [{"filename": "optimized-fps.mrpack", "url": url,
             "hashes": {}, "filesize": 123456}]


def make_release(ver, gvs, deps, files=None):
    return {"version_number": ver, "version_type": "release",
            "game_versions": gvs, "dependencies": deps,
            "files": files if files is not None else v_file()}


# ═══════════ 1. _pick_ofps_release: пин и forge-фильтр ═══════════
print("== 1. Выбор релиза Optimized fps ==")

pinned_forge = make_release("2.4.10", ["1.20.1"], [FORGE_DEP])
fresh_neoforge = make_release("4.4.0", ["26.1"], [NEO_FORGE_DEP])
older_forge = make_release("2.4.9", ["1.20.1"], [FORGE_DEP])
no_mrpack = make_release("2.4.8", ["1.20.1"], [FORGE_DEP], files=[])

# 1a. Pinned-версия доступна -> берётся она, список версий не трогаем
with mock.patch.object(mc, "_http_get_json", return_value=pinned_forge) as gj:
    rel = mc._pick_ofps_release()
check("pinned bf2aWUzR (Forge 1.20.1) выбран", rel and rel["version"] == "2.4.10")
check("запрошен именно /v2/version/<id>",
      gj.call_args[0][0].endswith("/v2/version/" + mc.OFPS_FORGE_VERSION_ID))

# 1b. Пин недоступен -> фолбек: первый release с forge + 1.20.x,
#     свежие NeoForge-релизы и версии без .mrpack пропускаются
def fake_json_fallback(url, timeout=30):
    if "/v2/version/" in url:
        raise RuntimeError("404 pinned")
    return [fresh_neoforge, no_mrpack, older_forge]

with mock.patch.object(mc, "_http_get_json", side_effect=fake_json_fallback):
    rel = mc._pick_ofps_release()
check("фолбек: NeoForge 4.4.0 и версия без .mrpack пропущены, "
      "взят Forge 2.4.9", rel and rel["version"] == "2.4.9")

# 1c. Подходящих Forge-релизов нет -> понятная ошибка
def fake_json_none(url, timeout=30):
    if "/v2/version/" in url:
        raise RuntimeError("404 pinned")
    return [fresh_neoforge]

with mock.patch.object(mc, "_http_get_json", side_effect=fake_json_none):
    try:
        mc._pick_ofps_release()
        raised = False
    except RuntimeError:
        raised = True
check("нет Forge 1.20.x релизов -> RuntimeError", raised)


# ═══════════ 2. _forge_build_from_metadata ═══════════
print("== 2. Сборка Forge из maven-metadata.xml ==")

METADATA_XML = """<metadata><versioning><versions>
<version>1.20.1-47.1.0</version>
<version>1.20.1-47.4.3</version>
<version>1.20.1-47.4.26</version>
<version>1.20.1-47.2.0</version>
<version>1.20.2-48.1.0</version>
</versions></versioning></metadata>"""


class FakeRespCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return METADATA_XML.encode()


with mock.patch.object(mc, "_http_get", return_value=FakeRespCtx()):
    build = mc._forge_build_from_metadata("1.20.1")
check("максимум 47.4.26 (не лексикографически 47.4.3/47.2.0)", build == "47.4.26")

with mock.patch.object(mc, "_http_get", return_value=FakeRespCtx()):
    check("для 1.20.2 своя ветка", mc._forge_build_from_metadata("1.20.2") == "48.1.0")
    check("несуществующая MC -> None", mc._forge_build_from_metadata("1.19.9") is None)

# promotions_slim мёртв (404) -> фолбек на metadata
with mock.patch.object(mc, "_http_get_json", side_effect=RuntimeError("404")), \
     mock.patch.object(mc, "_http_get", return_value=FakeRespCtx()):
    check("_forge_build_for падает на maven-metadata",
          mc._forge_build_for("1.20.1") == "47.4.26")


# ═══════════ 3. _forge_profile_from_installer ═══════════
print("== 3. Профиль Forge из installer.jar ==")

PROFILE = {"id": "1.20.1-forge-47.4.3", "inheritsFrom": "1.20.1",
           "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
           "libraries": [{"name": "net.minecraftforge:forge:1.20.1-47.4.3"}]}


def installer_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def fake_download_ok(mirrors, dest, label, **kw):
    Path(dest).write_bytes(installer_zip({"version.json": json.dumps(PROFILE)}))
    return True


def fake_download_fail(mirrors, dest, label, **kw):
    return False


orig_wine_dir = mc.WINE_DIR
mc.WINE_DIR = TMP
try:
    with mock.patch.object(mc, "download_file", side_effect=fake_download_ok):
        prof = mc._forge_profile_from_installer("1.20.1", "47.4.3")
    check("profile извлечён из installer.jar",
          prof and prof.get("mainClass", "").startswith("cpw.mods"))
    check("библиотеки сохранены", bool(prof and prof.get("libraries")))

    with mock.patch.object(mc, "download_file", side_effect=fake_download_fail):
        check("installer недоступен -> None (дальше фолбеки URL/заглушка)",
              mc._forge_profile_from_installer("1.20.1", "47.4.3") is None)

    def fake_download_broken(mirrors, dest, label, **kw):
        Path(dest).write_bytes(b"not a zip")
        return True

    with mock.patch.object(mc, "download_file", side_effect=fake_download_broken):
        check("битый installer -> None без исключения",
              mc._forge_profile_from_installer("1.20.1", "47.4.3") is None)
finally:
    mc.WINE_DIR = orig_wine_dir


# ═══════════ 4. Фильтр файлов .mrpack (Oculus / серверное) ═══════════
print("== 4. Пропуск Oculus и серверных файлов ==")
sk = mc._ofps_skip_entry
check("oculus-мод пропускается", sk({"path": "mods/oculus-mc1.20.1-1.6.9.jar",
                                     "env": {}}))
check("мод с -oculus в имени пропускается",
      sk({"path": "mods/some-dep-oculus.jar", "env": {}}))
check("Embeddium остаётся", not sk({"path": "mods/embeddium-0.3.1.jar", "env": {}}))
check("серверный файл пропускается",
      sk({"path": "mods/server-only.jar", "env": {"client": "unsupported"}}))
check("res/ файлы не режем", not sk({"path": "resources/whatever", "env": {}}))

game = TMP / "game"
(game / "mods").mkdir(parents=True, exist_ok=True)
(game / "mods" / "oculus-mc1.20.1-1.6.9.jar").write_bytes(b"x")
(game / "mods" / "embeddium.jar").write_bytes(b"y")
removed = mc._remove_oculus_from_mods(game)
check("_remove_oculus_from_mods удалил oculus и не тронул embeddium",
      removed == 1 and (game / "mods" / "embeddium.jar").exists()
      and not (game / "mods" / "oculus-mc1.20.1-1.6.9.jar").exists())


# ═══════════ 5. install_luxium_mod: порядок источников ═══════════
print("== 5. Luxium: локальный файл важнее сети ==")


def make_fake_lux(path):
    """Корректный zip-«мод» >5 МБ (append-блобом, чтобы EOCD читался)."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mods/luxium.accesswidener", "dummy")
        z.writestr("assets/blob.bin", os.urandom(6 * 1024 * 1024))
    return path


game2 = TMP / "game2"
game2.mkdir()
lux_jar = make_fake_lux(TMP / "Luxium-2.8.0.jar")

orig_repo_root = mc.REPO_ROOT
mc.REPO_ROOT = TMP           # «клон репозитория» для теста
try:
    with mock.patch.object(mc, "_http_get_json",
                           side_effect=AssertionError("сеть не нужна")):
        ok_flag = mc.install_luxium_mod(game2)
    check("JAR из корня репозитория скопирован в mods/ без сети",
          ok_flag and (game2 / "mods" / "Luxium-2.8.0.jar").exists())

    # уже установлен -> повторный вызов не копирует заново и не лезет в сеть
    with mock.patch.object(mc, "_http_get_json",
                           side_effect=AssertionError("сеть не нужна")):
        check("повторная установка = no-op (уже стоит)",
              mc.install_luxium_mod(game2))

    # ни локального файла, ни кэша, ни Modrinth, ни CF-ключа -> False + warn
    shutil.rmtree(TMP / "game3", ignore_errors=True)
    game3 = TMP / "game3"
    game3.mkdir()
    lux_jar.unlink()
    cache3 = TMP / "cache3"
    cache3.mkdir()
    with mock.patch.object(mc, "WINE_DIR", cache3), \
         mock.patch.object(mc, "_http_get_json", side_effect=RuntimeError("404")), \
         mock.patch.object(mc, "download_file", return_value=False):
        check("без локального файла и сети -> честный False",
              mc.install_luxium_mod(game3) is False)

    # CurseForge API c ключом: JSON {data: url} подхватывается
    cf_calls = []

    def fake_cf(mirrors, dest, label, **kw):
        cf_calls.append(mirrors[0])
        make_fake_lux(Path(dest))
        return True

    game4 = TMP / "game4"
    game4.mkdir()
    cache4 = TMP / "cache4"
    cache4.mkdir()
    with mock.patch.object(mc, "WINE_DIR", cache4), \
         mock.patch.dict(os.environ, {"WI_CF_API_KEY": "testkey"}), \
         mock.patch.object(mc, "_http_get_json",
                           return_value={"data": "https://edge.forgecdn.net/x.jar"}), \
         mock.patch.object(mc, "download_file", side_effect=fake_cf):
        check("WI_CF_API_KEY: Luxium ставится через CurseForge API",
              mc.install_luxium_mod(game4)
              and cf_calls and cf_calls[0][0] == "CurseForge API")

    # битый (<5 МБ / не zip) локальный jar игнорируется — код идёт в сеть
    bad = TMP / "Luxium-broken.jar"
    bad.write_bytes(b"not a zip at all")
    game5 = TMP / "game5"
    game5.mkdir()
    cache5 = TMP / "cache5"
    cache5.mkdir()
    with mock.patch.object(mc, "WINE_DIR", cache5), \
         mock.patch.object(mc, "_http_get_json", side_effect=RuntimeError("404")), \
         mock.patch.object(mc, "download_file", return_value=False):
        check("битый локальный jar не принимается за мод",
              mc.install_luxium_mod(game5) is False)
finally:
    mc.REPO_ROOT = orig_repo_root


# ═══════════ 6. install_embeddium_mod: офлайн-подхват и зеркала CDN ═══════════
print("== 6. Embeddium: локальный jar, no-op, зеркала Modrinth CDN ==")


def make_fake_embed(path):
    """Корректный zip-«мод» >200 КБ (порог _local_mod_jars)."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/mods.toml", 'modId="embeddium"\n')
        z.writestr("assets/blob.bin", os.urandom(300 * 1024))
    return path


emb_home = TMP / "embhome"
emb_home.mkdir()
orig_repo, orig_wine = mc.REPO_ROOT, mc.WINE_DIR
mc.REPO_ROOT = emb_home
mc.WINE_DIR = emb_home
try:
    # jar ЛЮБОГО имени в «корне репо» подхватывается без сети
    game_e1 = TMP / "gameE1"
    game_e1.mkdir()
    fake_emb = emb_home / "My Custom Embeddium.jar"
    make_fake_embed(fake_emb)
    # pin-валидация распространяется только на закоммиченный в репо файл;
    # для мока считаем этот jar ожидаемым (сверку с реальным пином проверяем ниже)
    with mock.patch.object(mc, "EMBEDDIUM_SHA1_PIN",
                           verify_sha1(fake_emb, mc.EMBEDDIUM_SHA1_PIN)[1]), \
         mock.patch.object(mc, "_http_get_json",
                           side_effect=AssertionError("сеть не нужна")):
        check("Embeddium: локальный jar любого имени скопирован офлайн",
              mc.install_embeddium_mod(game_e1)
              and (game_e1 / "mods" / "My Custom Embeddium.jar").exists())

    # уже стоит -> no-op, сеть не трогаем
    with mock.patch.object(mc, "_http_get_json",
                           side_effect=AssertionError("сеть не нужна")):
        check("Embeddium: повторная установка = no-op",
              mc.install_embeddium_mod(game_e1))

    # ни файла, ни сети -> честный False (и GitHub-фолбек тоже недоступен)
    game_e2 = TMP / "gameE2"
    game_e2.mkdir()
    (emb_home / "My Custom Embeddium.jar").unlink()
    with mock.patch.object(mc, "_http_get_json", side_effect=RuntimeError("404")), \
         mock.patch.object(mc, "download_file", return_value=False), \
         mock.patch.object(mc, "fo_mrpack_mirrors", return_value=[]):
        check("Embeddium: без файла и сети -> False",
              mc.install_embeddium_mod(game_e2) is False)

    # Modrinth API: фильтр по версии из mods.toml ([0.3.31,0.4)) + MC 1.20.1,
    # скачивание по прямому URL cdn.modrinth.com (рабочих зеркал не существует)
    def emb_files(url, timeout=30):
        assert url.startswith(mc.EMBEDDIUM_VERSIONS_API), url

        def ver(vn, loaders, fname=None):
            return {
                "version_number": vn, "game_versions": ["1.20.1"],
                "loaders": loaders, "status": "listed", "env": {},
                "files": [{"filename": fname or f"embeddium-{vn}.jar",
                           "url": "https://cdn.modrinth.com/data/sk9rgfiA/"
                                  + vn.replace(".", "_") + ".jar",
                           "hashes": {}}],
            }
        return [
            ver("0.5.4", ["forge"]),                       # вне диапазона
            ver("0.3.30", ["forge"]),                      # ниже минимума
            ver("0.3.31", ["fabric"]),                     # не forge
            {"version_number": "0.4.0", "game_versions": ["1.20.1"],
             "loaders": ["forge"], "status": "alpha", "env": {},
             "files": [{"filename": "embeddium-0.4.0-beta.jar",
                        "url": "https://cdn.modrinth.com/data/sk9rgfiA/beta.jar",
                        "hashes": {}}]},                  # не release
            {"version_number": "0.3.31+mc1.20.1",
             "game_versions": ["1.20.1"], "loaders": ["forge"],
             "status": "listed", "env": {},
             "files": [{"filename": "embeddium-0.3.31+mc1.20.1.pack",
                        "url": "https://cdn.modrinth.com/data/sk9rgfiA/no.jar",
                        "hashes": {}},                     # не .jar — пропускаем
                       {"filename": "embeddium-0.3.31+mc1.20.1.jar",
                        "url": "https://cdn.modrinth.com/data/sk9rgfiA/emb.jar",
                        "hashes": {}}]},                   # подходит
        ]

    emb_dl = []

    def fake_emb_download(mirrors, dest, label, **kw):
        if "github.com" in mirrors[0][1]:      # GitHub-фолбек (new) — пропускаем
            return False
        emb_dl.append([m[1] for m in mirrors])
        make_fake_embed(Path(dest))
        return True

    game_e3 = TMP / "gameE3"
    game_e3.mkdir()
    with mock.patch.object(mc, "_http_get_json", side_effect=emb_files), \
         mock.patch.object(mc, "download_file", side_effect=fake_emb_download):
        ok_e3 = mc.install_embeddium_mod(game_e3)
    check("Embeddium: взята версия из диапазона mods.toml (0.3.31, не 0.5.4)",
          ok_e3 and (game_e3 / "mods" / "embeddium-0.3.31+mc1.20.1.jar").exists())
    urls = emb_dl[0] if emb_dl else []
    check("Embeddium: alpha/beta-версии отсеиваются фильтром status",
          all("beta" not in u for u in emb_dl[0]))
    check("Embeddium: качаем по прямому cdn-URL (рабочих зеркал нет, см. MRCDN_PROXIES)",
          len(urls) == 1 and urls[0] == "https://cdn.modrinth.com/data/sk9rgfiA/emb.jar")
finally:
    mc.REPO_ROOT, mc.WINE_DIR = orig_repo, orig_wine


    # SHA-pin: битая/подменённая копия из КОРНЯ РЕПО отвергается офлайн,
    # сеть не трогается вовсе -> честный False с подсказкой
    game_e5 = TMP / "gameE5"
    game_e5.mkdir()
    make_fake_embed(emb_home / "embeddium-tampered.jar")   # sha1 != PIN
    with mock.patch.object(mc, "_http_get_json",
                           side_effect=AssertionError("сеть не нужна")), \
         mock.patch.object(mc, "download_file", return_value=False):
        check("Embeddium: jar из repo_root с неверным SHA-1 отвергнут (False)",
              mc.install_embeddium_mod(game_e5) is False
              and not (game_e5 / "mods" / "embeddium-tampered.jar").exists())

    # jar из КЭША (WINE_DIR, не REPO_ROOT) принимается без сверки pin —
    # это осознанный выбор пользователя
    game_e6 = TMP / "gameE6"
    game_e6.mkdir()
    cache_jar = emb_home / "embeddium-from-cache.jar"
    make_fake_embed(cache_jar)
    cache_dir = TMP / "embcache"
    cache_dir.mkdir()
    shutil.copy2(cache_jar, cache_dir / cache_jar.name)
    empty_repo = TMP / "emproot"
    empty_repo.mkdir()
    mc.REPO_ROOT = empty_repo             # REPO_ROOT пуст -> jar только в кэше
    mc.WINE_DIR = cache_dir
    with mock.patch.object(mc, "_http_get_json",
                           side_effect=AssertionError("сеть не нужна")):
        check("Embeddium: jar из кэша принимается без сверки pin",
              mc.install_embeddium_mod(game_e6)
              and (game_e6 / "mods" / "embeddium-from-cache.jar").exists())
    mc.REPO_ROOT = emb_home
    cache_jar.unlink()
    (emb_home / "embeddium-tampered.jar").unlink()

# ═══════════ 7. Парсер mods.toml Luxium (реальный формат [[dependencies.*]]) ═══════════
print("== 7. embeddium_version_range: чтение mods.toml ==")

MODS_TOML = '''modLoader="javafml"
loaderVersion="[47,)"
[[mods]]
modId="luxium"
[[dependencies.luxium]]
modId="forge"
mandatory=true
versionRange="[47.1.3,)"
[[dependencies.luxium]]
modId="embeddium"
mandatory=true
versionRange="[0.3.31,0.4)"
[[dependencies.luxium]]
modId="optionalthing"
mandatory=false
versionRange="[9.9,)"
'''

emb_home2 = TMP / "embhome2"
emb_home2.mkdir()
with zipfile.ZipFile(emb_home2 / "Luxium-test.jar", "w") as z:
    z.writestr("META-INF/mods.toml", MODS_TOML)
import modules.config as cfg  # noqa: E402
orig_cfg_repo = cfg.REPO_ROOT
cfg.REPO_ROOT = emb_home2
try:
    deps = cfg.luxium_required_deps()
    check("парсер читает секции [[dependencies.luxium]]",
          deps.get("embeddium") == ("0.3.31", "0.4"))
    check("forge-зависимость тоже распарсена",
          deps.get("forge") == ("47.1.3", None))
    check("mandatory=false игнорируется", "optionalthing" not in deps)
    check("embeddium_version_range() согласована с парсером",
          cfg.embeddium_version_range() == ("0.3.31", "0.4"))
finally:
    cfg.REPO_ROOT = orig_cfg_repo

check("embeddium_version_range доступна из minecraft (импортирована)",
      callable(mc.embeddium_version_range))
check("_ver_tuple сравнивает числа, а не строки",
      mc._ver_tuple("0.3.31") < mc._ver_tuple("0.4")
      and mc._ver_tuple("0.10.0") > mc._ver_tuple("0.9.9"))

# ═══════════ 8. Luxium 2.8.4 (Fabric) + FO v5.2.8 — приоритетный путь ═══════════
print("== 8. FO v5.2.8 pin + Luxium Fabric + Sodium ==")

# Бинарные ассеты (Luxium*.jar, *.mrpack) в git НЕ коммитятся (см. .gitignore)
# — они лежат только в GitHub-релизе `fo-5.2.8-fixed` и/или в локальном кэше.
# Поэтому тест сам собирает stand-in реального dual-loader JAR с настоящим
# содержимым fabric.mod.json Luxium 2.8.4-pre-alpha (sodium 0.5.13,
# fabric-api >=0.92.2+1.20.1). Если настоящий файл всё же кладут рядом с
# wine.py вручную (офлайн-кэш), он имеет приоритет над стендом.
REAL_LUX = ROOT / "Luxium Let there be light-2.8.4-pre-alpha forge-fabric.jar"
LUX_STANDIN = LUX_HOME / REAL_LUX.name
if not REAL_LUX.exists():
    FABRIC_META_284 = {
        "id": "luxium", "version": "2.8.4-pre-alpha",
        "entrypoints": {"main": ["ru.gamemods.luxium.FabricMod"]},
        "depends": {"fabricloader": ">=0.15.0", "minecraft": "1.20.1",
                    "java": ">=17", "sodium": "0.5.13",
                    "fabric-api": ">=0.92.2+1.20.1"},
    }
    with zipfile.ZipFile(LUX_STANDIN, "w") as z:
        z.writestr("fabric.mod.json", json.dumps(FABRIC_META_284))
        z.writestr("META-INF/mods.toml",
                   'modLoader="javafml"\n[[mods]]\nmodId="luxium"\n'
                   '[[dependencies.luxium]]\nmodId="embeddium"\n'
                   'mandatory=true\nversionRange="[0.3.31,0.4)"\n')
        z.writestr("dummy.bin", b"x" * (6 * 1024 * 1024))
else:
    shutil.copy2(REAL_LUX, LUX_STANDIN)
# cfg читает REPO_ROOT/WINE_DIR напрямую (не через mc-алиасы) — наводим их
# на стенд; в LUX_HOME других *uxi*.jar нет, так что детерминированно.
_cfg_orig_repo, _cfg_orig_wine = cfg.REPO_ROOT, cfg.WINE_DIR
cfg.REPO_ROOT = LUX_HOME
cfg.WINE_DIR = LUX_HOME
try:
    check("dual-loader JAR распознан как Fabric-совместимый",
          cfg.luxium_supports_fabric() is True)
    check("fabric.mod.json: зависимость sodium = 0.5.13",
          cfg.luxium_fabric_dep("sodium") == "0.5.13")
    check("fabric.mod.json: зависимость fabric-api = >=0.92.2+1.20.1",
          cfg.luxium_fabric_dep("fabric-api") == ">=0.92.2+1.20.1")
finally:
    cfg.REPO_ROOT = _cfg_orig_repo
    cfg.WINE_DIR = _cfg_orig_wine

# forge-only JAR (как старый Luxia-Shaders) -> luxium_supports_fabric False
fo_only = TMP / "luxhome2"
fo_only.mkdir()
with zipfile.ZipFile(fo_only / "Luxium-forge-only.jar", "w") as z:
    z.writestr("META-INF/mods.toml", 'modId="luxium"\n')
    z.writestr("dummy.bin", b"x" * (6 * 1024 * 1024))
orig_repo = cfg.REPO_ROOT
orig_wine = cfg.WINE_DIR
cfg.REPO_ROOT = fo_only
cfg.WINE_DIR = fo_only
try:
    check("forge-only JAR (без fabric.mod.json) -> supports_fabric False",
          cfg.luxium_supports_fabric() is False)

    # ═══ починенный FO: приоритет над официальным v5.2.8 ═══
    def _raise2(*a, **k):
        raise RuntimeError("network disabled in test")
    orig_github2 = mc._github_get_json
    mc._github_get_json = _raise2
    try:
        # локального mrpack нет, API недоступен -> URL нашего GitHub-релиза
        fx = mc._fo_fixed_release()
        check("_fo_fixed_release без сети строит URL починенного релиза "
              "(Godlike-Bagheera/wine-installer, тег fo-5.2.8-fixed)",
              fx is not None and fx["tag"] == "fo-5.2.8-fixed"
              and fx["mrpack_url"].startswith(
                  "https://github.com/Godlike-Bagheera/wine-installer/"
                  "releases/download/fo-5.2.8-fixed/")
              and fx["mrpack_name"].endswith(".mrpack"))
        rel_fx = mc._pick_fo_release()
        check("_pick_fo_release приоритетно отдаёт починенный FO, "
              "а не официальный v5.2.8",
              rel_fx["tag"] == "fo-5.2.8-fixed")
        # WI_FO_VERSION=v5.2.8 -> официальный пин по-прежнему доступен напрямую
        mc.FO_1201_PIN_TAG = "v5.2.8"
        try:
            pin = mc._fo_pinned_release()
            check("официальный пин v5.2.8 без сети отдаёт URL Fabulously-Optimized",
                  pin is not None and pin["version"] == "5.2.8"
                  and "Fabulously-Optimized/fabulously-optimized" in pin["mrpack_url"])
        finally:
            mc.FO_1201_PIN_TAG = "fo-5.2.8-fixed"
    finally:
        mc._github_get_json = orig_github2

    # локальный .mrpack в корне репо -> полный офлайн, сеть не нужна
    mp = fo_only / "Fabulously.Optimized-5.2.8-fixed.mrpack"
    with zipfile.ZipFile(mp, "w") as z:
        z.writestr("modrinth.index.json", json.dumps({
            "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.16.10"},
            "files": []}))
    check("find_local_mrpack находит *.mrpack в корне репо",
          cfg.find_local_mrpack() == mp)
    with mock.patch.object(mc, "_github_get_json", _raise2):
        loc = mc._fo_fixed_release()
        # Новая схема: локальный .mrpack — офлайн-переопределение, но
        # mrpack_url (наш GitHub-релиз через gh-прокси) остаётся запасным
        # путём на случай битого локального файла.
        check("_fo_fixed_release: локальный .mrpack приоритетнее интернета, "
              "URL релиза — фолбек",
              loc is not None and loc.get("local_mrpack") == str(mp)
              and loc["mrpack_url"].startswith(
                  "https://github.com/Godlike-Bagheera/wine-installer/"
                  "releases/download/fo-5.2.8-fixed/")
              and loc["mc_versions"] == ["1.20.1"])
        rel_loc = mc._pick_fo_release()
        check("_pick_fo_release возвращает локальный починенный mrpack",
              rel_loc.get("local_mrpack") == str(mp))
    # битый zip с расширением .mrpack игнорируется
    bad = fo_only / "broken.mrpack"
    bad.write_bytes(b"not a zip at all, but big enough to pass size gate" * 50)
    os.utime(bad, (time.time() + 10, time.time() + 10))
    check("find_local_mrpack пропускает битые (не zip) файлы",
          cfg.find_local_mrpack() == mp)
finally:
    cfg.REPO_ROOT = orig_repo
    cfg.WINE_DIR = orig_wine

# пин FO v5.2.8: API недоступен -> фолбек на известные URL релиза
def _raise(*a, **k):
    raise RuntimeError("network disabled in test")
orig_github = mc._github_get_json
mc._github_get_json = _raise
try:
    # локального .mrpack нет (REPO_ROOT — репозиторий без фикс-архива) ->
    # фолбек строит URL нашего починенного релиза из шаблона
    fx2 = mc._fo_fixed_release()
    check("_fo_fixed_release фолбек по умолчанию (без сети и локального файла)",
          fx2 is not None and fx2["tag"] == "fo-5.2.8-fixed"
          and fx2["mrpack_url"].startswith(
              "https://github.com/Godlike-Bagheera/wine-installer/releases/"
              "download/fo-5.2.8-fixed/")
          and fx2["mrpack_name"].endswith(".mrpack"))
    # официальный пин проверяем с его тегом (WI_FO_VERSION=v5.2.8)
    mc.FO_1201_PIN_TAG = "v5.2.8"
    try:
        pinned = mc._fo_pinned_release()
        check("FO пин v5.2.8 отдаётся даже без GitHub API",
              pinned is not None and pinned["tag"] == "v5.2.8"
              and pinned["mrpack_url"].endswith("Fabulously.Optimized-5.2.8.mrpack")
              and pinned["zip_url"].endswith("Fabulously.Optimized-5.2.8.zip"))
        rel = mc._pick_fo_release()
        check("_pick_fo_release возвращает пин 5.2.8 (не 14.x/15.x), "
              "когда WI_FO_VERSION=v5.2.8",
              rel["version"] == "5.2.8")
    finally:
        mc.FO_1201_PIN_TAG = "fo-5.2.8-fixed"
finally:
    mc._github_get_json = orig_github

# install_luxium_fabric_mod: офлайн-копирование dual-JAR в mods/
# (стенд LUX_STANDIN из LUX_HOME уже создан выше — бинарный JAR в git не лежит)
game_f = TMP / "game_fo"
(game_f / "mods").mkdir(parents=True)
with mock.patch.object(mc, "REPO_ROOT", LUX_HOME), \
     mock.patch.object(mc, "WINE_DIR", LUX_HOME):
    ok_f = mc.install_luxium_fabric_mod(game_f)
    placed = [f.name for f in (game_f / "mods").glob("*.jar")]
    check("Luxium (Fabric) скопирован из локального JAR в mods/",
          ok_f and any("uxi" in n.lower() for n in placed))
    # повторная установка = no-op
    with mock.patch.object(mc, "_luxium_local_jars", lambda: []):
        check("повторная установка Luxium (Fabric) = no-op",
              mc.install_luxium_fabric_mod(game_f))

# forge-only -> отказ + подсказка про `ofps`. GitHub-сеть мокана: иначе
# фолбек реально скачал бы dual-JAR из нашего релиза (что корректно, но это
# уже не офлайн-тест) и положил его в mods/.
game_f2 = TMP / "game_fo2"
game_f2.mkdir()
cfg.REPO_ROOT = fo_only
cfg.WINE_DIR = fo_only
try:
    with mock.patch.object(mc, "REPO_ROOT", fo_only), \
         mock.patch.object(mc, "WINE_DIR", fo_only), \
         mock.patch.object(mc, "_github_get_json", _raise), \
         mock.patch.object(mc, "download_file", lambda *a, **k: False):
        check("forge-only Luxium не кладётся в Fabric-модпак",
              mc.install_luxium_fabric_mod(game_f2) is False
              and not list((game_f2 / "mods").glob("*.jar")))
finally:
    cfg.REPO_ROOT = orig_repo
    cfg.WINE_DIR = orig_wine

# Sodium: уже в mods/ -> no-op; иначе — Modrinth API (mock) с точной версией 0.5.13
game_s = TMP / "game_sod"
(game_s / "mods").mkdir(parents=True)
(game_s / "mods" / "sodium-fabric-0.5.13+mc1.20.1.jar").write_bytes(b"z")
with mock.patch.object(mc, "REPO_ROOT", fo_only), \
     mock.patch.object(mc, "WINE_DIR", fo_only):
    check("Sodium уже установлен -> no-op", mc.install_sodium_for_luxium(game_s))
(game_s / "mods" / "sodium-fabric-0.5.13+mc1.20.1.jar").unlink()

SODUM_JSON = [{"id": "OihdIimA", "version_number": "mc1.20.1-0.5.13-fabric",
               "status": "release", "loaders": ["fabric"],
               "game_versions": ["1.20.1"],
               "files": [{"filename": "sodium-fabric-0.5.13+mc1.20.1.jar",
                          "url": "https://cdn.modrinth.com/data/AANobbMI/xx/sodium-fabric-0.5.13%2Bmc1.20.1.jar",
                          "hashes": {"sha1": ""}}]},
              {"id": "zzz", "version_number": "mc1.20.1-0.5.11",
               "status": "release", "loaders": ["fabric"],
               "game_versions": ["1.20.1"],
               "files": [{"filename": "sodium-fabric-0.5.11+mc1.20.1.jar",
                          "url": "https://cdn.modrinth.com/data/AANobbMI/yy/sodium.jar",
                          "hashes": {"sha1": ""}}]}]

def fake_dl_sodium(urls, dest, name, **kw):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w") as z:
        z.writestr("fabric.mod.json", '{"id":"sodium"}')
    return True

with mock.patch.object(mc, "_http_get_json", lambda url, **k: SODUM_JSON), \
     mock.patch.object(mc, "download_file", fake_dl_sodium), \
     mock.patch.object(mc, "REPO_ROOT", fo_only), \
     mock.patch.object(mc, "WINE_DIR", fo_only):
    ok_s = mc.install_sodium_for_luxium(game_s)
    check("Sodium: взята точная версия 0.5.13 из depends Luxium (не 0.5.11)",
          ok_s and (game_s / "mods" / "sodium-fabric-0.5.13+mc1.20.1.jar").exists())

# без сети и локального файла -> warn, но False (в FO это не блокирует установку)
with mock.patch.object(mc, "_http_get_json", _raise), \
     mock.patch.object(mc, "download_file", lambda *a, **k: False), \
     mock.patch.object(mc, "REPO_ROOT", fo_only), \
     mock.patch.object(mc, "WINE_DIR", fo_only):
    game_s2 = TMP / "game_sod2"
    game_s2.mkdir()
    check("Sodium: без файла и сети -> False (предупреждение, не краш)",
          mc.install_sodium_for_luxium(game_s2) is False)


print()
if fails:
    print(f"ПРОВАЛОВ: {len(fails)} -> {fails}")
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
shutil.rmtree(TMP, ignore_errors=True)
