"""A SQL-backed ``TenantStore`` over the Python DB-API 2.0 seam.

The web write path is durable via ``JsonFileTenantStore``; production wants it in
the same relational store as the ledger. This implements the same two-method
``TenantStore`` contract (``load`` / ``save``) against any PEP-249 connection —
stdlib ``sqlite3`` in tests, ``psycopg`` (Postgres) in production — so no driver
dependency is baked in and the store is exercised without a live database.

State is stored as one JSON document per tenant (the same shape
``TenantState.to_dict`` emits), upserted with ``INSERT ... ON CONFLICT`` (both
SQLite and Postgres support it). The paramstyle differs between drivers, so the
placeholder is injectable: ``"?"`` for SQLite (qmark), ``"%s"`` for psycopg.
"""

from __future__ import annotations

import json
from typing import Protocol

from .store import TenantState


class DbApiCursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...
    def fetchone(self) -> tuple[object, ...] | None: ...
    def close(self) -> None: ...


class DbApiConnection(Protocol):
    def cursor(self) -> DbApiCursor: ...
    def commit(self) -> None: ...


class SqlTenantStore:
    """Persist tenant write-path state as JSON in a single SQL table.

    ``connection`` is any DB-API 2.0 connection. ``placeholder`` is the driver's
    parameter marker (``"?"`` for sqlite3, ``"%s"`` for psycopg). Call
    ``create_schema()`` once against a fresh database.
    """

    def __init__(
        self,
        connection: DbApiConnection,
        *,
        table: str = "web_tenant_state",
        placeholder: str = "?",
    ) -> None:
        self._conn = connection
        self._table = table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._table} "
                "(tenant_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)"
            )
        finally:
            cur.close()
        self._conn.commit()

    def load(self, tenant_id: str) -> TenantState:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT state_json FROM {self._table} WHERE tenant_id = {self._ph}",
                (tenant_id,),
            )
            row = cur.fetchone()
        finally:
            cur.close()
        if row is None:
            return TenantState()
        return TenantState.from_dict(json.loads(str(row[0])))

    def save(self, tenant_id: str, state: TenantState) -> None:
        payload = json.dumps(state.to_dict(), sort_keys=True)
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._table} (tenant_id, state_json) "
                f"VALUES ({self._ph}, {self._ph}) "
                "ON CONFLICT (tenant_id) DO UPDATE SET state_json = excluded.state_json",
                (tenant_id, payload),
            )
        finally:
            cur.close()
        self._conn.commit()
