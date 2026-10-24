"""`FileKeystore`: AES-256-GCM with the master key from a 0600 file (SPEC §4.5; NFR-SEC-02).

**Not the production keystore** since 2026-10-24: the owner requires that a copied file is never usable, so the key
lives in the Keychain and `KeychainKeystore` (`keychain.py`) is wired in both bots. This module stays for two jobs:
- `read_key_file()` reads a legacy `~/.config/yordamchi/master.key` (`Paths.master_key`) that `KeychainKeystore`
  imports into the Keychain and then deletes;
- `FileKeystore` runs the offline keystore contract suite on a real key file in `tmp_path`.

The file is 32 random bytes, standard base64, an optional trailing newline. Nothing here creates or repairs it. The
key is read on first use and kept in memory:
- absent, unreadable, or not 32 base64 bytes → `VaultKeyMissing`;
- readable by the group or others → `VaultKeyInsecure` (the key is refused, as ssh refuses an open private key);
- a ciphertext that does not open (other key, other row binding, tampering) → `VaultDecryptFailed(name)`.
"""

import stat
from pathlib import Path
from typing import Final

from yordamchi.adapters.keystore.aead import MasterKey, decode_key
from yordamchi.core.errors import VaultKeyInsecure, VaultKeyMissing
from yordamchi.ports.keystore import Sealed

_OPEN_TO_OTHERS: Final = stat.S_IRWXG | stat.S_IRWXO


def read_key_file(path: Path) -> bytes:
    """The raw 32-byte key in a key file. Raises `VaultKeyMissing` or `VaultKeyInsecure`; never changes the file."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        raise VaultKeyMissing from None
    if mode & _OPEN_TO_OTHERS:
        raise VaultKeyInsecure(mode=f"{mode:04o}")
    try:
        return decode_key(path.read_bytes())
    except (OSError, ValueError):
        raise VaultKeyMissing from None


class FileKeystore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._key: MasterKey | None = None

    def _loaded(self) -> MasterKey:
        # Read once; a failed read is retried on the next call, so fixing the file needs no restart.
        if self._key is None:
            self._key = MasterKey.from_raw(read_key_file(self._path))
        return self._key

    def fingerprint(self) -> str:
        return self._loaded().fingerprint

    def seal(self, plaintext: bytes, *, aad: bytes) -> Sealed:
        return self._loaded().seal(plaintext, aad=aad)

    def unseal(self, sealed: Sealed, *, aad: bytes, name: str) -> bytes:
        return self._loaded().unseal(sealed, aad=aad, name=name)
