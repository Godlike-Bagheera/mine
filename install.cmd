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
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo  [!] Python не найден. Пробую поставить через winget...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    where python >nul 2>nul && set "PY=python"
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
        git -C "%DEST%" pull --ff-only
    ) else (
        echo  [..] Клонирую репозиторий через git...
        git clone --depth 1 "%REPO_URL%" "%DEST%"
    )
    goto :run
)

REM ---------- способ 2: PowerShell + zip ----------
echo  [..] Git не найден - качаю zip-архив через PowerShell...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$ProgressPreference='SilentlyContinue';" ^
  "New-Item -ItemType Directory -Force -Path '%TEMP%\mcinst' | Out-Null;" ^
  "Invoke-WebRequest -Uri '%ZIP_URL%' -OutFile '%TEMP%\mcinst\main.zip';" ^
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
if errorlevel 1 pause
endlocal
