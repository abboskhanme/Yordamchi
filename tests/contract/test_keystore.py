import base64
import hashlib
from pathlib import Path

import pytest

from tests.factories import write_master_key
from tests.fakes.keystore import InMemoryKeystore
from yordamchi.adapters.keystore.file import FileKeystore
from yordamchi.core.errors import VaultDecryptFailed, VaultKeyInsecure, VaultKeyMissing
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
