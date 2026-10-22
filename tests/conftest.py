"""Инфраструктура: тесты можно запускать И как скрипты (python tests/test_x.py,
так делает CI), И через pytest (pytest / pytest tests/).

Как это работает:
* Каждый test_*.py — standalone-скрипт: код верхнего уровня печатает
  результаты проверок и вызывает sys.exit(1) при провале. Обычный сбор
  pytest таких файлов ломается ещё на стадии collection: модуль
  импортируется целиком, его side-effects и sys.exit срабатывают внутри
  collection. Поэтому хук pytest_pycollect_makemodule возвращает None для
  наших скриптов — стандартный Python-сборщик (Module/Package) их не
  импортирует ни при `pytest`, ни при `pytest tests/test_x.py`
  (initpath-аргументы тоже проходят через этот хук).
* Вместо этого здесь определён явный wrapper-тест test_standalone_scripts,
  который параметризует все файлы tests/test_*.py и выполняет каждый из них
  отдельным subprocess'ом (интерпретатором текущего pytest-процесса).
  Код возврата транслируется в успех/провал теста, вывод показывается при
  падении. Скрипты при этом НЕ импортируются в процесс pytest.
* Изоляция HOME: каждый standalone-скрипт получает СВОЮ свежую temp-папку
  (её путь приходит в subprocess через env HOME + WI_FAKE_HOME=1, см.
  test_standalone_scripts), а WI_FAKE_HOME=1 запрещает config._resolve_home()
  фолбэк на passwd-дом (/root): иначе под root при недоступном fake-HOME
  фолбек вернул бы реальный /root и тесты начали бы трогать ~/.minecraft
  настоящего пользователя (FileExistsError на /root/.minecraft).
  Раньше HOME подменялся один раз при импорте conftest — это ломало
  повторные прогоны: скрипты пишут миры в $HOME/.minecraft/saves, после
  первого прогона там остаётся «New World», и e2e-проверки loadworlds
  начинали падать («уже есть в игре — пропускаю»).
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"

# --- изоляция fake-HOME -----------------------------------------------------
# ВАЖНО: os.environ["HOME"] здесь НЕ подменяется глобально. Скрипты получают
# свою temp-папку через env конкретного subprocess (см. test_standalone_scripts):
# общий HOME на весь pytest-процесс делал повторные прогоны несопоставимыми —
# остатки ~/.minecraft/saves от первого прогона сбивали e2e-проверки loadworlds.
# Для прямых вызовов хуков сбора (pytest tests/test_x.py без wrapper'а) ставим
# безобидный несуществующий HOME: с WI_FAKE_HOME=1 config не делает фолбэк на
# passwd-дом (/root), а модуль-скрипт в процессе pytest всё равно не импортируется.
os.environ.setdefault("WI_FAKE_HOME", "1")
if not os.environ.get("HOME", "").startswith("/tmp/fakehome"):
    os.environ["HOME"] = "/tmp/fakehome_pytest_unused"


def _discover_scripts():
    """Все standalone-скрипты tests/test_*.py (список не хардкодится)."""
    return sorted(glob.glob(str(TESTS_DIR / "test_*.py")))


# Совместимость с pytest 9.x: там hookwrapper'ы deprecated и удалены, а
# стандартный Python-сборщик может импортировать test_*.py раньше нашего
# collect_file-хука. Импорт standalone-скрипта = выполнение кода верхнего
# уровня в чужом HOME (ошибки collection). Единственный надёжный перехват
# ДО импорта — pytest_collect_file ниже: он вызывается для каждого файла до
# создания Module-коллектора, и возвращённый _NoCollect полностью заменяет
# обычный сбор (pytest_pycollect_makemodule не определяем сознательно — см.
# комментарий выше). Реальный запуск скриптов делает wrapper
# test_standalone_scripts отдельными subprocess'ами со свежим fake-HOME.


def pytest_collect_file(file_path, parent):
    """Отключает сбор standalone-скриптов стандартным Python-сборщиком.

    ВАЖНО про порядок хуков (почему нельзя полагаться только на свой хук):
    pytest_collect_file вызывается каскадом всех плагинов; результат
    плагина-коллектора складывается в список, и если ХОТЬ ОДИН плагин
    вернул непустой результат, все остальные (включая conftest) НЕ
    вызываются. Стандартный python-хук возвращает Module для файлов,
    совпадающих с python_files (test_*.py), поэтому наш хук для них бы
    просто не запускался — Module импортировал standalone-скрипт прямо в
    процесс pytest (side-effects, sys.exit, чужой HOME -> INTERNALERROR).
    Лечится ini-настройкой python_files = conftest.py (см. pyproject.toml):
    стандартный хук тогда возвращает None для наших скриптов, каскад
    доходит до нас, и мы отдаём _NoCollect.

    Возвращаем КОЛЛЕКТОР (_NoCollect) с нулём детей, а НЕ Item-заглушку:
    Collector раскрывается через collect() -> [], и сам файл НИКОГДА не
    импортируется. Работает и для `pytest`, и для `pytest tests/`, и для
    явного `pytest tests/test_x.py`. Реальный запуск скриптов делает
    wrapper test_standalone_scripts отдельными subprocess'ами со свежим
    fake-HOME.
    """
    path = Path(str(file_path))
    if path.parent == TESTS_DIR and path.name.startswith("test_") \
            and path.suffix == ".py":
        return _NoCollect.from_parent(parent, path=path, name=path.name)
    return None


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makemodule(module_path, parent):
    """Явный запуск `pytest tests/test_x.py` — не импорт скрипта, а wrapper-тест.

    Для initpath-файлов стандартный python-хук НЕ проверяет python_files и
    создал бы Module: импорт standalone-скрипта в процесс pytest = выполнение
    кода верхнего уровня (чужой HOME, sys.exit -> INTERNALERROR). Хук
    firstresult: без tryfirst победил бы встроенный плагин. Вместо Module
    возвращаем _ScriptWrapper — один тест, который гоняет ИМЕННО этот скрипт
    subprocess'ом со свежим fake-HOME (как test_standalone_scripts).
    """
    path = Path(str(module_path))
    if path.parent == TESTS_DIR and path.name.startswith("test_") \
            and path.suffix == ".py":
        return _ScriptWrapper.from_parent(parent, path=path, name=path.name)
    return None


class _NoCollect(pytest.Collector):
    """Collector-заглушка: собирает 0 детей, standalone-скрипт не импортируется."""

    def collect(self):
        return []


class _ScriptWrapper(pytest.Collector):
    """Коллектор одного файла: отдаёт единственный Item, запускающий скрипт."""

    def collect(self):
        return [_RunScriptItem.from_parent(self, name=self.path.name,
                                           script=str(self.path))]


def _run_script_subprocess(script_path):
    """Выполняет standalone-скрипт отдельным процессом со свежим fake-HOME.

    Возвращает (код возврата, вывод). Тот же механизм изоляции, что у
    test_standalone_scripts: свой temp-HOME на прогон, WI_FAKE_HOME=1
    запрещает фолбэк config на passwd-дом, папка удаляется после."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    fake_home = tempfile.mkdtemp(prefix="fakehome_pytest_")
    env["HOME"] = fake_home
    env["WI_FAKE_HOME"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, script_path],
            capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=300,
        )
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


