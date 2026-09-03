from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from pathlib import Path

from .config import USER_DATA_DIR

SECRET_PATH = USER_DATA_DIR / "secrets.dat"
HITHINK_SECRET_PATH = USER_DATA_DIR / "hithink-secrets.dat"
CRYPTPROTECT_UI_FORBIDDEN = 0x01
_session_key: str | None = None
_storage_mode: str | None = None
_hithink_session_key: str | None = None
_hithink_storage_mode: str | None = None


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_crypt32.CryptProtectData.argtypes = (
    ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
)
_crypt32.CryptProtectData.restype = wintypes.BOOL
_crypt32.CryptUnprotectData.argtypes = (
    ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
)
_crypt32.CryptUnprotectData.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
_kernel32.LocalFree.restype = wintypes.HLOCAL


def _input_blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _free_blob(blob: DATA_BLOB) -> None:
    if blob.pbData:
        _kernel32.LocalFree(ctypes.cast(blob.pbData, wintypes.HLOCAL))


def protect(data: bytes) -> bytes:
    source, keepalive = _input_blob(data)
    target = DATA_BLOB()
    success = _crypt32.CryptProtectData(
        ctypes.byref(source), "Little Leaf API Key", None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(target),
    )
    _ = keepalive
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        _free_blob(target)


def unprotect(data: bytes) -> bytes:
    source, keepalive = _input_blob(data)
    target = DATA_BLOB()
    description = wintypes.LPWSTR()
    success = _crypt32.CryptUnprotectData(
        ctypes.byref(source), ctypes.byref(description), None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(target),
    )
    _ = keepalive
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        _free_blob(target)
        if description:
            _kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))


def save_deepseek_key(api_key: str, path: Path = SECRET_PATH) -> str:
    global _session_key, _storage_mode
    value = api_key.strip()
    if not value:
        raise ValueError("API Key不能为空")
    try:
        encoded = base64.b64encode(protect(value.encode("utf-8")))
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(path)
        _storage_mode = "dpapi"
    except OSError:
        path.unlink(missing_ok=True)
        _storage_mode = "memory"
    _session_key = value
    return _storage_mode


def save_hithink_key(api_key: str, path: Path = HITHINK_SECRET_PATH) -> str:
    global _hithink_session_key, _hithink_storage_mode
    value = api_key.strip()
    if not value:
        raise ValueError("API Key不能为空")
    try:
        encoded = base64.b64encode(protect(value.encode("utf-8")))
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(path)
        _hithink_storage_mode = "dpapi"
    except OSError:
        path.unlink(missing_ok=True)
        _hithink_storage_mode = "memory"
    _hithink_session_key = value
    return _hithink_storage_mode


def load_deepseek_key(path: Path = SECRET_PATH) -> str | None:
    if _session_key:
        return _session_key
    if not path.exists():
        return None
    try:
        encrypted = base64.b64decode(path.read_bytes(), validate=True)
        return unprotect(encrypted).decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def load_hithink_key(path: Path = HITHINK_SECRET_PATH) -> str | None:
    if _hithink_session_key:
        return _hithink_session_key
    if not path.exists():
        return None
    try:
        encrypted = base64.b64decode(path.read_bytes(), validate=True)
        return unprotect(encrypted).decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def clear_deepseek_key(path: Path = SECRET_PATH) -> dict[str, object]:
    global _session_key, _storage_mode
    _session_key = None
    _storage_mode = None
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {"configured": False, "fileRemoved": existed}


def clear_hithink_key(path: Path = HITHINK_SECRET_PATH) -> dict[str, object]:
    global _hithink_session_key, _hithink_storage_mode
    _hithink_session_key = None
    _hithink_storage_mode = None
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {"configured": False, "fileRemoved": existed}


def deepseek_status() -> dict[str, object]:
    key = load_deepseek_key()
    storage = _storage_mode or ("dpapi" if key and SECRET_PATH.exists() else None)
    return {"configured": bool(key), "masked": f"{key[:3]}***{key[-3:]}" if key and len(key) >= 8 else None, "storage": storage}


def hithink_status() -> dict[str, object]:
    key = load_hithink_key()
    storage = _hithink_storage_mode or ("dpapi" if key and HITHINK_SECRET_PATH.exists() else None)
    return {"configured": bool(key), "masked": f"{key[:3]}***{key[-3:]}" if key and len(key) >= 8 else None, "storage": storage}
