"""Все константы и пути. Никакой логики — только данные."""
import os
import re
import json
import zipfile
from pathlib import Path

# ВАЖНО: modules.debug импортирует из этого модуля DEBUG_LOG/LOG_DIR,
# поэтому верхнеуровневый `from modules import debug` здесь вызвал бы
# циклический импорт. Локальный импорт внутри except — намеренный.
# NOTE: keep this comment in sync if the import layout changes (CI marker v2).


# Прокси-зеркала GitHub (порядок = приоритет).
# Базовые значения; пользователь может переопределить список зеркал в
# settings.json без правки кода (см. gh_proxies()).
# Проверено живыми запросами (окт. 2026): ghfast.top / ghproxy.net /
# gh.llkk.cc отдают 200; gh-proxy.com отдаёт 403 на release-ассеты — оставлен
# в конце списка как резерв (пользователь может переопределить через
# settings.json "gh_proxies").
_GH_PROXIES = [
    ("ghfast.top",   "https://ghfast.top/"),
    ("ghproxy.net",  "https://ghproxy.net/"),
    ("gh.llkk.cc",   "https://gh.llkk.cc/"),
    ("gh-proxy.com", "https://gh-proxy.com/"),
]

# Файл настроек известен здесь заранее (config не импортирует settings.py,
# но умеет читать из него безопасные user-overrides). Реальный путь —
# WINE_DIR/settings.json (см. ниже); на время чтения overrides используется
# угаданный HOME, а после определения HOME путь пересчитывается.
from modules.platform_utils import portable_root_dirname as _prd  # noqa: E402
SETTINGS_FILE = Path.home() / _prd() / "settings.json"


def _user_settings():
    """Кэш settings.json для user-overrides (обновляется при save_settings).

    Назначение: ротация прокси-ключей без правки кода — достаточно поменять
    "gh_proxies" или "log_keep_days" в ~/wine-portable/settings.json.
    Любая ошибка файла — тихий откат к дефолтам.
    """
    if not getattr(_user_settings, "_loaded", False):
        data = {}
        try:
            if SETTINGS_FILE.exists():
                raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
        except Exception:
            data = {}
        _user_settings._data = data
        _user_settings._loaded = True
    return _user_settings._data


def reload_user_settings():
    """Сброс кэша overrides (вызывается из settings.save_settings)."""
    _user_settings._loaded = False


def gh_proxies():
    """Список GitHub-прокси с учётом overrides из settings.json.

    Формат override: ["https://proxy1/", ...] или
    [["имя", "https://proxy1/"], ...]. Пустой/битый список -> дефолт.
    """
    raw = _user_settings().get("gh_proxies")
    if not isinstance(raw, list) or not raw:
        return list(_GH_PROXIES)
    out = []
    for item in raw:
        if isinstance(item, str) and item.startswith("http"):
            m = re.match(r"https?://([^/]+)", item)
            out.append((m.group(1), item if item.endswith("/") else item + "/"))
        elif (isinstance(item, (list, tuple)) and len(item) == 2
              and all(isinstance(x, str) for x in item)):
            out.append((item[0], item[1]))
    return out or list(_GH_PROXIES)


# ---------- ПУТИ ----------


def _resolve_home():
    """Определяет домашнюю папку надёжным способом (Windows 10 / Linux).

    Path.home() берёт переменную окружения (HOME / USERPROFILE), которая
    может указывать на несуществующий путь. Сначала пробуем её, затем
    passwd-запись пользователя (только POSIX) — в ход идёт только реально
    существующая директория.

    Исключение: WI_FAKE_HOME=1 (используется тестами). Тогда HOME берётся
    строго из переменной окружения, без фолбэка на passwd-запись: иначе под
    root при битом/несуществующем HOME фолбек вернул бы реальный /root и
    тесты начали бы трогать ~/.minecraft настоящего пользователя.
    """
    # локальный импорт — модуль platform_utils не импортирует config,
    # циклов нет
    from modules.platform_utils import IS_WINDOWS, user_home
    if IS_WINDOWS:
        # на Windows USERPROFILE почти всегда корректен; фолбэк не нужен
        return user_home()
    env_home = Path.home()
    if os.environ.get("WI_FAKE_HOME") == "1":
        return env_home
    if env_home.is_dir():
        return env_home
    try:
        import pwd
        pw_dir = Path(pwd.getpwuid(os.getuid()).pw_dir)
        if pw_dir.is_dir():
            return pw_dir
    except Exception as e:
        # локальный импорт — иначе циклический импорт config <-> debug
        from modules import debug
        debug.dbg_exc(e, "config")
    return env_home


HOME = _resolve_home()
# Корень репозитория (папка, куда попадает проект через `git clone`; рядом
# лежат wine.py и, например, Luxium*.jar — локальный запас мода без интернета).
REPO_ROOT = Path(__file__).resolve().parent.parent
from modules.platform_utils import portable_root_dirname  # noqa: E402
# На Windows 10 корневая папка называется mc-portable (Wine там нет);
# на РЕД ОС исторически оставлено wine-portable. WINE_BIN/DXVK_DIR ниже
# на Windows не используются — это заглушки для старой кодовой базы.
WINE_DIR = HOME / portable_root_dirname()
LOG_DIR = WINE_DIR / "logs"
DEBUG_LOG = LOG_DIR / "debug.log"

BIN_DIR = WINE_DIR / "bin"
GAMES_DIR = HOME / "games"
PRISM_DIR = HOME / "prism"
JAVA_DIR = HOME / "java"
SHORTCUTS_DIR = WINE_DIR / "shortcuts"
PREFIXES_DIR = WINE_DIR / "prefixes"
DEFAULT_PREFIX = WINE_DIR / "prefix"
WINE_PREFIX = DEFAULT_PREFIX              # backward-compat алиас

