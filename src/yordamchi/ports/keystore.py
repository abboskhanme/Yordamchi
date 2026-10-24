"""Keystore: AES-256-GCM sealing with the master key, which never leaves the adapter (SPEC §4.5; STACK §2.2).

The production adapter (`adapters/keystore/file.py`, T08) reads `~/.config/yordamchi/master.key` (0600). The vault
stores only `Sealed` parts. The methods are synchronous: sealing a short secret takes microseconds.
"""

from dataclasses import dataclass, field
from typing import Final, Protocol

NONCE_BYTES: Final = 12  # a random 96-bit nonce per write
FINGERPRINT_HEX: Final = 8  # key_fp = the first 8 hex chars of sha256(master key)


@dataclass(frozen=True, slots=True)
class Sealed:
    ciphertext: bytes = field(repr=False)
    nonce: bytes
    key_fp: str


class Keystore(Protocol):
    def fingerprint(self) -> str:
        """`key_fp` of the current master key."""
        ...

    def seal(self, plaintext: bytes, *, aad: bytes) -> Sealed:
        """Encrypt with a fresh nonce. `aad` binds the row (`f"{name}|{kind}|{id}"`)."""
        ...

    def unseal(self, sealed: Sealed, *, aad: bytes, name: str) -> bytes:
        """Decrypt. A wrong key, nonce, AAD or a tampered ciphertext raises `VaultDecryptFailed(name)`."""
        ...
