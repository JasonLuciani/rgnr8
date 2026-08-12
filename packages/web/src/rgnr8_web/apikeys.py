"""Partner API keys — programmatic access to a tenant, gated by entitlement.

A key is issued for a **(tenant, subject)** pair: the subject is a service
principal that carries a role in the `UserDirectory`, so an API request resolves
to a principal and then goes through the *exact same* RBAC as a browser request —
no privileged side-door. The plaintext key is shown once at issue time; we store
only a SHA-256 hash (constant-time compared on verify). Issuing keys is gated on
the account's `api_access` entitlement (enforced by the issuer, which has billing
in scope); verification here is entitlement-agnostic and fast.

Key format: ``rgk_<key_id>_<secret>`` — the id is a lookup handle, the secret is
the part we hash.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

_PREFIX = "rgk"


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApiKey:
    key_id: str
    secret_hash: str
    tenant_id: str
    subject: str        # the service principal (has a role in the directory)
    name: str = ""
    created_at: int = 0
    expires_at: int = 0  # epoch seconds; 0 means the key never expires
    revoked: bool = False


class ApiKeyStore(Protocol):
    def save(self, key: ApiKey) -> None: ...
    def get(self, key_id: str) -> ApiKey | None: ...
    def list_for_tenant(self, tenant_id: str) -> list[ApiKey]: ...


class InMemoryApiKeyStore:
    def __init__(self) -> None:
        self._by_id: dict[str, ApiKey] = {}

    def save(self, key: ApiKey) -> None:
        self._by_id[key.key_id] = key

    def get(self, key_id: str) -> ApiKey | None:
        return self._by_id.get(key_id)

    def list_for_tenant(self, tenant_id: str) -> list[ApiKey]:
        return [k for k in self._by_id.values() if k.tenant_id == tenant_id]


class ApiKeyService:
    """Issue / verify / revoke partner keys. `verify` returns the (tenant, subject)
    principal so the web app can run RBAC exactly as for a JWT."""

    def __init__(
        self,
        store: ApiKeyStore | None = None,
        *,
        clock: Callable[[], int] | None = None,
        secret_factory: Callable[[], str] | None = None,
        ttl_days: int = 0,
    ) -> None:
        self._store: ApiKeyStore = store if store is not None else InMemoryApiKeyStore()
        self._clock = clock if clock is not None else (lambda: int(time.time()))
        self._secret = secret_factory if secret_factory is not None else (lambda: secrets.token_urlsafe(32))
        self._ttl_days = ttl_days

    def issue(self, tenant_id: str, subject: str, name: str = "") -> tuple[ApiKey, str]:
        """Create a key; returns (record, plaintext). The plaintext is shown ONCE.
        With a non-zero ``ttl_days`` the key expires ``ttl_days`` after issue;
        the default (0) issues a non-expiring key."""
        key_id = secrets.token_hex(6)
        raw = self._secret()
        now = self._clock()
        expires_at = now + self._ttl_days * 86_400 if self._ttl_days > 0 else 0
        key = ApiKey(key_id=key_id, secret_hash=_hash(raw), tenant_id=tenant_id,
                     subject=subject, name=name, created_at=now, expires_at=expires_at)
        self._store.save(key)
        return key, f"{_PREFIX}_{key_id}_{raw}"

    def verify(self, presented: str) -> tuple[str, str] | None:
        """Verify a presented key → (tenant, subject), or None. Constant-time on the
        secret; rejects revoked and expired keys."""
        parts = presented.split("_", 2)
        if len(parts) != 3 or parts[0] != _PREFIX:
            return None
        _, key_id, raw = parts
        rec = self._store.get(key_id)
        if rec is None or rec.revoked:
            return None
        if not hmac.compare_digest(rec.secret_hash, _hash(raw)):
            return None
        # Expiry is checked only after a successful secret match, so it does not
        # perturb the constant-time compare above. 0 means never-expiring.
        if rec.expires_at != 0 and self._clock() >= rec.expires_at:
            return None
        return (rec.tenant_id, rec.subject)

    def revoke(self, key_id: str) -> bool:
        rec = self._store.get(key_id)
        if rec is None:
            return False
        self._store.save(dataclasses.replace(rec, revoked=True))
        return True

    def list_for_tenant(self, tenant_id: str) -> list[ApiKey]:
        return self._store.list_for_tenant(tenant_id)


class _DbApiCursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def close(self) -> None: ...


class _DbApiConnection(Protocol):
    def cursor(self) -> _DbApiCursor: ...
    def commit(self) -> None: ...


class SqlApiKeyStore:
    """Partner keys over any DB-API 2.0 connection (only the hash is stored)."""

    def __init__(self, connection: _DbApiConnection, *, table: str = "rgnr8_api_key",
                 placeholder: str = "?") -> None:
        self._conn = connection
        self._t = table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._t} "
                "(key_id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, tenant_id TEXT NOT NULL, "
                "subject TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL DEFAULT 0, "
                "expires_at INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0)")
        finally:
            cur.close()
        self._conn.commit()

    def save(self, key: ApiKey) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._t} (key_id, secret_hash, tenant_id, subject, name, created_at, expires_at, revoked) "
                f"VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}) "
                "ON CONFLICT (key_id) DO UPDATE SET revoked=excluded.revoked, name=excluded.name",
                (key.key_id, key.secret_hash, key.tenant_id, key.subject, key.name,
                 key.created_at, key.expires_at, 1 if key.revoked else 0))
        finally:
            cur.close()
        self._conn.commit()

    def _rows(self, sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            cur.close()

    def _to_key(self, r: tuple[object, ...]) -> ApiKey:
        return ApiKey(str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]),
                      int(str(r[5])), int(str(r[6])), bool(int(str(r[7]))))

    def get(self, key_id: str) -> ApiKey | None:
        rows = self._rows(
            f"SELECT key_id, secret_hash, tenant_id, subject, name, created_at, expires_at, revoked "
            f"FROM {self._t} WHERE key_id={self._ph}", (key_id,))
        return self._to_key(rows[0]) if rows else None

    def list_for_tenant(self, tenant_id: str) -> list[ApiKey]:
        rows = self._rows(
            f"SELECT key_id, secret_hash, tenant_id, subject, name, created_at, expires_at, revoked "
            f"FROM {self._t} WHERE tenant_id={self._ph} ORDER BY created_at", (tenant_id,))
        return [self._to_key(r) for r in rows]
