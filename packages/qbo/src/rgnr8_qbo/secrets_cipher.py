"""Encryption at rest for the secrets the platform stores — OAuth tokens above all.

A QBO connection holds a live access token and a long-lived refresh token. In the
database those are keys to a client's accounting system, so they must not sit in
plaintext. This module is the seam that encrypts them: a tiny ``SecretCipher``
protocol with two implementations.

- ``NullCipher`` is the default and preserves the old behaviour exactly — it does
  not encrypt. It is for local development and tests, where there is no key and
  no real client data. Crucially, it **fails closed**: if it is asked to decrypt
  something that was written encrypted, it raises rather than returning garbage,
  so a production database can never be read by a keyless process by accident.
- ``FernetCipher`` uses authenticated symmetric encryption (Fernet = AES-128-CBC
  + HMAC) with a key supplied by the deployment, never by a request. Ciphertext
  carries a version marker so plaintext written before the key existed still
  reads back transparently and is re-encrypted on the next save (a live, no
  -downtime migration).

The key comes from ``RGNR8_SECRET_KEY`` (a urlsafe-base64 32-byte Fernet key).
Generate one with ``Fernet.generate_key()`` and store it in your secret manager.
"""

from __future__ import annotations

from typing import Mapping, Protocol

_ENC_PREFIX = "enc:fernet:v1:"


class SecretCipher(Protocol):
    def encrypt(self, plaintext: str) -> str: ...
    def decrypt(self, stored: str) -> str: ...


class SecretCipherError(Exception):
    """Raised when stored ciphertext cannot be read (missing or wrong key)."""


class NullCipher:
    """No encryption — dev/test default. Refuses to *read* real ciphertext."""

    def encrypt(self, plaintext: str) -> str:
        return plaintext

    def decrypt(self, stored: str) -> str:
        if stored.startswith(_ENC_PREFIX):
            raise SecretCipherError(
                "found encrypted secret data but no encryption key is configured — "
                "set RGNR8_SECRET_KEY so the tokens can be decrypted"
            )
        return stored


class FernetCipher:
    """Authenticated symmetric encryption keyed from the deployment's secret."""

    def __init__(self, key: str) -> None:
        # Imported lazily so the module loads even where cryptography is absent
        # (a NullCipher deployment needs no crypto library at all).
        from cryptography.fernet import Fernet

        self._fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> str:
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return _ENC_PREFIX + token

    def decrypt(self, stored: str) -> str:
        if not stored.startswith(_ENC_PREFIX):
            # Plaintext written before the key existed — read it through and let
            # the next save re-write it encrypted. This is the online migration.
            return stored
        from cryptography.fernet import InvalidToken

        raw = stored[len(_ENC_PREFIX):]
        try:
            return self._fernet.decrypt(raw.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:  # wrong key, or tampered ciphertext
            raise SecretCipherError(
                "stored secret could not be decrypted — the encryption key does not "
                "match the one that wrote it"
            ) from exc


def is_encrypted(stored: str) -> bool:
    return stored.startswith(_ENC_PREFIX)


def cipher_from_env(env: Mapping[str, str]) -> SecretCipher:
    """A FernetCipher when ``RGNR8_SECRET_KEY`` is set, else a NullCipher."""
    key = str(env.get("RGNR8_SECRET_KEY", "")).strip()
    if key:
        return FernetCipher(key)
    return NullCipher()
