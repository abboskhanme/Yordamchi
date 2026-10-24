import pytest

from tests.fakes.keystore import InMemoryKeystore
from yordamchi.core.errors import VaultDecryptFailed
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
