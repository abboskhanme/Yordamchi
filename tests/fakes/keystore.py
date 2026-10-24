"""`InMemoryKeystore`: real AES-256-GCM with a random in-memory key, so vault tests see real tamper detection."""

import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from yordamchi.core.errors import VaultDecryptFailed
from yordamchi.ports.keystore import FINGERPRINT_HEX, NONCE_BYTES, Keystore, Sealed


class InMemoryKeystore:
    def __init__(self, key: bytes | None = None) -> None:
        self._key = key if key is not None else AESGCM.generate_key(bit_length=256)
        self._aead = AESGCM(self._key)
        self.sealed_count = 0

    def fingerprint(self) -> str:
        return hashlib.sha256(self._key).hexdigest()[:FINGERPRINT_HEX]

    def seal(self, plaintext: bytes, *, aad: bytes) -> Sealed:
        nonce = os.urandom(NONCE_BYTES)
        self.sealed_count += 1
        return Sealed(ciphertext=self._aead.encrypt(nonce, plaintext, aad), nonce=nonce, key_fp=self.fingerprint())

    def unseal(self, sealed: Sealed, *, aad: bytes, name: str) -> bytes:
        if sealed.key_fp != self.fingerprint():
            raise VaultDecryptFailed(name=name)
        try:
            return self._aead.decrypt(sealed.nonce, sealed.ciphertext, aad)
        except InvalidTag:
            raise VaultDecryptFailed(name=name) from None


_: Keystore = InMemoryKeystore()