HISTORY_FILE = WINE_DIR / "history.json"
CACHE_FILE = WINE_DIR / ".mirror_cache"
# SETTINGS_FILE уже был объявлен в начале файла (для user-overrides) —
# пересчитываем на надёжный HOME и сбрасываем кэш, чтобы overrides
# перечитались с правильного пути.
SETTINGS_FILE = WINE_DIR / "settings.json"
reload_user_settings()

WINE_BIN = WINE_DIR / "wine.AppImage"
RUNEXE = WINE_DIR / "runexe"
DXVK_DIR = WINE_DIR / "dxvk"
WINETRICKS_BIN = BIN_DIR / "winetricks"
ARIA2C_BIN = BIN_DIR / "aria2c"

DESKTOP_DIRS = [HOME / "Рабочий стол", HOME / "Desktop"]

# Приоритетные папки для поиска .exe.
PRIORITY_DIRS = [
    HOME / "Загрузки", HOME / "Downloads",
    HOME / "Рабочий стол", HOME / "Desktop",
    HOME / "games", HOME / "Игры",
    WINE_PREFIX / "drive_c",
]

EXCLUDE_DIRS = {
    ".wine", ".wine-appimage-stable", "wine-portable", ".cache", ".local", ".config",
    ".mozilla", ".thunderbird", ".steam", ".var", ".npm",
    ".cargo", ".rustup", ".dotnet", ".gradle", ".m2",
    "node_modules", ".git", "__pycache__", "venv", ".venv",
    "snap", "flatpak", "Trash", ".Trash", ".Trash-1000",
    ".thumbnails", "target", "build", "dist",
}

SUPPORTED_EXT = (".exe", ".lnk", ".msi")

# ---------- НАСТРОЙКИ ЗАГРУЗКИ / ПОВЕДЕНИЯ (runtime-флаги) ----------
# Дефолты; реальные значения берёт settings.apply_settings() из
# settings.json (DEFAULT_SETTINGS ниже), а носителями в рантайме служат
# флаги modules/state.py. Константы здесь — только стартовые значения.
MIN_SPEED_KB = 30
SPEED_TEST_SECONDS = 10
CONNECT_TIMEOUT = 60
SPEED_CHECK_MIN_MB = 50
SLOW_MODE_AFTER = 2

AUTO_FIX = True
MAX_RETRIES = 3
OK_RUN_SECONDS = 60
CRASH_SHOW_LOG_SECONDS = 25

MIN_WINETRICKS_SIZE = 100 * 1024
MIN_ARIA2C_SIZE = 500 * 1024

DXVK_OVERRIDES = "d3d11,dxgi,d3d9,d3d10core=n,b"

USE_PER_GAME_PREFIX = False
USE_DXVK_HUD = False
USE_MANGOHUD = False
USE_GAMESCOPE = False
USE_TTS_NOTIFY = False
LOG_KEEP_DAYS = 30

CURRENT_VERSION = "2.6"

# Все пользовательские опции — отсюда; меню `settings` и state.* их читают.
DEFAULT_SETTINGS = {
    "quiet_mode": False,
    "use_gamemode": False,
    "debug_mode": False,
    "use_dxvk_hud": False,
    "use_mangohud": False,
    "use_gamescope": False,
    "use_tts_notify": False,
    "use_per_game_prefix": False,
    "log_keep_days": 30,
}

# ---------- КОМАНДЫ ----------
EXPECTED_COMMANDS = [
    "help", "exit", "minecraft", "download", "fonts", "steamfix",
    "gamemode", "history", "log", "quiet", "reset", "dxvk",
    "desktop", "vulkan", "settings", "bin", "verify", "update",
    "debug", "debugreport", "install-java",
    "fo", "ofps", "optifine", "prism", "legacy",
    "export", "gpu-temp", "freegames",
    "worlds", "saveworlds", "loadworlds",
    "gamestatus", "stopgame", "waitgame", "games", "shaders",
    "fo-autofix",
]

# ---------- SYSTEM TRUSTSTORE ДЛЯ JAVA ----------
# Java использует свой cacerts в $JAVA_HOME/lib/security/, и русские
# корневые сертификаты (Минцифры) туда не попадают. Системные CA лежат
# в /etc/pki/... (RHEL-семейство) или /etc/ssl/... (Debian-семейство).
# Перебираем по порядку — берём первый существующий и достаточно большой.
SYSTEM_TRUSTSTORE_PATHS = [
    "/etc/pki/ca-trust/extracted/java/cacerts",   # Red OS / RHEL / Fedora
    "/etc/ssl/certs/java/cacerts",                # Debian / Ubuntu / Arch
    "/var/lib/ca-certificates/java-cacerts",      # openSUSE
    "/usr/lib/jvm/java-17-openjdk/lib/security/cacerts",
    "/usr/lib/jvm/java-11-openjdk/lib/security/cacerts",
    "/usr/lib/jvm/java-8-openjdk/lib/security/cacerts",
]

# Пароль системного cacerts от update-ca-trust
SYSTEM_TRUSTSTORE_PASSWORD = "changeit"

