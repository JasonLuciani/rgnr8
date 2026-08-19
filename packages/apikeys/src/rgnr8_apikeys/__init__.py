"""RGNR8 public-API keys — the authentication foundation for a public API.

Scoped, tenant-bound keys with the secret hashed at rest and verified in constant
time; an auth layer that turns a Bearer header into a tenant + scope decision.
This is what a public REST API and a partner/app marketplace are built on, paired
with the webhook outbox that already delivers events outward.
"""

from __future__ import annotations

from .auth import AuthContext, AuthError, authenticate, require
from .keys import (
    SCOPES,
    ApiKeyError,
    ApiKeyRecord,
    ApiKeyService,
    ApiKeyStore,
    InMemoryApiKeyStore,
    IssuedKey,
    hash_secret,
)

__all__ = [
    "SCOPES",
    "ApiKeyError",
    "ApiKeyRecord",
    "ApiKeyService",
    "ApiKeyStore",
    "InMemoryApiKeyStore",
    "IssuedKey",
    "hash_secret",
    "AuthContext",
    "AuthError",
    "authenticate",
    "require",
]
