"""The per-tenant QBO connection and its persistence.

A :class:`QboConnection` is what a tenant's QuickBooks link *is* after the OAuth
dance: the company ``realm_id``, the current access + refresh tokens, their
absolute expiries, and a status. It's the record the sync side reads to call the
Accounting API, and the record the connect UI reads to show "Connected / needs
reconnect".

Tokens are secrets. This store holds them as text; **encryption at rest is an
infrastructure concern** (KMS / pgcrypto on the column) — the same posture as the
other credential material in the platform. The store never logs a token.

The house persistence trio: a Protocol, an in-memory impl for tests/local, and a
DB-API SQL impl for production (mirroring the other RGNR8 stores).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Mapping, Protocol


class QboStatus(str, Enum):
    CONNECTED = "connected"          # access token live or refreshable
    EXPIRED = "expired"              # refresh token lapsed → owner must reconnect
    REVOKED = "revoked"             # access revoked (by us or at Intuit)
    DISCONNECTED = "disconnected"    # never connected / owner disconnected
    ERROR = "error"                  # last operation failed


@dataclass(frozen=True, slots=True)
class QboConnection:
    """A tenant's QuickBooks Online link. All datetimes are timezone-aware UTC."""

    tenant_id: str
    realm_id: str
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    status: QboStatus = QboStatus.CONNECTED
    connected_at: datetime | None = None

    def access_valid(self, now: datetime, *, skew_seconds: int = 120) -> bool:
        """Whether the access token is still usable at ``now`` (with a safety
        skew so we refresh slightly early rather than mid-request)."""
        return now + timedelta(seconds=skew_seconds) < self.access_expires_at

    def refresh_valid(self, now: datetime) -> bool:
        """Whether the refresh token can still mint a new access token."""
        return now < self.refresh_expires_at

    def redacted(self) -> "dict[str, object]":
        """A safe view for logs/UI — realm + expiries + status, never the tokens."""
        return {
            "tenant_id": self.tenant_id,
            "realm_id": self.realm_id,
            "status": self.status.value,
            "access_expires_at": self.access_expires_at.isoformat(),
            "refresh_expires_at": self.refresh_expires_at.isoformat(),
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
        }


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(dt: str) -> datetime:
    parsed = datetime.fromisoformat(dt)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def connection_to_dict(conn: QboConnection) -> dict[str, object]:
    """JSON-safe dict (the persisted form)."""
    return {
        "tenant_id": conn.tenant_id,
        "realm_id": conn.realm_id,
        "access_token": conn.access_token,
        "refresh_token": conn.refresh_token,
        "access_expires_at": _iso(conn.access_expires_at),
        "refresh_expires_at": _iso(conn.refresh_expires_at),
        "status": conn.status.value,
        "connected_at": _iso(conn.connected_at) if conn.connected_at else None,
    }


def connection_from_dict(data: Mapping[str, object]) -> QboConnection:
    """Rebuild a :class:`QboConnection` from its persisted dict."""
    def _s(key: str, default: str = "") -> str:
        v = data.get(key, default)
        return v if isinstance(v, str) else default

    connected_raw = data.get("connected_at")
    return QboConnection(
        tenant_id=_s("tenant_id"),
        realm_id=_s("realm_id"),
        access_token=_s("access_token"),
        refresh_token=_s("refresh_token"),
        access_expires_at=_parse(_s("access_expires_at")),
        refresh_expires_at=_parse(_s("refresh_expires_at")),
        status=QboStatus(_s("status", QboStatus.CONNECTED.value)),
        connected_at=_parse(connected_raw) if isinstance(connected_raw, str) else None,
    )


class ConnectionStore(Protocol):
    """Per-tenant persistence for QBO connections."""

    def save(self, conn: QboConnection) -> None: ...
    def get(self, tenant_id: str) -> QboConnection | None: ...
    def delete(self, tenant_id: str) -> None: ...
    def list_all(self) -> list[QboConnection]: ...


class InMemoryConnectionStore:
    """Connections held in memory — for tests and local development."""

    def __init__(self) -> None:
        self._by_tenant: dict[str, QboConnection] = {}

    def save(self, conn: QboConnection) -> None:
        self._by_tenant[conn.tenant_id] = conn

    def get(self, tenant_id: str) -> QboConnection | None:
        return self._by_tenant.get(tenant_id)

    def delete(self, tenant_id: str) -> None:
        self._by_tenant.pop(tenant_id, None)

    def list_all(self) -> list[QboConnection]:
        return [self._by_tenant[k] for k in sorted(self._by_tenant)]


class _DbApiCursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def close(self) -> None: ...


class _DbApiConnection(Protocol):
    def cursor(self) -> _DbApiCursor: ...
    def commit(self) -> None: ...


class SqlConnectionStore:
    """QBO connections over any DB-API 2.0 connection, one JSON row per tenant.
    ``placeholder`` is ``?`` (sqlite) or ``%s`` (psycopg). The token columns
    should be encrypted at rest by the deployment (KMS/pgcrypto)."""

    def __init__(
        self,
        connection: _DbApiConnection,
        *,
        table: str = "rgnr8_qbo_connection",
        placeholder: str = "?",
    ) -> None:
        self._conn = connection
        self._t = table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._t} "
                "(tenant_id TEXT PRIMARY KEY, conn_json TEXT NOT NULL)"
            )
        finally:
            cur.close()
        self._conn.commit()

    def save(self, conn: QboConnection) -> None:
        p = self._ph
        payload = json.dumps(connection_to_dict(conn), sort_keys=True)
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._t} (tenant_id, conn_json) VALUES ({p}, {p}) "
                "ON CONFLICT (tenant_id) DO UPDATE SET conn_json=excluded.conn_json",
                (conn.tenant_id, payload),
            )
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

    def get(self, tenant_id: str) -> QboConnection | None:
        rows = self._rows(
            f"SELECT conn_json FROM {self._t} WHERE tenant_id={self._ph}", (tenant_id,)
        )
        if not rows:
            return None
        return connection_from_dict(json.loads(str(rows[0][0])))

    def delete(self, tenant_id: str) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(f"DELETE FROM {self._t} WHERE tenant_id={self._ph}", (tenant_id,))
        finally:
            cur.close()
        self._conn.commit()

    def list_all(self) -> list[QboConnection]:
        rows = self._rows(f"SELECT conn_json FROM {self._t} ORDER BY tenant_id", ())
        return [connection_from_dict(json.loads(str(r[0]))) for r in rows]
