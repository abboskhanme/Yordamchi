"""AES-256-GCM with a 32-byte master key (SPEC §4.5): the sealing every keystore adapter shares.

`KeychainKeystore` (production) and `FileKeystore` (tests, legacy file import) differ only in where the key comes from;
both seal and open through `MasterKey`, so a value sealed by one opens with the other given the same key.

- A key is standard base64 of exactly 32 bytes (`decode_key`); `key_fp` is the first 8 hex chars of its sha256.
- `seal`: a fresh random 96-bit nonce per write; `aad` binds the row.
- `unseal`: a different key fingerprint, nonce, AAD or a tampered ciphertext raises `VaultDecryptFailed(name)`.
"""

import base64
import hashlib
import os
from dataclasses import dataclass, field
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from yordamchi.core.errors import VaultDecryptFailed
from yordamchi.ports.keystore import FINGERPRINT_HEX, NONCE_BYTES, Sealed

KEY_BYTES: Final = 32  # AES-256


def decode_key(encoded: str | bytes) -> bytes:
    """The raw key from its base64 text (surrounding whitespace allowed). Raises `ValueError` unless it is exactly 32
    bytes of standard base64."""
    raw = base64.b64decode(encoded.strip(), validate=True)  # binascii.Error is a ValueError
    if len(raw) != KEY_BYTES:
        msg = "a master key is 32 bytes"
        raise ValueError(msg)
    return raw


def encode_key(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@dataclass(frozen=True, slots=True)
class MasterKey:
    aead: AESGCM = field(repr=False)
    fingerprint: str

    @classmethod
    def from_raw(cls, raw: bytes) -> "MasterKey":
        return cls(aead=AESGCM(raw), fingerprint=hashlib.sha256(raw).hexdigest()[:FINGERPRINT_HEX])

    def seal(self, plaintext: bytes, *, aad: bytes) -> Sealed:
        nonce = os.urandom(NONCE_BYTES)
        return Sealed(ciphertext=self.aead.encrypt(nonce, plaintext, aad), nonce=nonce, key_fp=self.fingerprint)

    def unseal(self, sealed: Sealed, *, aad: bytes, name: str) -> bytes:
        if sealed.key_fp != self.fingerprint:
            raise VaultDecryptFailed(name=name)
        try:
            return self.aead.decrypt(sealed.nonce, sealed.ciphertext, aad)
        except (InvalidTag, ValueError):
            # ValueError: a nonce of the wrong length (a corrupted row).
            raise VaultDecryptFailed(name=name) from None
