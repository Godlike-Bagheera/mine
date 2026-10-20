@echo off
REM ============================================================
REM  MC Installer for Windows 10 - bootstrap (cmd.exe)
REM  Скачивает репозиторий Godlike-Bagheera\mine и запускает launcher.py
REM  Использование:  install.cmd            (обычный запуск)
REM                  install.cmd --debug    (подробный лог)
REM ============================================================
setlocal EnableExtensions
chcp 65001 >nul
title MC Installer bootstrap

set "REPO_URL=https://github.com/Godlike-Bagheera/mine.git"
set "ZIP_URL=https://github.com/Godlike-Bagheera/mine/archive/refs/heads/main.zip"
set "DEST=%USERPROFILE%\mc-installer"

echo.
echo  [MC Installer] Bootstrap для Windows 10...
echo.

REM ---------- ищем Python ----------
REM ВАЖНО: где-то в PATH может лежать алиас python.exe из Microsoft Store,
REM который ничего не делает, а лишь открывает магазин и мгновенно завершается
REM с кодом 9009 — из-за этого окно закрывалось за долю секунды ("запускается и
REM сразу закрывается"). Поэтому проверяем каждый кандидат реальным запуском.
set "PY="

REM 1) launcher "py" — самый надёжный способ на Windows 10
py -3 -c "print(1)" >nul 2>nul
if not errorlevel 1 set "PY=py -3"

REM 2) python / python3 из PATH (исключая пустые aliасы Microsoft Store)
if not defined PY (
    for %%P in (python python3) do (
        if not defined PY (
            where %%P >nul 2>nul
            if not errorlevel 1 (
                %%P -c "print(1)" >nul 2>nul
                if not errorlevel 1 set "PY=%%P"
            )
        )
    )
)

REM 3) ищем настоящую установку Python в типовых папках
if not defined PY (
    for /d %%V in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
        if exist "%%V\python.exe" (
            "%%V\python.exe" -c "print(1)" >nul 2>nul
            if not errorlevel 1 if not defined PY set "PY=%%V\python.exe"
        )
    )
)
if not defined PY (
    for /d %%V in ("C:\Python*" "C:\Program Files\Python*") do (
        if exist "%%V\python.exe" (
            "%%V\python.exe" -c "print(1)" >nul 2>nul
            if not errorlevel 1 if not defined PY set "PY=%%V\python.exe"
        )
    )
)

if not defined PY (
    echo  [!] Python не найден ^(или это заглушка Microsoft Store^).
    echo      Пробую поставить через winget...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    py -3 -c "print(1)" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
    if not defined PY where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo  [X] Не удалось найти или установить Python.
    echo      Поставь вручную: https://www.python.org/downloads/ ^(галочка "Add to PATH"^)
    echo      затем запусти install.cmd снова.
    pause
    exit /b 1
)

REM ---------- способ 1: git ----------
where git >nul 2>nul
if %ERRORLEVEL%==0 (
    if exist "%DEST%\.git" (
        echo  [..] Обновляю существующую копию через git pull...
        REM Если в локальной копии есть незакоммиченные правки — не даём им
        REM блокировать pull (раньше из-за этого pull падал и скрипт мог
        REM закрыться, не дойдя до запуска).
        REM -c http.proxy= -c https.proxy= — игнорируем системный/школьный
        REM прокси: он рвёт TLS к github.com («интернет как будто нет»).
        git -C "%DEST%" stash -q >nul 2>nul
        git -C "%DEST%" -c http.proxy= -c https.proxy= pull --ff-only
        if errorlevel 1 (
            echo  [!] git pull не прошёл. Похоже на блокировку прокси/DNS:
            echo      проверь настройки прокси ^(netfix в launcher.py^) или выполни вручную:
            echo      git config --global --unset http.proxy ^&^& git config --global --unset https.proxy
            echo      netsh winhttp reset proxy
            echo      Пробую PowerShell-фоллбек...
            goto :pszip
        )
    ) else (
        echo  [..] Клонирую репозиторий через git...
        git -c http.proxy= -c https.proxy= clone --depth 1 "%REPO_URL%" "%DEST%"
        if errorlevel 1 (
            echo  [!] git clone не прошёл. Похоже на блокировку прокси/DNS:
            echo      отключи прокси Windows ^(Параметры - Сеть и Интернет - Прокси^)
            echo      или выполни в cmd от администратора: netsh winhttp reset proxy
            echo      Пробую PowerShell-фоллбек...
            goto :pszip
        )
    )
    goto :run
)

REM ---------- способ 2: PowerShell + zip ----------
:pszip
echo  [..] Git недоступен - качаю zip-архив через PowerShell...
REM -Proxy $null заставляет Invoke-WebRequest игнорировать системный
REM прокси WinHTTP/IE — иначе школьный фильтр рвёт загрузку архива.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$ProgressPreference='SilentlyContinue';" ^
  "New-Item -ItemType Directory -Force -Path '%TEMP%\mcinst' | Out-Null;" ^
  "Invoke-WebRequest -Uri '%ZIP_URL%' -OutFile '%TEMP%\mcinst\main.zip' -Proxy $null;" ^
  "Expand-Archive -Force '%TEMP%\mcinst\main.zip' '%TEMP%\mcinst\ext';" ^
  "if (Test-Path '%DEST%') { Remove-Item -Recurse -Force '%DEST%' };" ^
  "Move-Item '%TEMP%\mcinst\ext\mine-main' '%DEST%'"
if not exist "%DEST%\launcher.py" (
    echo  [X] Загрузка не удалась. Скачай архив вручную:
    echo      %ZIP_URL%
    pause
    exit /b 1
)

:run
if not exist "%DEST%\launcher.py" (
    echo  [X] launcher.py не найден в %DEST%
    pause
    exit /b 1
)
echo  [OK] Запускаю установщик...
echo.
%PY% "%DEST%\launcher.py" %**
set "RC=%ERRORLEVEL%"
REM ВСЕГДА держим окно открытым после завершения (иначе при любом козе или
REM быстром выходе пользователь видит только мелькнувшее окно и всё).
if not "%RC%"=="0" echo.
if not "%RC%"=="0" echo  [i] Код выхода: %RC%
echo.
pause
endlocal
exit /b %RC%
