"""Сеть: единый SSL-контекст и безопасная распаковка архивов.

Проверка сертификатов по умолчанию СТРОГАЯ (как в обычном Python):
сначала стандартное доверенное хранилище, поверх него добавляются системные
CA из Red OS (/etc/ssl/certs/ca-certificates.crt и т.п.) — так работают и
цепочки GitHub/Mojang, и корни Минцифры, которыми школьная сеть может
подменять TLS (MITM-прокси).

CERT_NONE больше НЕ включён молча; он доступен двумя способами:
  ssl_ctx(insecure=True)                — для мест, где нужен явный фолбек
                                          после SSLCertVerificationError
                                          (см. _open_checked в download.py)
  WI_INSECURE_SSL=1                     — глобально отключить проверку
                                          (страховка на случай, если строгий
                                          режим на какой-то сборке RED OS
                                          всё же сломает скачивание)
Раньше такой контекст копировался 6 раз по модулям; теперь он ОДИН
(minecraft/shaders/download/winetricks берут его отсюда). Ответственность
за целостность содержимого дополнительно держат SHA-хеши
(modules.hash_utils) и сигнатуры файлов (download._looks_binary).
"""
import os
import ssl
import sys
import tarfile
import zipfile
from pathlib import Path

# Системные хранилища CA: Debian/Ubuntu/Red OS (openssl-linked), плюс каталог
# hash-симлинков для cert-режима. Разные дистрибутивы кладут их в разные места.
_CA_BUNDLE_PATHS = (
    "/etc/ssl/certs/ca-certificates.crt",          # Debian/Ubuntu/RED OS
    "/etc/pki/tls/certs/ca-bundle.crt",            # RHEL/Fedora-путь
    "/etc/ssl/ca-bundle.pem",                      # openSUSE
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # ca-tools (RED OS)
)
_CA_CERT_DIR = "/etc/ssl/certs"

_ctx_cache = {}


def _warn(msg):
    try:
        from modules.debug import dbg as _dbg
        _dbg(f"net.ssl: {msg}", level="WARN")
    except Exception:
        pass
    # WARN пишем в stderr даже без --debug — тихая деградация безопасности
    # не должна быть невидимой; молчим только если вывод перенаправлен в файл.
    if not sys.stderr.isatty():
        return
    print(f"[net] {msg}", file=sys.stderr)


def _build_ctx(insecure):
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    # Поверх стандартного хранилища — системные CA (корни Минцифры в RED OS
    # живут именно там; Python их сам не видит, если собран со своим certifi).
    for p in _CA_BUNDLE_PATHS:
        if os.path.isfile(p):
            try:
                ctx.load_verify_locations(cafile=p)
            except Exception:
                pass
    if os.path.isdir(_CA_CERT_DIR):
        try:
            ctx.load_verify_locations(capath=_CA_CERT_DIR)
        except Exception:
            pass
    return ctx


def ssl_ctx(insecure=False):
    """Единый SSL-контекст проекта (см. docstring модуля).

    По умолчанию строгий: стандартные CA + системные бандлы RED OS.
    insecure=True — CERT_NONE (использовать только как фолбек после
    SSLCertVerificationError или при WI_INSECURE_SSL=1).
    """
    global _ctx_cache
    if not isinstance(_ctx_cache, dict):  # защита от старых кэшей/тестов
        _ctx_cache = {}
    env_insecure = os.environ.get("WI_INSECURE_SSL") == "1"
    if env_insecure and not insecure:
        insecure = True
    if insecure and not _ctx_cache.get("_warned"):
        _ctx_cache["_warned"] = True
        _warn("SSL-контекст без проверки сертификатов (WI_INSECURE_SSL=1 "
              "или явный insecure=True)")
    key = "insecure" if insecure else "strict"
    if key not in _ctx_cache:
        _ctx_cache[key] = _build_ctx(insecure)
    return _ctx_cache[key]


def is_insecure():
    """True, если глобально отключена проверка сертификатов (env-флаг)."""
    return os.environ.get("WI_INSECURE_SSL") == "1"


# ─────────────────────────────────────────────────────────────────────
#  Безопасная распаковка (защита от path traversal / zip-slip)
# ─────────────────────────────────────────────────────────────────────

def _safe_target(dest_dir, member_name):
    """Путь внутри dest_dir для имени из архива или None, если имя опасное.

    Отсекается: абсолютные пути, '..', выход за пределы dest_dir
    (в т.ч. через симлинки-предки), Windows-префиксы вида C:/path.
    """
    name = str(member_name).replace("\\", "/")
    if not name or name.startswith("/") or ":" in name.split("/")[0]:
        return None
    parts = []
    for seg in name.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            return None
        parts.append(seg)
    if not parts:
        return None
    dest_root = os.path.realpath(str(dest_dir))
    target = os.path.realpath(os.path.join(dest_root, *parts))
    if target != dest_root and not target.startswith(dest_root + os.sep):
        return None
    return target


def safe_extract_tar(archive, dest_dir):
    """tar.extractall с фильтрацией опасных имён (работает на Python 3.9+)."""
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers()
                   if _safe_target(dest_dir, m.name) is not None]
        try:
            # Python 3.12+: встроенный фильтр data (на 3.9 TypeError — тихо мимо)
            tar.extractall(dest_dir, members=members, filter="data")
        except TypeError:
            tar.extractall(dest_dir, members=members)


def safe_extract_zip(archive, dest_dir):
    """zipfile.extractall с фильтрацией опасных имён (zip-slip)."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            target = _safe_target(dest, info.filename)
            if target is None:
                continue
            z.extract(info, dest)
