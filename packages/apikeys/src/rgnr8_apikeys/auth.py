"""The public-API authentication layer.

A public REST endpoint calls `authenticate(header)` to turn an `Authorization:
Bearer rgnr8_...` header into an `AuthContext` (which tenant, which scopes), then
`require(ctx, scope)` before doing anything. Every failure mode is a typed
`AuthError` with an HTTP-shaped status, so the surface built on top can answer
401/403 uniformly. This is the primitive a public API and a partner marketplace
sit on — the same webhook outbox already delivers events outward; this governs
who may call in.
"""

from __future__ import annotations

from dataclasses import dataclass

from .keys import ApiKeyRecord, ApiKeyService


class AuthError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class AuthContext:
    tenant: str
    scopes: frozenset[str]
    key_prefix: str

    def has(self, scope: str) -> bool:
        return scope in self.scopes


def authenticate(service: ApiKeyService, authorization_header: str | None) -> AuthContext:
    """401 unless the header carries a valid, active key."""
    if not authorization_header:
        raise AuthError(401, "missing Authorization header")
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError(401, "expected 'Authorization: Bearer <key>'")
    record: ApiKeyRecord | None = service.verify(parts[1].strip())
    if record is None:
        raise AuthError(401, "invalid or revoked API key")
    return AuthContext(tenant=record.tenant, scopes=record.scopes, key_prefix=record.prefix)


def require(ctx: AuthContext, scope: str) -> None:
    """403 unless the authenticated key holds `scope`."""
    if not ctx.has(scope):
        raise AuthError(403, f"this key is not authorised for '{scope}'")


__all__ = ["AuthError", "AuthContext", "authenticate", "require"]
