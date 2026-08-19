"""Scoped, tenant-bound API keys — the front door for a public API.

The security posture mirrors the rest of RGNR8: the *secret* is shown once, at
creation, and never stored — only a salted SHA-256 hash is kept, and verification
is a constant-time compare, so a leaked database yields no usable keys. Each key
carries a tenant and a set of scopes; a request is authorised only for the scope
its key holds. Randomness and time are injected (a `rng` and a `clock`) so tests
are deterministic; production binds `secrets.token_hex` and the wall clock.

Key format: ``rgnr8_<env>_<prefix>_<secret>``. The prefix is stored in the clear
and indexes the lookup; the secret half is never persisted.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Callable, Protocol

# A small, explicit catalogue — a key is granted a subset of these.
SCOPES: tuple[str, ...] = (
    "ledger:read",
    "invoices:read",
    "invoices:write",
    "bills:read",
    "bills:write",
    "reports:read",
    "webhooks:manage",
)


class ApiKeyError(Exception):
    pass


@dataclass(frozen=True)
class ApiKeyRecord:
    id: str
    tenant: str
    prefix: str
    secret_hash: str  # hex sha256(salt + secret)
    salt: str
    scopes: frozenset[str]
    label: str
    active: bool
    created_at: float

    def allows(self, scope: str) -> bool:
        return self.active and scope in self.scopes

    def redacted(self) -> dict[str, object]:
        """Safe to show in a UI — never includes the secret or its hash."""
        return {
            "id": self.id,
            "tenant": self.tenant,
            "prefix": self.prefix,
            "scopes": sorted(self.scopes),
            "label": self.label,
            "active": self.active,
            "created_at": self.created_at,
        }


def hash_secret(secret: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{secret}".encode()).hexdigest()


class ApiKeyStore(Protocol):
    def put(self, record: ApiKeyRecord) -> None: ...
    def get_by_prefix(self, prefix: str) -> ApiKeyRecord | None: ...
    def list_for_tenant(self, tenant: str) -> list[ApiKeyRecord]: ...


class InMemoryApiKeyStore:
    def __init__(self) -> None:
        self._by_prefix: dict[str, ApiKeyRecord] = {}

    def put(self, record: ApiKeyRecord) -> None:
        self._by_prefix[record.prefix] = record

    def get_by_prefix(self, prefix: str) -> ApiKeyRecord | None:
        return self._by_prefix.get(prefix)

    def list_for_tenant(self, tenant: str) -> list[ApiKeyRecord]:
        return sorted(
            (r for r in self._by_prefix.values() if r.tenant == tenant),
            key=lambda r: r.created_at,
        )


@dataclass(frozen=True)
class IssuedKey:
    """The one and only time the full secret is available."""

    record: ApiKeyRecord
    full_key: str


class ApiKeyService:
    """Issue, verify, and revoke keys. Randomness and time are injected."""

    def __init__(
        self,
        store: ApiKeyStore,
        *,
        rng: Callable[[int], str],
        clock: Callable[[], float],
        env: str = "live",
    ) -> None:
        self._store = store
        self._rng = rng
        self._clock = clock
        self._env = env

    def issue(self, tenant: str, scopes: set[str], label: str) -> IssuedKey:
        unknown = scopes - set(SCOPES)
        if unknown:
            raise ApiKeyError(f"unknown scope(s): {', '.join(sorted(unknown))}")
        if not scopes:
            raise ApiKeyError("a key needs at least one scope")
        prefix = self._rng(8)
        secret = self._rng(32)
        salt = self._rng(16)
        record = ApiKeyRecord(
            id=self._rng(12),
            tenant=tenant,
            prefix=prefix,
            secret_hash=hash_secret(secret, salt),
            salt=salt,
            scopes=frozenset(scopes),
            label=label,
            active=True,
            created_at=self._clock(),
        )
        self._store.put(record)
        return IssuedKey(record=record, full_key=f"rgnr8_{self._env}_{prefix}_{secret}")

    def verify(self, presented: str) -> ApiKeyRecord | None:
        """Return the record iff the presented key is valid and active. Constant
        time against the stored hash; an unknown prefix still does the compare
        work against a dummy so timing does not reveal which prefixes exist."""
        parts = presented.split("_")
        if len(parts) != 4 or parts[0] != "rgnr8":
            return None
        prefix, secret = parts[2], parts[3]
        record = self._store.get_by_prefix(prefix)
        salt = record.salt if record else "x"
        expected = record.secret_hash if record else hash_secret("no", salt)
        candidate = hash_secret(secret, salt)
        ok = hmac.compare_digest(candidate, expected)
        if not ok or record is None or not record.active:
            return None
        return record

    def revoke(self, prefix: str) -> bool:
        record = self._store.get_by_prefix(prefix)
        if record is None:
            return False
        self._store.put(
            ApiKeyRecord(
                id=record.id, tenant=record.tenant, prefix=record.prefix,
                secret_hash=record.secret_hash, salt=record.salt, scopes=record.scopes,
                label=record.label, active=False, created_at=record.created_at,
            )
        )
        return True


__all__ = [
    "SCOPES",
    "ApiKeyError",
    "ApiKeyRecord",
    "IssuedKey",
    "ApiKeyStore",
    "InMemoryApiKeyStore",
    "ApiKeyService",
    "hash_secret",
]