# ---------- ЗЕРКАЛА ----------
WINE_MIRRORS = [
    ("ghfast.top",       "https://ghfast.top/https://github.com/mmtrt/WINE_AppImage/releases/download/continuous-stable/wine-stable_11.0-x86_64.AppImage"),
    ("ghproxy.net",      "https://ghproxy.net/https://github.com/mmtrt/WINE_AppImage/releases/download/continuous-stable/wine-stable_11.0-x86_64.AppImage"),
    ("gh-proxy.com",     "https://gh-proxy.com/https://github.com/mmtrt/WINE_AppImage/releases/download/continuous-stable/wine-stable_11.0-x86_64.AppImage"),
    ("ghproxy.cc",       "https://ghproxy.cc/https://github.com/mmtrt/WINE_AppImage/releases/download/continuous-stable/wine-stable_11.0-x86_64.AppImage"),
    ("gh.llkk.cc",       "https://gh.llkk.cc/https://github.com/mmtrt/WINE_AppImage/releases/download/continuous-stable/wine-stable_11.0-x86_64.AppImage"),
    ("GitHub (прямой)",  "https://github.com/mmtrt/WINE_AppImage/releases/download/continuous-stable/wine-stable_11.0-x86_64.AppImage"),
]

DXVK_VERSIONS = ["v2.4.1", "v2.3.1", "v2.4", "v2.3"]

WINETRICKS_MIRRORS = [
    ("ghfast.top",                "https://ghfast.top/https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks"),
    ("ghproxy.net",               "https://ghproxy.net/https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks"),
    ("gh-proxy.com",              "https://gh-proxy.com/https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks"),
    ("gh.llkk.cc",                "https://gh.llkk.cc/https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks"),
    ("raw.githubusercontent.com", "https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks"),
]

ARIA2C_MIRRORS = [
    ("ghfast.top",       "https://ghfast.top/https://github.com/P3TERX/aria2-builder/releases/download/1.35.0_2020.09.04/aria2-1.35.0-static-linux-amd64.tar.gz"),
    ("ghproxy.net",      "https://ghproxy.net/https://github.com/P3TERX/aria2-builder/releases/download/1.35.0_2020.09.04/aria2-1.35.0-static-linux-amd64.tar.gz"),
    ("gh-proxy.com",     "https://gh-proxy.com/https://github.com/P3TERX/aria2-builder/releases/download/1.35.0_2020.09.04/aria2-1.35.0-static-linux-amd64.tar.gz"),
    ("gh.llkk.cc",       "https://gh.llkk.cc/https://github.com/P3TERX/aria2-builder/releases/download/1.35.0_2020.09.04/aria2-1.35.0-static-linux-amd64.tar.gz"),
    ("GitHub (P3TERX)",  "https://github.com/P3TERX/aria2-builder/releases/download/1.35.0_2020.09.04/aria2-1.35.0-static-linux-amd64.tar.gz"),
]

def _temurin_jdk_mirrors(win: bool):
    """Зеркала портативной Temurin JDK 17 (zip для Windows, tar.gz для Linux).

    gh-прокси в начале — на случай_school/блокировок GitHub; прямой Adoptium
    API/GitHub — в конце как резерв.
    """
    if win:
        asset = "OpenJDK17U-jdk_x64_windows_hotspot_17.0.13_11.zip"
        base = ("https://github.com/adoptium/temurin17-binaries/releases/"
                "download/jdk-17.0.13%2B11/")
    else:
        asset = "OpenJDK17U-jdk_x64_linux_hotspot_17.0.13_11.tar.gz"
        base = ("https://github.com/adoptium/temurin17-binaries/releases/"
                "download/jdk-17.0.13%2B11/")
    out = [(name, prox + base + asset) for name, prox in _GH_PROXIES]
    out.append(("GitHub (прямой)", base + asset))
    return out


from modules.platform_utils import IS_WINDOWS as _IS_WIN  # noqa: E402
JDK_MIRRORS = _temurin_jdk_mirrors(_IS_WIN)

# Legacy Launcher Portable .jar — нативный запуск на Windows 10 (без Wine).
LEGACY_JAR_URL_API = "https://api.legacylauncher.net/ll.jar.php"

# Ручное скачивание: НЕ https://dl.legacylauncher.ru/legacy/installer —
# этот URL отдаёт .exe-установщик (не .jar) и часто отвечает 403 на
# не-браузерные запросы. Правильная страница с portable-сборками:
LEGACY_MANUAL_PAGE = "https://legacylauncher.ru/"
LEGACY_MANUAL_JAR_URL = "https://llaun.ch/jar"   # редирект на dl.llaun.ch/legacy/bootstrap

PRISM_URL = ("https://github.com/PrismLauncher/PrismLauncher/releases/"
             "download/11.0.3/PrismLauncher-Windows-MSVC-Portable-11.0.3.zip")

# ─────────────────────────────────────────────────────────────
# Legacy Launcher — .exe-установщик.
# Официальная ссылка есть, но .exe всё равно падает в Wine 11 (WOW64).
# Реально используется Portable .jar.
# ─────────────────────────────────────────────────────────────
LEGACY_MIRRORS = [
    ("legacylauncher.ru (официальный)",
     "https://dl.legacylauncher.ru/legacy/installer"),
    ("GitHub (gradenGnostic)",
     "https://github.com/gradenGnostic/LegacyLauncher/releases/download/v3.5.0/LegacyLauncher.Setup.3.5.0.exe"),
    ("ghfast.top",
     "https://ghfast.top/https://github.com/gradenGnostic/LegacyLauncher/releases/download/v3.5.0/LegacyLauncher.Setup.3.5.0.exe"),
    ("ghproxy.net",
     "https://ghproxy.net/https://github.com/gradenGnostic/LegacyLauncher/releases/download/v3.5.0/LegacyLauncher.Setup.3.5.0.exe"),
    ("gh-proxy.com",
     "https://gh-proxy.com/https://github.com/gradenGnostic/LegacyLauncher/releases/download/v3.5.0/LegacyLauncher.Setup.3.5.0.exe"),
    ("Archive.org",
     "https://archive.org/download/legacy-launcher-for-minecraft/LegacyLauncher_Installer_legacy.exe"),
]

