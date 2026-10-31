"""`FakeDpapi`: Windows DPAPI on any platform, through the same `DATA_BLOB` marshalling `core.dpapi` does.

`lib()` is a `core.dpapi.DpapiLib` whose `protect` / `unprotect` read the input blob through its pointer and write
the output blob back through `p_out.contents`, as `crypt32` does, and whose `free` records each released buffer (as
`LocalFree`). The "seal" is reversible and bound to the entropy: unprotecting with other entropy, or a blob this fake
did not make, fails with `ERROR_INVALID_DATA`, as the real call does. `fail` makes every call fail with that code.
"""

import ctypes
import hashlib
from dataclasses import dataclass, field
from typing import Any, Final, cast

from yordamchi.core import dpapi

ERROR_INVALID_DATA: Final = 13
MAGIC: Final = b"FAKEDPAPI1"


def _read(pointer: object) -> bytes:
    blob = cast("Any", pointer).contents
    return ctypes.string_at(blob.pbData, blob.cbData)


def _header(entropy: bytes) -> bytes:
    return MAGIC + hashlib.sha256(entropy).digest()[:8]


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(byte ^ key[index % len(key)] for index, byte in enumerate(data)) if key else data


@dataclass
class FakeDpapi:
    fail: int | None = None
    calls: list[tuple[str, int]] = field(default_factory=list)  # (function, flags)
    allocated: list[int] = field(default_factory=list)
    freed: list[int] = field(default_factory=list)
    _buffers: dict[int, ctypes.Array[ctypes.c_char]] = field(default_factory=dict)
    _error: int = 0

    def lib(self) -> dpapi.DpapiLib:
        return dpapi.DpapiLib(protect=self.protect, unprotect=self.unprotect, free=self.free, last_error=self.error)

    def error(self) -> int:
        return self._error

    def _out(self, pointer: object, data: bytes) -> None:
        buffer = ctypes.create_string_buffer(data, len(data))
        address = ctypes.addressof(buffer)
        self._buffers[address] = buffer
        self.allocated.append(address)
        blob = cast("Any", pointer).contents
        blob.cbData = len(data)
        blob.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))

    def _refuse(self) -> int:
        self._error = self.fail or ERROR_INVALID_DATA
        return 0

    def protect(
        self,
        p_in: object,
        description: object,
        p_entropy: object,
        reserved: object,
        prompt: object,
        flags: int,
        p_out: object,
    ) -> int:
        del description, reserved, prompt
        self.calls.append(("protect", flags))
        if self.fail is not None:
            return self._refuse()
        entropy = _read(p_entropy)
        self._out(p_out, _header(entropy) + _xor(_read(p_in), entropy))
        return 1

    def unprotect(
        self,
        p_in: object,
        description: object,
        p_entropy: object,
        reserved: object,
        prompt: object,
        flags: int,
        p_out: object,
    ) -> int:
        del description, reserved, prompt
        self.calls.append(("unprotect", flags))
        if self.fail is not None:
            return self._refuse()
        blob, entropy = _read(p_in), _read(p_entropy)
        header = _header(entropy)
        if not blob.startswith(header):
            return self._refuse()
        self._out(p_out, _xor(blob[len(header) :], entropy))
        return 1

    def free(self, pointer: object) -> None:
        address = ctypes.cast(cast("Any", pointer), ctypes.c_void_p).value or 0
        self.freed.append(address)
        self._buffers.pop(address, None)
