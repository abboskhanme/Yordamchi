"""`FileKeystore`: AES-256-GCM with the master key from a 0600 file (SPEC §4.5; STACK §2.2; NFR-SEC-02).

The file is `~/.config/yordamchi/master.key` (`Paths.master_key`; `YORDAMCHI_CONFIG_DIR` moves it): 32 random bytes,
standard base64, an optional trailing newline. The installer creates it and never overwrites it; this adapter only
reads it, never creates or repairs it. The key is read on first use and kept in memory, so a missing key degrades
the vault instead of stopping the bot:

- absent, unreadable, or not 32 base64 bytes → `VaultKeyMissing`;
- readable by the group or others → `VaultKeyInsecure` (the key is refused, as ssh refuses an open private key);
- a ciphertext that does not open (other key, other row binding, tampering) → `VaultDecryptFailed(name)`.

The only module that imports `cryptography` (C5). The key never leaves this object.
"""

import base64
import binascii
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from yordamchi.core.errors import VaultDecryptFailed, VaultKeyInsecure, VaultKeyMissing
from yordamchi.ports.keystore import FINGERPRINT_HEX, NONCE_BYTES, Sealed

KEY_BYTES: Final = 32  # AES-256
_OPEN_TO_OTHERS: Final = stat.S_IRWXG | stat.S_IRWXO


@dataclass(frozen=True, slots=True)
class _Key:
    aead: AESGCM
    fingerprint: str


def _read_key(path: Path) -> _Key:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        raise VaultKeyMissing from None
    if mode & _OPEN_TO_OTHERS:
        raise VaultKeyInsecure(mode=f"{mode:04o}")
    try:
        raw = base64.b64decode(path.read_bytes().strip(), validate=True)
    except (OSError, binascii.Error):
        raise VaultKeyMissing from None
    if len(raw) != KEY_BYTES:
        raise VaultKeyMissing
    return _Key(aead=AESGCM(raw), fingerprint=hashlib.sha256(raw).hexdigest()[:FINGERPRINT_HEX])


class FileKeystore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._key: _Key | None = None

    def _loaded(self) -> _Key:
        # Read once; a failed read is retried on the next call, so fixing the file needs no restart.
        if self._key is None:
            self._key = _read_key(self._path)
        return self._key

    def fingerprint(self) -> str:
        return self._loaded().fingerprint

    def seal(self, plaintext: bytes, *, aad: bytes) -> Sealed:
        key = self._loaded()
        nonce = os.urandom(NONCE_BYTES)
        return Sealed(ciphertext=key.aead.encrypt(nonce, plaintext, aad), nonce=nonce, key_fp=key.fingerprint)

    def unseal(self, sealed: Sealed, *, aad: bytes, name: str) -> bytes:
        key = self._loaded()
        if sealed.key_fp != key.fingerprint:
            raise VaultDecryptFailed(name=name)
        try:
            return key.aead.decrypt(sealed.nonce, sealed.ciphertext, aad)
        except (InvalidTag, ValueError):
            # ValueError: a nonce of the wrong length (a corrupted row).
            raise VaultDecryptFailed(name=name) from None