_PRISM_WIN_BASE = ("https://github.com/PrismLauncher/PrismLauncher/releases/"
                   "download/11.1.1")

# Portable .zip — основная сборка для Windows. В Setup .exe и MinGW/w64
# сборках Qt ICU-библиотеки (icu.dll / icuuc.dll) НЕ включены в пакет —
# при запуске prismlauncher.exe Windows выдаёт «Не удается продолжить
# выполнение кода, поскольку система не обнаружила icu.DLL». Только в
# MSVC-Portable zip лежат icu*.dll рядом с exe.
PRISM_WIN_ZIP_NAME = "PrismLauncher-Windows-MSVC-Portable-11.1.1.zip"
PRISM_WIN_ZIP_URL = f"{_PRISM_WIN_BASE}/{PRISM_WIN_ZIP_NAME}"
_PRISM_PROXY_PREFIXES = ("https://ghfast.top/", "https://ghproxy.net/",
                         "https://gh.llkk.cc/", "https://gh-proxy.com/")

# Важно: у прокси-зеркал должен быть ТОТ ЖЕ размер ответа, что и у GitHub.
# Некоторые зеркала на неизвестный asset отдают 404-страницу (несколько КБ) —
# раньше такое «скачивание» молча проходило проверку min_size_mb=10 только по
# размеру zip-а; теперь download_file дополнительно сверяет точный размер и
# магические байты PK\x03\x04 (см. _prism_zip_ok в minecraft.py).
#
# dlcdn.com.cdn.delivered.by — тот же Яндекс-CDN, который для Legacy
# стабильно проходит школьные сети (см. LEGACY_WIN_OFFICIAL_CDN): GitHub
# отдаёт ему свой asset напрямую, домен Яндекса фильтры обычно не рвут.
PRISM_YANDEX_CDN_URL = ("https://dlcdn.com.cdn.delivered.by/" +
                        PRISM_WIN_ZIP_URL.removeprefix("https://"))
PRISM_WIN_URLS = [
    ("GitHub (релиз)", PRISM_WIN_ZIP_URL),
    ("Яндекс CDN (delivered.by)", PRISM_YANDEX_CDN_URL),
    ("ghfast.top", _PRISM_PROXY_PREFIXES[0] + PRISM_WIN_ZIP_URL),
    ("ghproxy.net", _PRISM_PROXY_PREFIXES[1] + PRISM_WIN_ZIP_URL),
    ("gh.llkk.cc", _PRISM_PROXY_PREFIXES[2] + PRISM_WIN_ZIP_URL),
    ("gh-proxy.com", _PRISM_PROXY_PREFIXES[3] + PRISM_WIN_ZIP_URL),
]

# Зеркала Legacy Launcher Portable (.jar) для Windows с учётом блокировок:
# к официальным доменам школьные фильтры часто рвут TLS (WinError 10054).
# Порядок важен для curl-очереди (download_file на Windows сначала гоняет
# curl.exe по всей очереди): archive.org — обычный статичный HTTPS без
# редиректов иTLS-фиngerprint-блокировок, поэтому первым; официальные
# домены — следом.
# Официальный Yandex-CDN Legacy Launcher отдаёт НАСТОЯЩИЙ установщик .exe,
# проверено работает даже когда все зеркала .jar блокируются сетью.
# Он медленный (думает 20-30 сек), но стабильный и качает до конца.
LEGACY_WIN_OFFICIAL_CDN = [
    ("Yandex CDN (официальный .exe)",
     "https://dl-cdn-yc.legacylauncher.ru/legacy/installer/LegacyLauncher.exe"
     "?md5=amzePVm455iPVFWWsKbn8A&expires=1792485882"),
]

# Скачанный браузером установщик Legacy тоже принимаем: в Windows он
# кладёт файл как «Legacy Launcher Setup.exe» (с пробелами) — раньше поиск
# кэша знал только наше имя LegacyLauncher-setup.exe, и уже скачанный
# установщик не находился, скрипт качал 117 МБ заново.
LEGACY_EXE_CACHE_NAMES = ("LegacyLauncher-setup.exe",
                          "Legacy Launcher Setup.exe",
                          "LegacyLauncherSetup.exe")

LEGACY_JAR_WIN_MIRRORS = [
    # Сначала пробуем быстрые .jar зеркала — если сеть их не блокирует, это
    # самый чистый путь (portable без установщика). Но они часто RST'ятся DPI.
    ("llaun.ch (официальный)", "https://llaun.ch/jar"),
    ("dl.llaun.ch (прямой)", "https://dl.llaun.ch/legacy/bootstrap"),
    # Если .jar недоступны — сразу переходим к официальному .exe через CDN:
    # он медленнее стартует (долгий HEAD + TLS handshake), но надёжнее проходит
    # через корпоративные файрволы и школьные MITM-прокси.
    *LEGACY_WIN_OFFICIAL_CDN,
    ("Archive.org (bootstrap jar)",
     "https://archive.org/download/legacy-launcher-bootstrap/"
     "LegacyLauncher.jar"),
    ("go.legacylauncher.ru", "https://go.legacylauncher.ru/jar"),
]

# Запасной вариант: официальный установщик от разработчиков (Qt из их
# репозитория, icu.dll ставит сам). Качается через браузер, если
# автоматическая загрузка блокируется сетью.
PRISM_WIN_OFFICIAL_PAGE = "https://prismlauncher.org/downloads/"

LEGACY_URL = LEGACY_MIRRORS[0][1]

