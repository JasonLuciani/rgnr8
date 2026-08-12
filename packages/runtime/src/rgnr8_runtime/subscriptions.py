"""Durable storage for weekly-briefing subscriptions.

A `Subscription` (tenant, recipient, `Schedule`, `last_sent`) is the runtime's
per-recipient cursor: `last_sent` is what makes delivery idempotent across
restarts — the scheduler only sends when the most recent scheduled fire is newer
than it. So the store must persist `last_sent`, and the runtime writes it back
after every fired tick. Two implementations: in-memory, and SQL over a DB-API 2.0
seam (sqlite3 in tests, psycopg/Postgres in production) — the same seam the web's
`SqlTenantStore` uses.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from rgnr8_briefing import Schedule, Subscription


class SubscriptionStore(Protocol):
    def list(self) -> list[Subscription]:
        """All subscriptions (each carries its own last_sent cursor)."""
        ...

    def save(self, sub: Subscription) -> None:
        """Persist a subscription, keyed by (tenant_id, recipient)."""
        ...

    def remove(self, tenant_id: str, recipient: str) -> None:
        """Delete a subscription. Idempotent — a no-op if absent."""
        ...


class InMemorySubscriptionStore:
    def __init__(self) -> None:
        self._subs: dict[tuple[str, str], Subscription] = {}

    def list(self) -> list[Subscription]:
        return list(self._subs.values())

    def save(self, sub: Subscription) -> None:
        self._subs[(sub.tenant_id, sub.recipient)] = sub

    def remove(self, tenant_id: str, recipient: str) -> None:
        self._subs.pop((tenant_id, recipient), None)


class DbApiCursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def close(self) -> None: ...


class DbApiConnection(Protocol):
    def cursor(self) -> DbApiCursor: ...
    def commit(self) -> None: ...


class SqlSubscriptionStore:
    """Persist subscriptions in a SQL table over any DB-API 2.0 connection.
    ``placeholder`` is the driver's marker (``"?"`` sqlite3, ``"%s"`` psycopg)."""

    def __init__(
        self,
        connection: DbApiConnection,
        *,
        table: str = "briefing_subscription",
        placeholder: str = "?",
    ) -> None:
        self._conn = connection
        self._table = table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._table} ("
                "tenant_id TEXT NOT NULL, recipient TEXT NOT NULL, "
                "weekday INTEGER NOT NULL, hour INTEGER NOT NULL, minute INTEGER NOT NULL, "
                "timezone TEXT NOT NULL, last_sent TEXT, active INTEGER NOT NULL DEFAULT 1, "
                "PRIMARY KEY (tenant_id, recipient))"
            )
        finally:
            cur.close()
        self._conn.commit()

    def list(self) -> list[Subscription]:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT tenant_id, recipient, weekday, hour, minute, timezone, last_sent, active FROM {self._table}"
            )
            rows = cur.fetchall()
        finally:
            cur.close()
        subs: list[Subscription] = []
        for row in rows:
            last_sent_raw = row[6]
            last_sent = (
                datetime.fromisoformat(str(last_sent_raw)) if last_sent_raw is not None else None
            )
            subs.append(
                Subscription(
                    tenant_id=str(row[0]),
                    recipient=str(row[1]),
                    schedule=Schedule(
                        weekday=int(str(row[2])),
                        hour=int(str(row[3])),
                        minute=int(str(row[4])),
                        timezone=str(row[5]),
                    ),
                    last_sent=last_sent,
                    active=bool(int(str(row[7]))),
                )
            )
        subs.sort(key=lambda s: (s.tenant_id, s.recipient))
        return subs

    def save(self, sub: Subscription) -> None:
        p = self._ph
        last_sent = sub.last_sent.isoformat() if sub.last_sent is not None else None
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._table} "
                "(tenant_id, recipient, weekday, hour, minute, timezone, last_sent, active) "
                f"VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}) "
                "ON CONFLICT (tenant_id, recipient) DO UPDATE SET "
                "weekday=excluded.weekday, hour=excluded.hour, minute=excluded.minute, "
                "timezone=excluded.timezone, last_sent=excluded.last_sent, active=excluded.active",
                (
                    sub.tenant_id,
                    sub.recipient,
                    sub.schedule.weekday,
                    sub.schedule.hour,
                    sub.schedule.minute,
                    sub.schedule.timezone,
                    last_sent,
                    1 if sub.active else 0,
                ),
            )
        finally:
            cur.close()
        self._conn.commit()

    def remove(self, tenant_id: str, recipient: str) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"DELETE FROM {self._table} WHERE tenant_id={p} AND recipient={p}",
                (tenant_id, recipient),
            )
        finally:
            cur.close()
        self._conn.commit()
