import base64
import hashlib
import os
from pathlib import Path

import pytest

from tests.factories import write_master_key
from tests.fakes.keystore import InMemoryKeystore
from tests.fakes.security import FakeSecurity
from yordamchi.adapters.keystore.file import FileKeystore
from yordamchi.adapters.keystore.keychain import KeychainKeystore
from yordamchi.core import keychain
from yordamchi.core.errors import VaultDecryptFailed, VaultKeyInsecure, VaultKeyMissing
from yordamchi.core.paths import BotName
from yordamchi.ports.keystore import FINGERPRINT_HEX, NONCE_BYTES, Keystore, Sealed

AAD = b"google_api_key|google_api_key|7"


class KeystoreContract:
    @pytest.fixture
    def keystore(self) -> Keystore:
        raise NotImplementedError

    def test_roundtrip(self, keystore: Keystore) -> None:
        sealed = keystore.seal(b"secret value", aad=AAD)
        assert keystore.unseal(sealed, aad=AAD, name="k") == b"secret value"

    def test_ciphertext_never_holds_the_plaintext(self, keystore: Keystore) -> None:
        sealed = keystore.seal(b"plaintext-marker", aad=AAD)
        assert b"plaintext-marker" not in sealed.ciphertext

    def test_every_write_gets_a_fresh_96_bit_nonce(self, keystore: Keystore) -> None:
        first, second = keystore.seal(b"x", aad=AAD), keystore.seal(b"x", aad=AAD)
        assert len(first.nonce) == NONCE_BYTES
        assert first.nonce != second.nonce

    def test_fingerprint_is_8_hex_and_stamped_on_each_seal(self, keystore: Keystore) -> None:
        fingerprint = keystore.fingerprint()
        assert len(fingerprint) == FINGERPRINT_HEX
        int(fingerprint, 16)
        assert keystore.seal(b"x", aad=AAD).key_fp == fingerprint

    def test_a_different_row_binding_does_not_open(self, keystore: Keystore) -> None:
        sealed = keystore.seal(b"x", aad=AAD)
        with pytest.raises(VaultDecryptFailed):
            keystore.unseal(sealed, aad=b"other|kind|8", name="k")

    def test_a_tampered_ciphertext_does_not_open(self, keystore: Keystore) -> None:
        sealed = keystore.seal(b"x", aad=AAD)
        flipped = bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]
        with pytest.raises(VaultDecryptFailed):
            keystore.unseal(Sealed(flipped, sealed.nonce, sealed.key_fp), aad=AAD, name="k")


class TestInMemoryKeystore(KeystoreContract):
    @pytest.fixture
    def keystore(self) -> Keystore:
        return InMemoryKeystore()

    def test_another_key_does_not_open(self) -> None:
        sealed = InMemoryKeystore().seal(b"x", aad=AAD)
        with pytest.raises(VaultDecryptFailed):
            InMemoryKeystore().unseal(sealed, aad=AAD, name="k")


class TestFileKeystore(KeystoreContract):
    """The production adapter on a real 0600 key file in `tmp_path` (offline: no `live` mark needed)."""

    @pytest.fixture
    def key_path(self, tmp_path: Path) -> Path:
        return tmp_path / "config" / "master.key"

    @pytest.fixture
    def keystore(self, key_path: Path) -> Keystore:
        write_master_key(key_path)
        return FileKeystore(key_path)

    def test_the_fingerprint_is_the_sha256_of_the_raw_key(self, key_path: Path) -> None:
        raw = write_master_key(key_path)
        assert FileKeystore(key_path).fingerprint() == hashlib.sha256(raw).hexdigest()[:FINGERPRINT_HEX]

    def test_the_same_key_file_opens_what_another_instance_sealed(self, key_path: Path) -> None:
        write_master_key(key_path)
        sealed = FileKeystore(key_path).seal(b"x", aad=AAD)  # e.g. the personal bot sealed it
        assert FileKeystore(key_path).unseal(sealed, aad=AAD, name="k") == b"x"

    def test_a_sealed_value_from_another_key_does_not_open(self, key_path: Path) -> None:
        write_master_key(key_path)
        sealed = InMemoryKeystore().seal(b"x", aad=AAD)
        with pytest.raises(VaultDecryptFailed):
            FileKeystore(key_path).unseal(sealed, aad=AAD, name="k")

    def test_a_nonce_of_the_wrong_length_does_not_open(self, keystore: Keystore) -> None:
        sealed = keystore.seal(b"x", aad=AAD)
        with pytest.raises(VaultDecryptFailed):
            keystore.unseal(Sealed(sealed.ciphertext, b"short", sealed.key_fp), aad=AAD, name="k")

    def test_a_missing_key_file_raises_vault_key_missing_and_is_not_created(self, key_path: Path) -> None:
        keystore = FileKeystore(key_path)
        with pytest.raises(VaultKeyMissing):
            keystore.seal(b"x", aad=AAD)
        assert not key_path.exists()  # the installer creates it, never a bot

    @pytest.mark.parametrize(
        "content",
        [b"", b"not base64 at all!", base64.b64encode(b"too short") + b"\n", base64.b64encode(bytes(33))],
        ids=["empty", "not_base64", "short", "long"],
    )
    def test_a_malformed_key_file_raises_vault_key_missing(self, key_path: Path, content: bytes) -> None:
        key_path.parent.mkdir(parents=True)
        key_path.write_bytes(content)
        key_path.chmod(0o600)
        with pytest.raises(VaultKeyMissing):
            FileKeystore(key_path).fingerprint()

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660], ids=oct)
    def test_a_key_others_can_read_is_refused(self, key_path: Path, mode: int) -> None:
        write_master_key(key_path, mode=mode)
        with pytest.raises(VaultKeyInsecure) as raised:
            FileKeystore(key_path).fingerprint()
        assert raised.value.params == {"mode": f"{mode:04o}"}

    def test_a_fixed_key_file_works_without_a_restart(self, key_path: Path) -> None:
        keystore = FileKeystore(key_path)
        with pytest.raises(VaultKeyMissing):
            keystore.fingerprint()
        write_master_key(key_path)
        assert len(keystore.fingerprint()) == FINGERPRINT_HEX

    def test_the_key_is_read_once(self, key_path: Path) -> None:
        write_master_key(key_path)
        keystore = FileKeystore(key_path)
        sealed = keystore.seal(b"x", aad=AAD)
        key_path.unlink()
        assert keystore.unseal(sealed, aad=AAD, name="k") == b"x"