# ─────────────────────────────────────────────────────────────
# Legacy Launcher Portable (.jar) — автоскачивание.
# ─────────────────────────────────────────────────────────────
# llaun.ch / dl.llaun.ch отдают НАСТОЯЩИЙ portable bootstrap .jar и
# поддерживают докачку (Range) — они в начале очереди.
LEGACY_JAR_MIRRORS = [
    ("llaun.ch (официальный)",           "https://llaun.ch/jar"),
    ("dl.llaun.ch (прямой)",             "https://dl.llaun.ch/legacy/bootstrap"),
]

# Запасные зеркала: эти URL отдают НЕ portable .jar, а .exe-установщик
# (в config LEGACY_JAR_EXTS). В Windows-ветке legacy мы их не трогаем —
# .exe требует Wine и известен крашем на Wine 11 (WOW64).
LEGACY_JAR_FALLBACK_EXE = [
    ("go.legacylauncher.ru (официальный)", "https://go.legacylauncher.ru/jar"),
    ("dl.legacylauncher.ru (installer)",
     "https://dl.legacylauncher.ru/legacy/installer"),
    ("lln4.cc",                          "https://lln4.cc/jar"),
]
LEGACY_JAR_MIN_SIZE = 1 * 1024 * 1024   # реальный .jar ~ 2-5 МБ


def legacy_jar_mirrors(is_windows):
    """Очередь зеркал portable-лаунчера для download_file.

    На Windows — jar-зеркала с учётом сетевых блокировок (archive.org и
    официальные домены; мёртвый api.legacylauncher.net в конце). Раньше
    очередь начиналась с мёртвых/блокируемых зеркал и до живых llaun.ch
    дело не доходило.
    """
    if is_windows:
        out = list(LEGACY_JAR_WIN_MIRRORS)
    else:
        out = list(LEGACY_JAR_MIRRORS) + list(LEGACY_JAR_FALLBACK_EXE)
    out.append(("api.legacylauncher.net", LEGACY_JAR_URL_API))
    return out

# ─────────────────────────────────────────────────────────────
# Wine Mono — НЕ используется (Wine сам предлагает установку).
# ─────────────────────────────────────────────────────────────
WINE_MONO_PATH = Path.home() / ".cache" / "wine" / "wine-mono-9.0.0-x86.msi"
WINE_MONO_MIN_SIZE = 50 * 1024 * 1024

WINE_MONO_MIRRORS = [
    ("GitHub (wine-mono)",
     "https://github.com/wine-mono/wine-mono/releases/download/wine-mono-9.0.0/wine-mono-9.0.0-x86.msi"),
    ("ghfast.top",
     "https://ghfast.top/https://github.com/wine-mono/wine-mono/releases/download/wine-mono-9.0.0/wine-mono-9.0.0-x86.msi"),
    ("WineHQ (прямой)",
     "https://dl.winehq.org/wine/wine-mono/9.0.0/wine-mono-9.0.0-x86.msi"),
]

# Fabulously Optimized — официальный источник: GitHub Releases.
# Скачиваются ТОЛЬКО release-версии (не alpha/beta/rc): фильтруем по
# prerelease=False и additional фильтрами в именах тегов/файлов.
FO_GITHUB_API = "https://api.github.com/repos/Fabulously-Optimized/fabulously-optimized/releases"
FO_MODRINTH_API = "https://api.modrinth.com/v2/project/fabulously-optimized/version"

# ─────────────────────────────────────────────────────────────
# Luxium 2.8.4-pre-alpha: dual-loader (Forge + Fabric), MC 1.20.1.
# Приоритетный модпак — Fabulously Optimized (Fabric, MC 1.20.1): команда
# `fo` ставит его и подкладывает Luxium (fabric.mod.json внутри JAR).
# Запасной вариант — Optimized fps (Forge 1.20.1, без Oculus): команда
# `ofps`, поведение не изменилось.
# ─────────────────────────────────────────────────────────────
# ВАЖНО: ванильный FO v5.2.8 плохо работает с Luxium — пришлось чинить
# модпак вручную (обновлены Fabric loader и все моды, удалены Controlify
# и Iris Shaders — шейдеры рендерит сам Luxium) и экспортировать готовый
# .mrpack из Modrinth в GitHub-релизы этого репозитория (Godlike-Bagheera/
# wine-installer). Поэтому источник по умолчанию — ИСПРАВЛЕННЫЙ mrpack с
# нашего GitHub; официальный FO v5.2.8 остаётся запасным вариантом.
# ФАЙЛ НЕ ЗАКОММИЧЕН В РЕПОЗИТОРИЙ (см. .gitignore): починку выкладываешь
# ты один раз в GitHub-релиз, пользователи получают её автоматически при
# `fo` — вручную ничего менять не нужно. Локальный *.mrpack (в корне репо или
# ~/wine-portable/) — только офлайн-кэш/переопределение, не обязательность.
# Переопределение: WI_FO_MRPACK_URL — ссылка на свой mrpack; WI_FO_OWNER /
# WI_FO_REPO — другой репозиторий-источник; WI_FO_VERSION — тег релиза
# (для официального FO: v5.2.8).
FO_1201_MC_VERSION = "1.20.1"
FO_MRPACK_OWNER = os.environ.get("WI_FO_OWNER", "Godlike-Bagheera")
FO_MRPACK_REPO = os.environ.get("WI_FO_REPO", "wine-installer")
FO_1201_PIN_TAG = os.environ.get("WI_FO_VERSION", "fo-5.2.8-fixed")
# Ссылка на фиксированный .mrpack: точный URL или префикс релиза
# (к нему дописывается имя файла mrpack при скачивании).
FO_1201_MRPACK_URL = os.environ.get("WI_FO_MRPACK_URL", "").strip() or (
    f"https://github.com/{FO_MRPACK_OWNER}/{FO_MRPACK_REPO}/releases/"
    f"download/{FO_1201_PIN_TAG}/")


