"""RGNR8 public-API keys — a scoped, tenant-bound key model.

SUPERSEDED (H1-7): the web app's own `rgnr8_web.apikeys` is the single canonical
API-key system now — it is the one actually wired into request auth, and it gained
per-key scopes (role-perms ∩ scopes, enforced at the route boundary). This package
is retained only as a reference design / for any future standalone public-API
service; do NOT wire it alongside the web keys, or authorization drifts (the exact
problem H1-7 removed). New work should extend `rgnr8_web.apikeys`.

Scoped, tenant-bound keys with the secret hashed at rest and verified in constant
time; an auth layer that turns a Bearer header into a tenant + scope decision.
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
