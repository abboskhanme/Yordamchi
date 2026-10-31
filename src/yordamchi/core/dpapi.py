"""Windows DPAPI through `ctypes`: what `core.keychain.DpapiStore` seals the bot token and the master key with.

`CryptProtectData` / `CryptUnprotectData` in user scope: only the same Windows user on the same machine can open a
blob, and a plain file copy to another account or machine is useless. `CRYPTPROTECT_UI_FORBIDDEN` keeps a terminal
run from ever showing a prompt. The entropy is the item's name, so a blob moved to another item's file does not open.
The output buffer belongs to Windows and is released with `LocalFree`.

- **Seam**: `DpapiLib` holds the three Windows calls and `GetLastError`. `system()` builds it from `crypt32` and
  `kernel32` and exists only on Windows; tests pass a fake lib that does the same `_Blob` marshalling, so the
  pointer handling is exercised on macOS too. No pywin32.
- **Failures** raise `DpapiError` (an `OSError`) with the Windows error code; nothing here sees or logs a value.
"""

import ctypes
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

CRYPTPROTECT_UI_FORBIDDEN: Final = 0x1


class DpapiError(OSError):
    """A DPAPI call failed (or DPAPI does not exist here). `errno` is the Windows error code when there is one."""


class _Blob(ctypes.Structure):
    """`DATA_BLOB`: a DWORD length and a byte pointer."""

    _fields_ = (("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char)))


@dataclass(frozen=True, slots=True)
class DpapiLib:
    """The Windows calls, with the C signatures:
    `protect(p_in, description, p_entropy, reserved, prompt, flags, p_out) -> BOOL` (CryptProtectData),
    `unprotect(p_in, p_description, p_entropy, reserved, prompt, flags, p_out) -> BOOL` (CryptUnprotectData),
    `free(pointer)` (LocalFree) and `last_error() -> int` (GetLastError)."""

    protect: Callable[..., int]
    unprotect: Callable[..., int]
    free: Callable[..., object]
    last_error: Callable[[], int]


def system() -> DpapiLib:
    """The real DPAPI. Raises `DpapiError` anywhere but Windows."""
    if sys.platform == "win32":
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob = ctypes.POINTER(_Blob)
        protect = crypt32.CryptProtectData
        protect.argtypes = (blob, ctypes.c_wchar_p, blob, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, blob)
        protect.restype = ctypes.c_int
        unprotect = crypt32.CryptUnprotectData
        unprotect.argtypes = (
            blob,
            ctypes.POINTER(ctypes.c_wchar_p),
            blob,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            blob,
        )
        unprotect.restype = ctypes.c_int
        free = kernel32.LocalFree
        free.argtypes = (ctypes.c_void_p,)
        free.restype = ctypes.c_void_p
        return DpapiLib(protect=protect, unprotect=unprotect, free=free, last_error=ctypes.get_last_error)
    msg = "DPAPI exists only on Windows"
    raise DpapiError(msg)


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array[ctypes.c_char]]:
    """A `DATA_BLOB` over a copy of `data`; the buffer is returned too, so it outlives the call."""
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _call(lib: DpapiLib, function: Callable[..., int], data: bytes, entropy: bytes) -> bytes:
    source, source_buffer = _blob(data)
    salt, salt_buffer = _blob(entropy)
    out = _Blob()
    ok = function(
        ctypes.pointer(source),
        None,  # no description in, none wanted out
        ctypes.pointer(salt),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.pointer(out),
    )
    del source_buffer, salt_buffer  # alive until here: the call read them
    if not ok:
        code = lib.last_error()
        raise DpapiError(code, "DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        lib.free(out.pbData)


def protect(data: bytes, *, entropy: bytes, lib: DpapiLib | None = None) -> bytes:
    """Seal `data` for this Windows user (CryptProtectData). Raises `DpapiError`."""
    api = lib or system()
    return _call(api, api.protect, data, entropy)


def unprotect(blob: bytes, *, entropy: bytes, lib: DpapiLib | None = None) -> bytes:
    """Open a blob `protect` made with the same `entropy` (CryptUnprotectData). Raises `DpapiError`."""
    api = lib or system()
    return _call(api, api.unprotect, blob, entropy)
