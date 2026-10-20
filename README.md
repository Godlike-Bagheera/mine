# Wine Installer + Game Launcher

Запуск Windows-игр на Linux без sudo. Плюс установка Minecraft
(Legacy / Prism / Fabulously Optimized / OptiFine).

## Установка

**GitFlic (основной):**
```bash
git clone https://gitflic.ru/project/guroo/wine-installer.git
cd wine-installer
python3 wine.py
```

**GitHub (зеркало):**
```bash
git clone https://github.com/Godlike-Bagheera/wine-installer.git
cd wine-installer
python3 wine.py
```

Или просто одной строкой:
```bash
bash install.sh
```

## Возможности

- 🎮 Запуск `.exe` / `.msi` / `.lnk` через Wine AppImage
- 🚀 DXVK (d3d9 / d3d10 / d3d11) + автоматическая установка в префикс
- 🧩 Winetricks: автофиксы отсутствующих DLL (vcrun*, d3dx*, xact, ...)
- ⛏ Minecraft: Prism, Legacy Launcher, Fabulously Optimized, Optimized fps
  (`fo`: **приоритетный** модпак для Luxium — починенный Fabulously Optimized
  **v5.2.8-fixed** (ванильный v5.2.8 — последний релиз FO для MC 1.20.1;
  ветки 13/14/15 — только 1.21+; в починке обновлены Fabric loader и все моды,
  удалены Controlify и Iris Shaders — шейдеры рендерит сам Luxium). Источник —
  GitHub-релиз этого репозитория (тег `fo-5.2.8-fixed`, ассеты `.mrpack` +
  `Luxium*.jar`), скачивание через gh-прокси; локальные файлы в корне репо или
  `~/wine-portable/` — только офлайн-кэш/переопределение, в git они НЕ
  коммитятся. После сборки в `mods/` подкладывается Luxium (с 2.8.4-pre-alpha
  это dual-loader JAR Forge+Fabric), обязательная зависимость Sodium 0.5.13
  проверяется/доустанавливается с Modrinth. ВАЖНО: Embeddium в Fabric-пути не
  ставится — он форк Sodium и конфликтовал бы с ним. Тег/источник переопреде-
  ляются через `WI_FO_VERSION`, `WI_FO_MRPACK_URL`, `WI_FO_OWNER`/`WI_FO_REPO`,
  зеркала — `WI_FO_MRPACK_MIRRORS`;
  `ofps`: запасной вариант — Forge 1.20.1 без Oculus (Modrinth), Luxium — из
  GitHub-релиза/локального кэша/файла рядом с wine.py; обязательная зависимость
  Embeddium качается с Modrinth, при блокировке cdn.modrinth.com — с нашего
  GitHub-релиза через gh-прокси (валидация по SHA-1) либо из локального
  `*embeddium*.jar` (корень репо/кэш); рабочих публичных зеркал
  cdn.modrinth.com не существует),
  OptiFine, Fabric
- ☕ Портативная JDK 17 (Temurin) для OptiFine installer
- 🎯 GameMode, MangoHud, gamescope (опционально, через `config.py`)
- 📜 История запусков, логи на каждую игру, `debugreport`
- 🌍 Мультизеркала (ghfast.top / ghproxy.net / …) + докачка + slow-mode
- 📦 Экспорт истории/настроек (`export`)
- 🌡  Температура GPU (`gpu-temp`) — nvidia-smi или sensors
- 🆓 Каталог бесплатных игр (`freegames`)
- 🩺 Автопочинка сборки Fabulously Optimized (`fo-autofix`)

## Команды

Введи `help` внутри программы — увидишь полную справку.

## Требования

- Linux x86_64
- Python 3.9+
- ~2 ГБ свободного места
- Без root / sudo (Wine работает от пользователя)

## Настройки окружения

### Где что хранится

Все данные скрипт пишет **только в домашнюю папку** (`$HOME`), ничего не требует
и не трогает системные директории:

| Путь | Назначение |
|---|---|
| `~/wine-portable/` | Wine AppImage, префиксы, bin-утилиты — всё ядро лаунчера |
| `~/wine-portable/prefix/` | Windows-префикс по умолчанию (`WINEPREFIX`) |
| `~/wine-portable/prefixes/` | Дополнительные префиксы |
| `~/wine-portable/logs/` | Логи запуска каждой игры + `debug.log` |
| `~/wine-portable/history.json` | История запусков |
| `~/wine-portable/settings.json` | Пользовательские настройки |
| `~/wine-portable/.mirror_cache` | Кэш доступности зеркал скачивания |
| `~/games/` | Каталог игр для поиска `.exe` |
| `~/prism/` | Prism Launcher |
| `~/java/` | Портативная JDK 17 (Temurin) |

### Миры на «Диске D» (RED OS, откат системы после перезагрузки)

Команды `saveworlds` / `loadworlds` / `worlds` копируют миры из всех найденных
`.minecraft/saves` в папку `minecraft-worlds/` на Диске D и обратно. Скрипт
автоматически ищет Диск D среди: `~/Рабочий стол/Диск D`, `~/Desktop/Disk D`,
смонтированных томов в `/media`, `/run/media`, `/mnt` с именами вида «Диск D»,
«D», «New Volume». Если по пути нельзя писать (сетевой read-only ресурс вроде
`mnt`), скрипт честно об этом предупредит и предложит указать путь вручную.
Старые копии миров на диске не удаляются — они переименовываются в
`saves.bak-ГГГГММДД-ЧЧММСС`.

Переопределить домашнюю папку можно стандартной переменной `HOME` —
всё вышеперечисленное «поедет» за ней.

### Переменные окружения

| Переменная | Эффект |
|---|---|
| `WI_DEBUG=1` | Подробный лог в `~/wine-portable/logs/debug.log` (то же самое, что флаг `--debug`) |
| `WI_INSECURE_SSL=1` | Отключить проверку TLS-сертификатов при скачивании. По умолчанию проверка **строгая**: стандартные CA Python + системные хранилища RED OS (`/etc/ssl/certs`, `/etc/pki/...`). Если сертификат не прошёл (например, школьный MITM-прокси с корнем Минцифры, которого нет в доверии), лаунчер сам делает одну повторную попытку без проверки и печатает предупреждение; `=1` закрепляет insecure-поведение сразу, `=0` запрещает фолбек (жёсткая ошибка). Флаг применяется и к aria2c (`--check-certificate`) |
| `HOME` | Корень всех путей хранения (см. таблицу выше) |
| `WINEDEBUG`, `DXVK_HUD`, `MANGOHUD` … | Стандартные переменные Wine/DXVK/MangoHud — скрипт копирует твоё окружение (`os.environ.copy()`) и поверх ставит только нужное (`WINEPREFIX`, `WINEDLLOVERRIDES`), поэтому любые свои переменные можно задать в shell и они дойдут до игры |

### Полный конфиг

Логика выбора зеркал, версий DXVK/Winetricks/JDK лежат в `modules/config.py`
(только константы, без логики). Меню `settings` внутри программы позволяет
менять пользовательские опции без правки кода: они сохраняются в
`~/wine-portable/settings.json` (`quiet`, `gamemode`, `dxvkhud`, `mangohud`,
`gamescope`, `tts`, `pergameprefix`, `logdays`, а также свои зеркала GitHub
через ключ `gh_proxies`).

## Лицензия

MIT — см. [LICENSE](LICENSE).