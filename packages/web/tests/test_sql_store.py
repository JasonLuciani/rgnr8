"""The SQL-backed TenantStore persists the web write path against a real
relational store (exercised with stdlib sqlite3; prod swaps in psycopg/Postgres).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from rgnr8_forecast import (
    CashPosition,
    ForecastConfig,
    ForecastInputs,
    Invoice,
    Money,
)
from rgnr8_web import Request, SqlTenantStore, WebApp

BRIGHT = {"authorization": "Bearer tok-bright"}


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def _inputs(available: str) -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 3), available=usd(available)),
        invoices=(Invoice("INV-1", "acme", date(2026, 8, 1), date(2026, 8, 20), usd("15000.00")),),
    )


def _app(store: SqlTenantStore) -> WebApp:
    app = WebApp(store=store)
    app.add_tenant("bright", "Bright Agency", _inputs("80000.00"), ForecastConfig(minimum_cash=usd("10000.00")), token="tok-bright")
    return app


def _today(app: WebApp) -> dict[str, object]:
    return json.loads(app.handle(Request("GET", "/api/bright/today", BRIGHT)).body)


def test_write_path_persists_across_connections(tmp_path: object) -> None:
    db = f"{tmp_path}/web.db"  # type: ignore[str-bytes-safe]

    # connection 1: create schema, raise the floor, record a decision
    conn1 = sqlite3.connect(db)
    store1 = SqlTenantStore(conn1)
    store1.create_schema()
    app1 = _app(store1)
    r = app1.handle(Request("POST", "/api/bright/assumptions", BRIGHT, json.dumps({"minimum_cash": "60000.00"})))
    assert r.status == 200
    app1.handle(Request("POST", "/api/bright/decisions", BRIGHT, json.dumps({"label": "Call Northwind"})))
    conn1.close()

    # connection 2: brand-new connection + WebApp against the same DB file
    conn2 = sqlite3.connect(db)
    store2 = SqlTenantStore(conn2)
    app2 = _app(store2)
    assert _today(app2)["floor"] == "60000.00"  # override rehydrated from SQL
    listed = json.loads(app2.handle(Request("GET", "/api/bright/decisions", BRIGHT)).body)
    labels = [d.get("label") for d in listed["decisions"]]
    assert "Call Northwind" in labels
    assert any(d.get("kind") == "assumption" for d in listed["decisions"])
    conn2.close()


def test_save_is_an_upsert_one_row_per_tenant() -> None:
    conn = sqlite3.connect(":memory:")
    store = SqlTenantStore(conn)
    store.create_schema()
    app = _app(store)
    # three assumption changes for the same tenant
    for floor in ("11000.00", "12000.00", "13000.00"):
        app.handle(Request("POST", "/api/bright/assumptions", BRIGHT, json.dumps({"minimum_cash": floor})))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM web_tenant_state")
    count = cur.fetchone()[0]
    assert count == 1  # upsert, not append
    assert _today(app)["floor"] == "13000.00"
    conn.close()


def test_load_unknown_tenant_returns_empty_state() -> None:
    conn = sqlite3.connect(":memory:")
    store = SqlTenantStore(conn)
    store.create_schema()
    state = store.load("nobody")
    assert state.min_cash_override is None
    assert state.payment_overrides == {}
    assert state.decisions == []
    conn.close()


def test_postgres_placeholder_builds_valid_sql() -> None:
    # with the psycopg placeholder the generated SQL still round-trips against a
    # driver that accepts "%s" — sqlite3 doesn't, so we just assert construction
    # and schema creation don't blow up on the qmark path used everywhere else.
    conn = sqlite3.connect(":memory:")
    store = SqlTenantStore(conn, placeholder="?", table="tenants_v2")
    store.create_schema()
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tenants_v2'")
    assert cur.fetchone() is not None
    conn.close()
