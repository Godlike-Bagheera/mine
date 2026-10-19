#!/usr/bin/env python3
"""Проверки фиксов saveworlds/loadworlds (modules/worlds.py, modules/prefix.py).

Воспроизводит 5 дефектов разбора:
1. Коллизия имён миров из разных инстантов/снимков -> молчаливая потеря копий;
2. Битая защита от вложенности (сравнение с ключами-именами вместо путей)
   -> фантомные миры из datapack-областей (DIM-1/DIM1);
3. rglob('*') по всему дереву + неисчищаемые saves.bak-* -> медленный обход
   и недетерминированный выбор копии;
4. Целевая папка = saves[0] без выбора пользователя при нескольких лаунчерах;
5. find_minecraft_dirs принимает бэкап minecraft-worlds/.minecraft за игру
   (когда «Диск D» лежит внутри HOME).
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# изолируем fake-HOME ДО импорта config (он резолвит HOME при импорте)
FAKE_HOME = Path(tempfile.mkdtemp(prefix="fakehome_worlds_"))
os.environ["HOME"] = str(FAKE_HOME)
# WI_FAKE_HOME=1 запрещает config._resolve_home фолбэк на passwd-дом (/root):
# иначе под root недоступный fake-HOME молча подменяется реальным /root.
os.environ["WI_FAKE_HOME"] = "1"

from modules import worlds, prefix  # noqa: E402
from modules.config import HOME     # noqa: E402

fails = []


def check(name, cond):
    print(f"  {'OK ' if cond else 'FAIL'} {name}")
    if not cond:
        fails.append(name)


def mkworld(base, name, marker="level.dat", nested_dim=False):
    """Создаёт мир-заглушку; nested_dim —datapack-область World/DIM-1/DIM1."""
    w = base / name
    (w / "region").mkdir(parents=True, exist_ok=True)
    (w / marker).write_bytes(b"\x0a\xff")
    (w / "region" / "r.0.0.mca").write_bytes(b"chunk-" + name.encode())
    if nested_dim:
        dim = w / "DIM-1" / "DIM1"          # вложенная область со своим level.dat
        dim.mkdir(parents=True, exist_ok=True)
        (dim / "level.dat").write_bytes(b"\x0a\xff")
        (dim / "region").mkdir(exist_ok=True)
    return w


tmp = Path(tempfile.mkdtemp(prefix="worlds_test_"))

# ═══════════════ Каркас: Диск D внутри HOME (типичный «на рабочем столе») ═════
disk = FAKE_HOME / "Рабочий стол" / "Диск D"
backup = disk / "minecraft-worlds"

# Инстант .minecraft: актуальный saves + старый снимок saves.bak-*
mc1 = backup / ".minecraft"
mkworld(mc1 / "saves.bak-20240101-000000", "New World")
(backup / ".minecraft" / "saves.bak-20240101-000000" / "New World" / "region" / "r.0.0.mca").write_bytes(b"OLD-STALE")
mkworld(mc1 / "saves", "New World")
mkworld(mc1 / "saves", "Second")
# Prism: свой мир с тем же именем «New World» (дефолтное имя при создании!)
prism = backup / "Prism_Minecraft"
mkworld(prism / "saves", "New World")
# Wine-префикс: worlds/ + datapack-область внутри мира World
wine = backup / "wine_prefix"
mkworld(wine / "worlds", "WineWorld")
mkworld(wine / "worlds", "World", nested_dim=True)

# гарантируем детерминированный порядок «свежести»: .bak старше актуального saves
import subprocess  # noqa: E402
subprocess.run(["touch", "-m", "-d", "2024-01-01 00:00:00",
                str(mc1 / "saves.bak-20240101-000000")], check=False)
subprocess.run(["touch", "-m", "-d", "2025-06-01 00:00:00", str(mc1 / "saves"),
                str(prism / "saves"), str(wine / "worlds")], check=False)

# ═══════════════ 1+2+3. Сбор миров из структуры бэкапа ═══════════════════════
found = worlds._collect_backup_worlds(backup)
names = sorted(found.values())
print("  найдено:", names)

check("миры собраны (не пусто)", len(found) >= 6)
# Problem 1: ВСЕ копии «New World» сохраняются — проигравшие переименовываются
nws = [n for n in names if n.startswith("New World")]
check("нет молчаливой потери копий: все 3 'New World' выгружаются", len(nws) == 3)
check("копии переименованы с меткой инстанта/снимка",
      sum(1 for n in nws if n != "New World" and "[" in n) == 2)
#Problem 2: DIM-область не стала отдельным миром «World»/«DIM1»
check("вложенная datapack-область не стала фантомным миром",
      "DIM1" not in names and "Nether" not in names)
check("мир World принят целиком", "World" in names)
# Problem 3: целевой обход видит и старые снимки saves.bak-*, ничего не теряя
old_copy = [p for p, n in found.items() if "saves.bak-20240101-000000" in str(p)]
check("старый снимок saves.bak-* учтён как отдельная копия", len(old_copy) == 1)
fresh = [p for p, n in found.items() if str(p).endswith("saves/New World")
         and "saves.bak" not in str(p)]
check("актуальная 'New World' сохраняет исходное имя (самый свежий снимок)",
      found.get(fresh[0]) == "New World" if fresh else False)
# имена уникальны (нет перезаписей при выгрузке)
check("все имена выгрузки уникальны", len(set(names)) == len(names))

#rglob-обхода больше нет в модуле (ни в load, ни в menu)
src = (ROOT / "modules" / "worlds.py").read_text(encoding="utf-8")
check("rglob('*') по дереву бэкапа устранён из worlds.py", 'rglob("*")' not in src)

# ═══════════════ 5. Бэкап внутри HOME не матчится как .minecraft ═════════════
# Реальная игра в fake-HOME
real_mc = HOME / ".minecraft"
(real_mc / "versions" / "1.20.1").mkdir(parents=True)
(real_mc / "versions" / "1.20.1" / "1.20.1.json").write_text("{}")
mkworld(real_mc / "saves", "RealWorld")

# Бэкап-копия игры внутри minecraft-worlds (saveworlds копирует структуру с versions/)
fake_in_backup = backup / ".minecraft" / "copy_of_game"
(fake_in_backup / "versions" / "1.20.1").mkdir(parents=True)
mkworld(fake_in_backup / "saves", "BackupGhost")
# и ещё вариант: сам инстант-каталог бэкапа выглядит как игра (имя .minecraft + versions)
(backup / ".minecraft" / "versions" / "1.20.1").mkdir(parents=True)

dirs = prefix.find_minecraft_dirs(limit=12)
in_backup_found = [d for d in dirs if prefix._in_worlds_backup(d)]
check("find_minecraft_dirs не возвращает пути из-под minecraft-worlds",
      not in_backup_found)
check("реальная .minecraft всё равно найдена",
      any(str(d).startswith(str(FAKE_HOME)) and (d / "saves").is_dir() for d in dirs))

saves = worlds.get_saves_dirs()
check("get_saves_dirs не берёт saves из бэкапа",
      all(not prefix._in_worlds_backup(s) for s, _ in saves))
check("get_saves_dirs видит реальные миры",
      any((s / "RealWorld").is_dir() for s, _ in saves))

# ═══════════════ 4. Выбор целевой папки при нескольких лаунчерах ═════════════
# вторая «игра» на глубине, которую реально находит find_minecraft_dirs (HOME/*/.minecraft)
second_mc = HOME / "Prism" / ".minecraft"
(second_mc / "versions").mkdir(parents=True)
mkworld(second_mc / "saves", "PrismOnly")
saves2 = worlds.get_saves_dirs()
check("тест окружения: найдено >=2 папок saves", len(saves2) >= 2)

# авто-поведение (Enter) = первый вариант — обратная совместимость
import builtins  # noqa: E402
_orig_input = builtins.input
try:
    builtins.input = lambda prompt="": ""
    t_auto = worlds.choose_saves_target(saves2)
    check("пустой ввод -> первый вариант (совместимость)", t_auto == saves2[0][0])
    builtins.input = lambda prompt="": "2"
    t_pick = worlds.choose_saves_target(saves2)
    check("выбор '2' -> вторая папка, а не always saves[0]", t_pick == saves2[1][0])
    builtins.input = lambda prompt="": "99"
    t_bad = worlds.choose_saves_target(saves2)
    check("некорректный номер -> откат к первому варианту", t_bad == saves2[0][0])
finally:
    builtins.input = _orig_input

# одиночная папка — без вопроса
t_single = None
try:
    builtins.input = lambda prompt="": (_ for _ in ()).throw(AssertionError("вопрос не задавался бы"))
    t_single = worlds.choose_saves_target([saves2[0]])
except AssertionError:
    pass
finally:
    builtins.input = _orig_input
check("одна папка saves -> берётся без спроса", t_single == saves2[0][0])

# ═══════════════ Сквозной прогон cmd_load_worlds (e2e) ═══════════════════════
# ВАЖНО: fake-HOME в скриптах — это mkdtemp с префиксом «fakehome_»
# (см. строку FAKE_HOME выше). conftest подставляет каждому скрипту СВОЮ
# temp-папку с тем же префиксом; если HOME вдруг указывает НЕ на такую
# папку (например, pytest-хук сбора не отработал), прогон тронул бы
# реальную ~/.minecraft пользователя — поэтому e2e пропускается с кодом 0.
def _is_fake_home_ok():
    import pwd
    try:
        env = os.environ.get("HOME", "")
        real = str(Path(pwd.getpwuid(os.getuid()).pw_dir))
        if env and env != real:
            return Path(env).name.startswith("fakehome_")
        return not Path(real / ".minecraft").exists()   # чистой системе — можно
    except Exception:
        return False


if not _is_fake_home_ok():
    print(f"[!] HOME={os.environ.get('HOME')} — не изолированная temp-папка "
          "(fakehome_*); e2e пропущен, чтобы не трогать реальный ~/.minecraft.")
    if fails:
        print(f"ПРОВАЛОВ: {len(fails)} -> {fails}")
    sys.exit(1 if fails else 0)

try:
    def _e2e_input(prompt=""):
        # Enter = принять найденный Диск D и вариант 1; ручной запрос пути к
        # Диску D возвращаем прямо на подготовленный бэкап (в conda-подобных
        # системах HOME может резолвиться в symlink /tmp -> /var/tmp, и тогда
        # find_disk_d не находит диск по нерезолвленному пути).
        return str(disk) if "Укажи путь" in prompt else ""
    builtins.input = _e2e_input
    worlds.cmd_load_worlds()
finally:
    builtins.input = _orig_input

target = saves2[0][0]
loaded_names = {p.name for p in target.iterdir() if p.is_dir()}
print("  в игре после загрузки:", sorted(loaded_names))
check("e2e: актуальная 'New World' загружена под своим именем", "New World" in loaded_names)
check("e2e: вторая копия 'New World' НЕ потеряна (есть с суффиксом)",
      any(n.startswith("New World [") for n in loaded_names))
check("e2e: старый снапшот НЕ перекрывает актуальный мир",
      (target / "New World" / "region" / "r.0.0.mca").read_bytes() != b"OLD-STALE")
check("e2e: WineWorld и World восстановлены", {"WineWorld", "World"} <= loaded_names)
check("e2e: фантомного DIM1-мира нет", "DIM1" not in loaded_names)
check("e2e: у восстановленного World цела вложенная область",
      (target / "World" / "DIM-1" / "DIM1" / "level.dat").exists())

print()
if fails:
    print(f"ПРОВАЛОВ: {len(fails)} -> {fails}")
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ МИРОВ ПРОЙДЕНЫ")
