"""Persist the beta fleet so the operator layer survives a restart.

`Fleet` in-memory is fine for a run, but a real operator can't lose the roster of
onboarded clients (and their forecast inputs, floor, schedule, and last-known
operational status) every time the process bounces. `FleetStore` is the seam:
an in-memory implementation for tests, and a SQL implementation over any DB-API
2.0 connection (sqlite3 in tests, psycopg/Postgres in production) mirroring the
pattern the delivery runtime already uses for subscriptions. A client is stored
as its `forecast-inputs/1` DTO plus the operator-set floor + schedule, so
rehydration rebuilds the exact `BetaTenant`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Protocol

from rgnr8_briefing import Schedule
from rgnr8_forecast import ForecastConfig, Money, from_dto, to_dto
from rgnr8_runtime.subscriptions import DbApiConnection

from .fleet import BetaTenant


@dataclass(frozen=True, slots=True)
class FleetTenantRecord:
    """The persisted form of a `BetaTenant` (DTO + floor + schedule + status)."""

    tenant_id: str
    name: str
    recipient: str
    inputs_json: str  # forecast-inputs/1 DTO as JSON text
    currency: str
    minimum_cash_minor: int
    horizon_weeks: int
    weekday: int
    hour: int
    minute: int
    timezone: str
    status_json: str | None = None  # ops-status/1 JSON, or None

    @staticmethod
    def of(bt: BetaTenant, status_json: str | None = None) -> "FleetTenantRecord":
        cfg = bt.config
        return FleetTenantRecord(
            tenant_id=bt.tenant_id,
            name=bt.name,
            recipient=bt.recipient,
            inputs_json=json.dumps(to_dto(bt.inputs)),
            currency=cfg.currency,
            minimum_cash_minor=cfg.minimum_cash.minor_units,
            horizon_weeks=cfg.horizon_weeks,
            weekday=bt.schedule.weekday,
            hour=bt.schedule.hour,
            minute=bt.schedule.minute,
            timezone=bt.schedule.timezone,
            status_json=status_json,
        )

    def to_beta_tenant(self) -> BetaTenant:
        return BetaTenant(
            tenant_id=self.tenant_id,
            name=self.name,
            recipient=self.recipient,
            inputs=from_dto(json.loads(self.inputs_json)),
            config=ForecastConfig(
                currency=self.currency,
                minimum_cash=Money(self.minimum_cash_minor, self.currency),
                horizon_weeks=self.horizon_weeks,
            ),
            schedule=Schedule(
                weekday=self.weekday, hour=self.hour, minute=self.minute, timezone=self.timezone
            ),
        )


class FleetStore(Protocol):
    def save_tenant(self, record: FleetTenantRecord) -> None:
        """Upsert a tenant's definition (leaves any stored status untouched)."""
        ...

    def save_status(self, tenant_id: str, status_json: str | None) -> None:
        """Update just the stored operational status for a tenant."""
        ...

    def load(self) -> list[FleetTenantRecord]:
        """Every persisted tenant, with its last-known status."""
        ...


class InMemoryFleetStore:
    def __init__(self) -> None:
        self._rows: dict[str, FleetTenantRecord] = {}

    def save_tenant(self, record: FleetTenantRecord) -> None:
        prior = self._rows.get(record.tenant_id)
        # upsert tenant fields; preserve an existing status
        status = record.status_json if prior is None else prior.status_json
        self._rows[record.tenant_id] = replace(record, status_json=status)

    def save_status(self, tenant_id: str, status_json: str | None) -> None:
        row = self._rows.get(tenant_id)
        if row is not None:
            self._rows[tenant_id] = replace(row, status_json=status_json)

    def load(self) -> list[FleetTenantRecord]:
        return [self._rows[k] for k in sorted(self._rows)]


_COLS = (
    "tenant_id, name, recipient, inputs_json, currency, minimum_cash_minor, "
    "horizon_weeks, weekday, hour, minute, timezone, status_json"
)


class SqlFleetStore:
    """Persist the fleet in a SQL table over any DB-API 2.0 connection."""

    def __init__(
        self,
        connection: DbApiConnection,
        *,
        table: str = "fleet_tenant",
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
                "tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL, recipient TEXT NOT NULL, "
                "inputs_json TEXT NOT NULL, currency TEXT NOT NULL, "
                "minimum_cash_minor INTEGER NOT NULL, horizon_weeks INTEGER NOT NULL, "
                "weekday INTEGER NOT NULL, hour INTEGER NOT NULL, minute INTEGER NOT NULL, "
                "timezone TEXT NOT NULL, status_json TEXT)"
            )
        finally:
            cur.close()
        self._conn.commit()

    def save_tenant(self, record: FleetTenantRecord) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._table} ({_COLS}) VALUES "
                f"({', '.join([p] * 12)}) "
                "ON CONFLICT (tenant_id) DO UPDATE SET "
                "name=excluded.name, recipient=excluded.recipient, inputs_json=excluded.inputs_json, "
                "currency=excluded.currency, minimum_cash_minor=excluded.minimum_cash_minor, "
                "horizon_weeks=excluded.horizon_weeks, weekday=excluded.weekday, hour=excluded.hour, "
                "minute=excluded.minute, timezone=excluded.timezone",
                (
                    record.tenant_id, record.name, record.recipient, record.inputs_json,
                    record.currency, record.minimum_cash_minor, record.horizon_weeks,
                    record.weekday, record.hour, record.minute, record.timezone, record.status_json,
                ),
            )
        finally:
            cur.close()
        self._conn.commit()

    def save_status(self, tenant_id: str, status_json: str | None) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"UPDATE {self._table} SET status_json={p} WHERE tenant_id={p}",
                (status_json, tenant_id),
            )
        finally:
            cur.close()
        self._conn.commit()

    def load(self) -> list[FleetTenantRecord]:
        cur = self._conn.cursor()
        try:
            cur.execute(f"SELECT {_COLS} FROM {self._table} ORDER BY tenant_id")
            rows = cur.fetchall()
        finally:
            cur.close()
        out: list[FleetTenantRecord] = []
        for r in rows:
            out.append(
                FleetTenantRecord(
                    tenant_id=str(r[0]), name=str(r[1]), recipient=str(r[2]),
                    inputs_json=str(r[3]), currency=str(r[4]),
                    minimum_cash_minor=int(str(r[5])), horizon_weeks=int(str(r[6])),
                    weekday=int(str(r[7])), hour=int(str(r[8])), minute=int(str(r[9])),
                    timezone=str(r[10]), status_json=str(r[11]) if r[11] is not None else None,
                )
            )
        return out