def fo_mrpack_mirrors(url):
    """Зеркала для mrpack/Luxium с github.com releases.

    Порядок: 1) пользовательский WI_FO_MRPACK_MIRRORS ("url1,url2", {url}
    подставляется вместо относительной части), 2) gh-прокси из settings.json
    (gh_proxies()), 3) прямой адрес. Прокси решает проблему доступа к
    GitHub из РФ без каких-либо ручных сборок. Не-github URL возвращается
    как есть (один вариант).
    """
    direct = [("GitHub (прямой)", url)]
    if not re.search(r"(?:^|//)(?:www\.)?github\.com/", url):
        return direct
    rel = re.sub(r"^https?://(?:www\.)?github\.com/", "", url)
    out = []
    raw = os.environ.get("WI_FO_MRPACK_MIRRORS", "")
    for m in [x.strip() for x in raw.split(",") if x.strip()]:
        out.append(("custom", m.format(url=url, rel=rel)
                    if "{url}" in m or "{rel}" in m else m + rel))
    out += [(name, prefix + url) for name, prefix in gh_proxies()]
    return out + direct
# Запасной вариант — оригинальный неремонтированный FO v5.2.8 с официального
# GitHub (последний релиз FO для 1.20.1; ветки 13/14/15 — только 1.21+).
FO_OFFICIAL_GITHUB_API = ("https://api.github.com/repos/"
                          "Fabulously-Optimized/fabulously-optimized/releases")
FO_OFFICIAL_TAG = "v5.2.8"
FO_OFFICIAL_MRPACK_URL = (
    "https://github.com/Fabulously-Optimized/fabulously-optimized/releases/"
    "download/v5.2.8/Fabulously.Optimized-5.2.8.mrpack")


def find_local_mrpack():
    """Локальный .mrpack починенного FO: корень репо, затем кэш WINE_DIR.

    НЕ обязателен: обычный путь — скачивание из нашего GitHub-релиза через
    gh-прокси. Если файл всё же лежит рядом (офлайн-машина или переопреде-
    ление), сборка ставится полностью офлайн, без GitHub/Modrinth.
    Свежие первыми; битые zip-файлы пропускаются. Возвращает Path или None.
    """
    for base in (REPO_ROOT, WINE_DIR):
        try:
            if not base.is_dir():
                continue
            for p in sorted(base.glob("*.mrpack"),
                            key=lambda p: p.stat().st_mtime, reverse=True):
                # минимальный порог маленький (минимальный валидный zip ~130
                # байт): реальный mrpack — десятки МБ, но тестовые/пустые
                # манифесты тоже должны находиться; мусор отсекает is_zipfile
                if zipfile.is_zipfile(p) and p.stat().st_size > 100:
                    return p
        except Exception:
            continue
    return None


# fabric.mod.json Luxium (читается из локального JAR): обязательные зависимости.
LUXIUM_FABRIC_REQUIRED = ["sodium", "fabric-api"]
# Sodium строго "0.5.13" (см. depends в fabric.mod.json); на Modrinth это
# версия mc1.20.1-0.5.13-fabric, файл sodium-fabric-0.5.13+mc1.20.1.jar.
SODIUM_PROJECT_ID = "AANobbMI"
SODIUM_VERSIONS_API = f"https://api.modrinth.com/v2/project/{SODIUM_PROJECT_ID}/version"
SODIUM_PINNED_VERSION = "0.5.13"
SODIUM_MC_VERSION = "1.20.1"

# Optimized fps (Forge-модпак оптимизации, ЗАПАСНОЙ вариант для Luxium) —
# Modrinth. Ставится без мода Oculus (шейдер-мод на Iris) и с шейдер-модом
# Luxium (forge-часть dual-JAR) — команда `ofps`.
OFPS_MODRINTH_API = "https://api.modrinth.com/v2/project/optimized-fps/version"
# Minecraft-side номер сборки Forge для Luxium (47.x == MC 1.20.1).
LUXIUM_MC_SIDE = 47
# Embeddium — mandatory-зависимость Luxium (см. META-INF/mods.toml внутри
# Luxium JAR: [[dependencies.luxium]] modId="embeddium" versionRange="[...]").
# Требуемая версия берётся из самого JAR (embeddium_version_range()), а эти
# константы — только дефолт/документация на случай, если JAR не читается.
# ВАЖНО: проверено живым запросом к api.modrinth.com — id "xzK9D5Yv" отдаёт
# 404 (это не Embeddium); правильный проект — slug "embeddium" / id "sk9rgfiA".
# ВНИМАНИЕ: EMBEDDIUM_PROJECT_ID используется ТОЛЬКО в forge-пуле мода
# (install_embeddium_mod, команда `ofps`). В Fabric-сборке FO его нет —
# там рендер-бэкенд обеспечивает сам Luxium.
EMBEDDIUM_PROJECT_ID = "sk9rgfiA"
EMBEDDIUM_VERSIONS_API = f"https://api.modrinth.com/v2/project/{EMBEDDIUM_PROJECT_ID}/version"
EMBEDDIUM_MIN_VERSION = "0.3.31"          # для Luxium 2.8.0-pre-alpha
EMBEDDIUM_MC_VERSION = "1.20.1"
# Ожидаемый SHA-1 embeddium-0.3.31+mc1.20.1.jar (проверено живым запросом к
# api.modrinth.com, version id UTbfe5d1). Используется как offline-pin: если
# сеть/CDN недоступны, локальный embeddium-jar валидируется по этому хешу —
# и его можно безопасно закоммитить в репозиторий рядом с Luxium JAR.
EMBEDDIUM_SHA1_PIN = "bb2fa8f3e493af16af9160d049f96c614a1faf2f"