class _RunScriptItem(pytest.Item):
    """Item: запуск скрипта subprocess'ом; код возврата -> успех/провал."""

    def __init__(self, *args, script, **kwargs):
        super().__init__(*args, **kwargs)
        self.script = script

    def runtest(self):
        code, output = _run_script_subprocess(self.script)
        if code != 0:
            raise ScriptFailed(self.script, code, output)

    def reportinfo(self):
        return self.path, None, f"standalone {Path(self.script).name}"


class ScriptFailed(Exception):
    """Провал standalone-скрипта; repr_truncated даёт читаемый репорт."""

    def __init__(self, script, code, output):
        self.script, self.code, self.output = script, code, output

    def __repr__(self):
        return f"{Path(self.script).name}: код возврата {self.code}"

    def _to_repr_failure(self):
        return self.__repr__() + "\n--- вывод скрипта ---\n" + self.output


@pytest.fixture(params=_discover_scripts(), ids=lambda p: Path(p).name)
def script_path(request):
    return request.param


def test_standalone_scripts(script_path):
    """Каждый tests/test_*.py выполняется как отдельный скрипт.

    Изоляция: каждому скрипту выдаётся СВОЯ свежая temp-папка в качестве
    HOME (фолбэка на /root нет благодаря WI_FAKE_HOME=1) — прогоны
    полностью независимы и повторяемы, остатки ~/.minecraft от предыдущих
    запусков не влияют на результат. Папка удаляется после прогона.
    """
    code, output = _run_script_subprocess(script_path)
    if code != 0:
        pytest.fail(f"{Path(script_path).name}: код возврата {code}\n"
                    f"--- вывод скрипта ---\n{output}")
