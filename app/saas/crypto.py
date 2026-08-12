"""Authenticated encryption for server-side provider credentials."""

from __future__ import annotations

from collections.abc import Iterable

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings


class TokenCipherError(ValueError):
    """A safe credential-encryption error that never includes key/token material."""


class TokenCipher:
    """Encrypt tokens with Fernet and optionally decrypt with rotation keys."""

    def __init__(self, key: str | bytes, previous_keys: Iterable[str | bytes] = ()) -> None:
        try:
            self._primary = Fernet(self._as_bytes(key))
            self._decryptors = (self._primary,) + tuple(
                Fernet(self._as_bytes(old_key)) for old_key in previous_keys
            )
        except (TypeError, ValueError):
            raise TokenCipherError("Token encryption configuration is invalid") from None

    @staticmethod
    def _as_bytes(value: str | bytes) -> bytes:
        if isinstance(value, str):
            value = value.encode("ascii")
        if not isinstance(value, bytes) or not value:
            raise ValueError("missing key")
        return value

    @classmethod
    def from_settings(
        cls, settings: Settings, previous_keys: Iterable[str | bytes] = ()
    ) -> "TokenCipher":
        return cls(settings.token_encryption_key, previous_keys)

    @staticmethod
    def generate_key() -> str:
        """Generate a new operator-managed key for setup tooling."""

        return Fernet.generate_key().decode("ascii")

    def encrypt(self, plaintext: str) -> str:
        if not isinstance(plaintext, str) or not plaintext:
            raise TokenCipherError("A non-empty credential is required")
        return self._primary.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if not isinstance(ciphertext, str) or not ciphertext:
            raise TokenCipherError("Encrypted credential is missing")
        encoded = ciphertext.encode("ascii", errors="ignore")
        for decryptor in self._decryptors:
            try:
                return decryptor.decrypt(encoded).decode("utf-8")
            except (InvalidToken, UnicodeDecodeError):
                continue
        raise TokenCipherError("Encrypted credential could not be decrypted")

    def decrypt_optional(self, ciphertext: str | None) -> str | None:
        return None if ciphertext is None else self.decrypt(ciphertext)

    def rotate(self, ciphertext: str) -> str:
        """Decrypt with any configured key and encrypt with the primary key."""

        return self.encrypt(self.decrypt(ciphertext))