def _ver_tuple(v):
    """'0.3.31+mc1.20.1' / '1.2.0-beta.2' -> (1, 2, 0) — первые числа версии."""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:3]) or (0,)


def _luxium_jar_candidates():
    """Все локальные Luxium JAR (корень репо + кэш WINE_DIR), свежие первыми."""
    cands = []
    for base in (REPO_ROOT, WINE_DIR):
        try:
            if base.is_dir():
                cands.extend(base.glob("*uxi*.jar"))
        except Exception:
            continue
    return sorted(cands, key=lambda p: p.stat().st_mtime, reverse=True)


def luxium_required_deps():
    """Обязательные зависимости Luxium из META-INF/mods.toml локального JAR.

    Возвращает {modId: (мин_версия, макс_версия_или_None)}; парсятся секции
    [[dependencies.*]] с mandatory=true (mandatory=false игнорируется).
    Хардкод-дефолт — {'embeddium': ('0.3.31', '0.4')} (Luxium 2.8.0-pre-alpha).
    """
    default = {"embeddium": (EMBEDDIUM_MIN_VERSION, "0.4")}
    # корень репо + кэш WINE_DIR (скачанный с GitHub-зеркала JAR), свежие первыми
    for jar in _luxium_jar_candidates():
        try:
            if not zipfile.is_zipfile(jar):
                continue
            with zipfile.ZipFile(jar) as z:
                raw = z.read("META-INF/mods.toml").decode("utf-8", errors="replace")
        except Exception:
            continue
        deps = {}
        for block in re.split(r"\n(?=\[\[)", raw):
            if not block.lstrip().startswith("[[dependencies"):
                continue
            m_id = re.search(r'modId\s*=\s*"([^"]+)"', block)
            if not m_id:
                continue
            mand = re.search(r"mandatory\s*=\s*(\w+)", block)
            if mand and mand.group(1).lower() != "true":
                continue
            rng = re.search(r'versionRange\s*=\s*"([^"]+)"', block)
            if not rng:
                continue
            # формат maven range: "[0.3.31,0.4)" / "[47,)" / "[47.1.3,)"
            m = re.match(r"[\[(]\s*([\w.\-+]+)\s*(?:,\s*([\w.\-+]+))?\s*[\])]?",
                         rng.group(1))
            if not m:
                continue
            lo = m.group(1).split("-")[0]      # '47.1.3' -> '47.1.3'; '47' ok
            hi = m.group(2) or None
            deps[m_id.group(1)] = (lo, hi)
        return deps or default
    return default


def embeddium_version_range():
    """(мин, макс_или_None) Embeddium из mods.toml Luxium; дефолт — хардкод."""
    lo, hi = luxium_required_deps().get("embeddium",
                                        (EMBEDDIUM_MIN_VERSION, "0.4"))
    return lo, hi


def _luxium_local_jar_paths():
    """Локальные .jar Luxium (корень репо + кэш WINE_DIR), свежие первыми."""
    out = []
    for p in _luxium_jar_candidates():
        try:
            if zipfile.is_zipfile(p):
                out.append(p)
        except Exception:
            pass
    return out


def luxium_fabric_meta():
    """fabric.mod.json Luxium 2.8.4+ (dual-loader Forge/Fabric JAR).

    Возвращает dict (depends, id, version...) или None, если ни один локальный
    JAR не содержит fabric.mod.json (старые forge-only сборки Luxia-Shaders).
    """
    for jar in _luxium_local_jar_paths():
        try:
            with zipfile.ZipFile(jar) as z:
                raw = z.read("fabric.mod.json").decode("utf-8", errors="replace")
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("id"):
                return data
        except Exception:
            continue
    return None


def luxium_supports_fabric():
    """True, если локальный Luxium JAR умеет Fabric (есть fabric.mod.json)."""
    meta = luxium_fabric_meta()
    return bool(meta and meta.get("entrypoints"))


def luxium_fabric_dep(mod_id):
    """Требование Luxium к fabric-моду из fabric.mod.json depends.

    'sodium': '0.5.13' (точная версия), 'fabric-api': '>=0.92.2+1.20.1'.
    Возвращает строку требования или None.
    """
    meta = luxium_fabric_meta() or {}
    dep = (meta.get("depends") or {}).get(mod_id)
    return dep if isinstance(dep, str) else None


# Зеркала cdn.modrinth.com (в РФ CDN может блокироваться). API modrinth.com
# при этом остаётся доступен, поэтому проксируем только URL самих файлов.
# Зеркала cdn.modrinth.com. ВНИМАНИЕ (проверено живыми запросами):
# gh-прокси (gh-proxy.com / ghfast.top / ghproxy.net) проксируют ТОЛЬКО
# github.com — на Modrinth CDN они отдают 403/404; mirror.ileo.dev не
# резолвится. Поэтому список пуст: честнее один раз попробовать прямой
# URL, чем гонять пользователя по заведомо мёртвым зеркалам. Если
# появится проверенное рабочее зеркало cdn.modrinth.com — добавь сюда
# префикс (он оборачивает полный URL, как gh-прокси для GitHub).
MRCDN_PROXIES = []


def modrinth_file_mirrors(url):
    """[(label, url)] для файла с cdn.modrinth.com: зеркала + прямой адрес.

    Рабочих публичных зеркал cdn.modrinth.com не существует (см. комментарий
    к MRCDN_PROXIES), поэтому всегда возвращается только прямой URL. Если
    CDN блокируется провайдером — единственный надёжный путь: положить jar
    локально (см. install_embeddium_mod / README).
    Для прочих доменов возвращает [] — подмешивать нечего.
    """
    if "cdn.modrinth.com" not in (url or ""):
        return []
    out = [(p.rstrip("/").split("//")[-1], p + url) for p in MRCDN_PROXIES]
    out.append(("Modrinth CDN (прямой)", url))
    return out
