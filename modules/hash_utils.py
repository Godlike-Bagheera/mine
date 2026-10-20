"""Хеши (SHA512/SHA1) + буфер обмена."""
import hashlib
import subprocess
from modules import debug


def _hash_file(file_path, algo):
    """Потоковый хеш файла. Возвращает hex-строку или None."""
    try:
        h = hashlib.new(algo)
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest().lower()
    except Exception as e:
        debug.dbg_exc(e, f"_hash_file/{algo}")
        return None


def verify_sha512(file_path, expected_hex):
    if not expected_hex:
        return True, ""
    actual = _hash_file(file_path, "sha512")
    if actual is None:
        return False, ""
    return actual == expected_hex.lower(), actual


def verify_sha1(file_path, expected_hex):
    """Проверка SHA-1 (используется Mojang для client.jar)."""
    if not expected_hex:
        return True, ""
    actual = _hash_file(file_path, "sha1")
    if actual is None:
        return False, ""
    return actual == expected_hex.lower(), actual


def copy_to_clipboard(text):
    for cmd in [
        ["xclip", "-selection", "clipboard"],
        ["wl-copy"],
        ["xsel", "--clipboard", "--input"],
    ]:
        try:
            r = subprocess.run(
                cmd, input=text.encode(), check=False, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if r.returncode == 0:
                return True
        except (FileNotFoundError, Exception):
            continue
    return False
