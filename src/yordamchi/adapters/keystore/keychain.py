"""`KeychainKeystore`: AES-256-GCM with the master key in the macOS login Keychain (owner requirement 2026-10-24).

The production keystore of both bots: `KeychainKeystore(bot=config.bot, legacy_key_file=config.paths.master_key)`.
The key is the generic password `uz.yordamchi/vault-master-key` (`core.keychain.MASTER_KEY`): 32 random bytes,
standard base64, created by the installer, never by a bot. A copied DB, backup or data dir is useless without it.
Sealing is `aead.MasterKey`, shared with `FileKeystore`.

- The key is read on first use through `/usr/bin/security`, scoped to this bot (`core.keychain.Keychain`), and kept
  in memory. That first read is the only subprocess: call `fingerprint()` once at startup off the event loop
  (`asyncio.to_thread`), before the vault is published.
- **No fallback to a file.** A locked or unreadable Keychain, a missing or malformed item → `VaultKeyMissing`: the
  vault is degraded, the owner gets `S.ERR_VAULT_KEY_MISSING`, and `python -m yordamchi.check` names the cause. A
  failed read is retried at most every `retry_after_s`, so a locked Keychain cannot stall the loop on every call.
- **Migration.** While the legacy `~/.config/yordamchi/master.key` exists, it is imported: written into the Keychain
  (when the item is absent), read back and compared, and only then deleted. If the Keychain already holds a
  different key, nothing is written or deleted (`master_key_conflict` → `VaultKeyMissing`): which key opens the rows
  is the owner's call (docs/DEPLOY.md). A legacy file readable by others is refused first (`VaultKeyInsecure`).
"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from yordamchi.adapters.keystore.aead import MasterKey, decode_key, encode_key
from yordamchi.adapters.keystore.file import read_key_file
from yordamchi.core import keychain
from yordamchi.core.errors import KeychainItemMissing, KeychainUnavailable, VaultKeyInsecure, VaultKeyMissing
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.ports.keystore import Sealed

log = get_log(__name__)

RETRY_AFTER_S: Final = 30.0

type KeyUnavailable = VaultKeyMissing | VaultKeyInsecure


def _same_key(stored: str, raw: bytes) -> bool:
    try:
        return decode_key(stored) == raw
    except ValueError:
        return False


class KeychainKeystore:
    def __init__(
        self,
        *,
        bot: BotName,
        legacy_key_file: Path,
        runner: keychain.Runner | None = None,
        retry_after_s: float = RETRY_AFTER_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """`runner` replaces `/usr/bin/security` (tests only)."""
        self._keychain = keychain.Keychain(bot, runner=runner)
        self._legacy = legacy_key_file
        self._retry_after_s = retry_after_s
        self._monotonic = monotonic
        self._key: MasterKey | None = None
        self._failed: tuple[float, KeyUnavailable] | None = None

    def __repr__(self) -> str:
        return f"KeychainKeystore(bot={self._keychain.bot.value})"

    # --- the key -------------------------------------------------------------------------------------------------
    def _loaded(self) -> MasterKey:
        if self._key is not None:
            return self._key
        if self._failed is not None:
            at, error = self._failed
            if self._monotonic() - at < self._retry_after_s:
                raise error.with_traceback(None)
        try:
            self._key = self._load()
        except (VaultKeyMissing, VaultKeyInsecure) as error:
            self._failed = (self._monotonic(), error)
            raise
        self._failed = None
        return self._key

    def _load(self) -> MasterKey:
        stored = self._read_stored()
        if self._legacy.exists():
            stored = self._import_legacy(stored)
        if stored is None:
            log.warning("vault_key_unavailable", problem="missing")
            raise VaultKeyMissing
        try:
            raw = decode_key(stored)
        except ValueError:
            log.error("vault_key_unavailable", problem="malformed")
            raise VaultKeyMissing from None
        return MasterKey.from_raw(raw)

    def _read_stored(self) -> str | None:
        try:
            return self._keychain.read(keychain.MASTER_KEY)
        except KeychainItemMissing:
            return None
        except KeychainUnavailable:
            log.warning("vault_key_unavailable", problem="keychain_unavailable")
            raise VaultKeyMissing from None

    def _import_legacy(self, stored: str | None) -> str:
        """Move the legacy key file into the Keychain; delete the file only once the Keychain holds the same key."""
        raw = read_key_file(self._legacy)  # VaultKeyMissing / VaultKeyInsecure: the file is kept as it is
        if stored is None:
            try:
                self._keychain.write(keychain.MASTER_KEY, encode_key(raw))
                stored = self._keychain.read(keychain.MASTER_KEY)
            except (KeychainItemMissing, KeychainUnavailable):
                log.error("master_key_import_failed", problem="keychain_write")
                raise VaultKeyMissing from None
            if not _same_key(stored, raw):
                log.error("master_key_import_failed", problem="read_back_differs")
                raise VaultKeyMissing
        elif not _same_key(stored, raw):
            log.error("master_key_conflict")
            raise VaultKeyMissing
        self._legacy.unlink(missing_ok=True)
        log.info("master_key_imported")
        return stored

    # --- Keystore ------------------------------------------------------------------------------------------------
    def fingerprint(self) -> str:
        return self._loaded().fingerprint

    def seal(self, plaintext: bytes, *, aad: bytes) -> Sealed:
        return self._loaded().seal(plaintext, aad=aad)

    def unseal(self, sealed: Sealed, *, aad: bytes, name: str) -> bytes:
        return self._loaded().unseal(sealed, aad=aad, name=name)