# Прямые зеркала .jar с CurseForge CDN (официальный адрес edge.forgecdn.net +
# публичные mirror-домены). Итоговый путь: <база>/files/<id//1000>/<id%1000>/<имя>.jar
CF_EDGE_BASES = [
    "https://edge.forgecdn.net",
    "https://media.curseforge.com",
    "https://addons-ecs.forgesvc.net",
]
OPTIFINE_PAGE = "https://optifine.net/downloads"
FABRIC_META = "https://meta.fabricmc.net/v2/versions/installer"
FABRIC_MAVEN = "https://maven.fabricmc.net/net/fabricmc/fabric-installer"

# Fabric meta API — профили версий (для установки БЕЗ Java, если installer
# недоступен) и maven-репозиторий библиотек.
FABRIC_META_API = "https://meta.fabricmc.net/v2"
MAVEN_FABRIC = "https://maven.fabricmc.net/"
MAVEN_CENTRAL = "https://repo1.maven.org/maven2/"

# Mojang — списки версий и клиентские jar'ы (для доведения версии до конца,
# чтобы лаунчер видел установленный модпак целиком).
MOJANG_VERSION_MANIFEST = "https://launchermeta.mojang.com/mc/game/version_manifest.json"
MOJANG_RESOURCES = "https://resources.download.minecraft.net/"


def _gh_mirror_list(base_url, direct_name="GitHub (прямой)"):
    """base_url + прокси-зеркала из gh_proxies() (+ прямой адрес в конце)."""
    return gh_proxies() + [(direct_name, base_url)]


def make_github_mirrors(url):
    """Оборачивает ссылку github.com прокси-зеркалами (для плохих сетей).

    Прокси умеют отдавать ассеты GitHub Releases — поддерживаются все
    форматы ссылок:
      - github.com/<owner>/<repo>/releases/download/<tag>/<file>
        (конкретный релиз);
      - github.com/<owner>/<repo>/releases/latest/download/<file>
        (последний релиз, используется в офлайн-каталоге шейдеров);
      - ghuser.io/<owner>/<repo>@<tag>/<file> — сокращённые ссылки
        (встречаются в конфигах модпаков);
      - адреса с портом (github.com:8080/...) и редирект-пути
        (/releases/latest/..., /releases/tag/...).
    Для api.github.com и прочих URL возвращают пустой список — качаем прямо.
    """
    # Сокращённая форма ghuser/repo@tag/file — свои зеркала.
    canonical = _short_github_form(url)
    if canonical:
        return [(name, f"{prefix}{canonical}") for name, prefix in gh_proxies()]

    if "github.com" not in url:
        return []
    # Только API — прокси не нужны (и ломают запрос), качаем прямо.
    no_scheme = re.sub(r"^\w+://", "", url).lower()
    if no_scheme.startswith("api.github.com") or "://api.github.com" in url.lower():
        return []
    # Раньше путь вырезался жадным регэкспом «(.+?)/releases», из-за чего
    # адреса с портом (github.com:8080/owner/repo/releases/...) давали битые
    # зеркала (owner/repo:8080). Теперь host и path разделяются явно, а
    # owner/repo берётся non-greedily до '/releases'.
    m = re.search(r"github\.com(?::\d+)?/(.+?)/releases(/.*)?$", url)
    if not m:
        return []
    # Редиректы /releases/latest и /releases/tag/<t> — прямой download-ассет
    # неизвестен; оборачиваем ссылку целиком (прокси следуют редиректам).
    return [(name, f"{prefix}{url}") for name, prefix in gh_proxies()]


def _short_github_form(url):
    """Сокращённая форма ghuser/repo@tag/file (gh.io, github.io, git.io).

    Встречается в конфигах модпаков; раньше не поддерживалась вовсе.
    Возвращает канонический URL releases/download или None.
    """
    m = re.search(r"(?:^|[/:.])g(?:h|it)\.io/(?:github\.(?:io|com)/)?"
                  r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)@([^/?#]+)/([^?#]+)", url)
    if not m:
        m = re.search(r"github\.(?:io|com)/([A-Za-z0-9_.-]+)/"
                      r"([A-Za-z0-9_.-]+)@([^/?#]+)/([^?#]+)", url)
    if not m:
        return None
    owner, repo, tag, asset = m.groups()
    return (f"https://github.com/{owner}/{repo}"
            f"/releases/download/{tag}/{asset}")


def make_dxvk_mirrors(version):
    fname = f"dxvk-{version.lstrip('v')}.tar.gz"
    base = f"https://github.com/doitsujin/dxvk/releases/download/{version}/{fname}"
    return _gh_mirror_list(base)


# ---------- КАТАЛОГ БЕСПЛАТНЫХ ИГР ----------
FREE_GAMES = [
    ("Cave Story",            "https://www.cavestory.org/downloads/cavestoryen.zip"),
    ("Doom (shareware)",      "https://distro.ibiblio.org/slitaz/sources/packages/d/doom1.wad"),
    ("OpenTTD (Windows)",     "https://cdn.openttd.org/openttd-releases/latest/openttd-windows.zip"),
    ("Wolfenstein 3D",        "https://distro.ibiblio.org/slitaz/sources/packages/w/wolf3d.zip"),
    ("SuperTux (Linux AppImage)", "https://github.com/SuperTux/supertux/releases/download/v0.6.3/SuperTux-v0.6.3-x86_64.AppImage"),
    ("Battle for Wesnoth",    "https://sourceforge.net/projects/wesnoth/files/latest/download"),
]