# --- KeychainKeystore: the production adapter, on a fake `/usr/bin/security` -------------------------------------
def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestKeychainKeystore(KeystoreContract):
    """The key is the Keychain item `uz.yordamchi/vault-master-key`; nothing here touches the real Keychain."""

    @pytest.fixture
    def raw(self) -> bytes:
        return os.urandom(32)

    @pytest.fixture
    def security(self, raw: bytes) -> FakeSecurity:
        security = FakeSecurity()
        security.put(keychain.MASTER_KEY, b64(raw))
        return security

    @pytest.fixture
    def legacy(self, tmp_path: Path) -> Path:
        return tmp_path / "config" / "master.key"

    @pytest.fixture
    def keystore(self, security: FakeSecurity, legacy: Path) -> Keystore:
        return KeychainKeystore(bot=BotName.DEV, legacy_key_file=legacy, runner=security)

    def test_the_fingerprint_is_the_sha256_of_the_key_in_the_keychain(self, keystore: Keystore, raw: bytes) -> None:
        assert keystore.fingerprint() == hashlib.sha256(raw).hexdigest()[:FINGERPRINT_HEX]

    def test_both_bots_open_what_the_other_sealed(self, security: FakeSecurity, legacy: Path) -> None:
        sealed = KeychainKeystore(bot=BotName.PERSONAL, legacy_key_file=legacy, runner=security).seal(b"x", aad=AAD)
        dev = KeychainKeystore(bot=BotName.DEV, legacy_key_file=legacy, runner=security)
        assert dev.unseal(sealed, aad=AAD, name="k") == b"x"

    def test_it_seals_exactly_like_the_file_keystore(self, keystore: Keystore, raw: bytes, tmp_path: Path) -> None:
        key_file = tmp_path / "other" / "master.key"
        write_master_key(key_file, raw)
        sealed = FileKeystore(key_file).seal(b"same aes-gcm", aad=AAD)
        assert keystore.unseal(sealed, aad=AAD, name="k") == b"same aes-gcm"
        assert FileKeystore(key_file).unseal(keystore.seal(b"back", aad=AAD), aad=AAD, name="k") == b"back"

    def test_a_nonce_of_the_wrong_length_does_not_open(self, keystore: Keystore) -> None:
        sealed = keystore.seal(b"x", aad=AAD)
        with pytest.raises(VaultDecryptFailed):
            keystore.unseal(Sealed(sealed.ciphertext, b"short", sealed.key_fp), aad=AAD, name="k")

    def test_the_key_is_read_once_with_the_item_name_only_on_argv(
        self, keystore: Keystore, security: FakeSecurity, raw: bytes
    ) -> None:
        for _ in range(3):
            keystore.unseal(keystore.seal(b"x", aad=AAD), aad=AAD, name="k")
        assert security.calls == [tuple(keychain.find_argv(keychain.MASTER_KEY, reveal=True))]
        assert all(b64(raw) not in part for call in security.calls for part in call)

    def test_a_missing_item_degrades_the_vault_and_no_key_is_created(self, legacy: Path) -> None:
        security = FakeSecurity()
        keystore = KeychainKeystore(bot=BotName.DEV, legacy_key_file=legacy, runner=security)
        with pytest.raises(VaultKeyMissing):
            keystore.seal(b"x", aad=AAD)
        assert security.items == {}  # the installer creates the key, never a bot
        assert security.stdins == []

    def test_a_malformed_item_degrades_the_vault(self, legacy: Path) -> None:
        security = FakeSecurity()
        security.put(keychain.MASTER_KEY, b64(b"too short"))
        with pytest.raises(VaultKeyMissing):
            KeychainKeystore(bot=BotName.DEV, legacy_key_file=legacy, runner=security).fingerprint()

    def test_a_locked_keychain_is_retried_only_after_the_interval(
        self, security: FakeSecurity, legacy: Path, raw: bytes
    ) -> None:
        clock = FakeMonotonic()
        security.locked = True
        keystore = KeychainKeystore(
            bot=BotName.DEV, legacy_key_file=legacy, runner=security, retry_after_s=30.0, monotonic=clock
        )
        for _ in range(3):
            with pytest.raises(VaultKeyMissing):
                keystore.fingerprint()
        assert len(security.calls) == 1  # a locked Keychain never stalls every call
        security.locked = False
        clock.now += 31
        assert keystore.fingerprint() == hashlib.sha256(raw).hexdigest()[:FINGERPRINT_HEX]  # no restart needed
        assert len(security.calls) == 2

    def test_a_hung_security_is_a_degraded_vault(self, security: FakeSecurity, legacy: Path) -> None:
        security.hang = True
        with pytest.raises(VaultKeyMissing):
            KeychainKeystore(bot=BotName.DEV, legacy_key_file=legacy, runner=security).fingerprint()

    # --- migration of the legacy master.key file ---
    def test_a_legacy_key_file_is_imported_verified_and_deleted(self, legacy: Path) -> None:
        raw = write_master_key(legacy)
        before = FileKeystore(legacy).seal(b"sealed before the move", aad=AAD)
        security = FakeSecurity()
        keystore = KeychainKeystore(bot=BotName.DEV, legacy_key_file=legacy, runner=security)
        assert keystore.unseal(before, aad=AAD, name="k") == b"sealed before the move"
        assert security.get(keychain.MASTER_KEY) == b64(raw)
        assert security.trusted[(keychain.MASTER_KEY.service, keychain.MASTER_KEY.account)] == "/usr/bin/security"
        assert not legacy.exists()
        verbs = [call[1] for call in security.calls]
        assert verbs == ["find-generic-password", "-i", "find-generic-password"]  # read, write, read back
        assert all(b64(raw) not in part for call in security.calls for part in call)  # stdin only

    def test_a_leftover_file_with_the_same_key_is_deleted_without_a_write(
        self, security: FakeSecurity, legacy: Path, raw: bytes
    ) -> None:
        write_master_key(legacy, raw)
        KeychainKeystore(bot=BotName.PERSONAL, legacy_key_file=legacy, runner=security).fingerprint()
        assert not legacy.exists()
        assert security.stdins == []

    def test_a_different_key_in_the_keychain_is_a_conflict_and_nothing_is_lost(
        self, security: FakeSecurity, legacy: Path, raw: bytes
    ) -> None:
        file_raw = write_master_key(legacy)
        with pytest.raises(VaultKeyMissing):
            KeychainKeystore(bot=BotName.DEV, legacy_key_file=legacy, runner=security).fingerprint()
        assert base64.b64decode(legacy.read_bytes()) == file_raw  # the file is kept
        assert security.get(keychain.MASTER_KEY) == b64(raw)  # and so is the item
        assert security.stdins == []

    def test_no_silent_fallback_to_the_file_when_the_keychain_is_locked(
        self, security: FakeSecurity, legacy: Path
    ) -> None:
        write_master_key(legacy)
        security.locked = True
        with pytest.raises(VaultKeyMissing):
            KeychainKeystore(bot=BotName.DEV, legacy_key_file=legacy, runner=security).seal(b"x", aad=AAD)
        assert legacy.exists()

    def test_a_failed_import_keeps_the_file(self, legacy: Path) -> None:
        write_master_key(legacy)
        security = FakeSecurity(fail_writes=True)
        with pytest.raises(VaultKeyMissing):
            KeychainKeystore(bot=BotName.DEV, legacy_key_file=legacy, runner=security).fingerprint()
        assert legacy.exists()
        assert security.items == {}

    def test_an_open_or_malformed_legacy_file_is_never_imported(self, legacy: Path) -> None:
        write_master_key(legacy, mode=0o644)
        security = FakeSecurity()
        with pytest.raises(VaultKeyInsecure):
            KeychainKeystore(bot=BotName.DEV, legacy_key_file=legacy, runner=security).fingerprint()
        legacy.write_text("garbage\n")
        legacy.chmod(0o600)
        with pytest.raises(VaultKeyMissing):
            KeychainKeystore(bot=BotName.DEV, legacy_key_file=legacy, runner=security).fingerprint()
        assert legacy.exists()
        assert security.items == {}

    def test_the_repr_holds_no_key(self, keystore: Keystore, raw: bytes) -> None:
        keystore.fingerprint()
        assert b64(raw) not in repr(keystore)
        assert repr(keystore) == "KeychainKeystore(bot=dev)"
