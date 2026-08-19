"""The fleet survives a restart — onboard, bounce, rehydrate, same picture."""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from factory import at_risk_tenant, steady_tenant
from rgnr8_forecast import Money
from rgnr8_ops import (
    Fleet,
    InMemoryFleetStore,
    SqlFleetStore,
    build_ops_report,
)

SECRET = "persist-secret"
NOW_EPOCH = 1_760_000_000
MT = ZoneInfo("America/Denver")
OVERLAY_STATUS = {
    "connectors": {"total": 2, "needs_attention": 1, "stale": 0, "last_sync": "2026-09-02T06:00:00Z"},
    "close": {"period": "2026-08", "total": 7, "done": 5, "overdue": 1, "blocked": 0,
              "next_task": "Reconcile bank", "next_due": "2026-09-03"},
}


def test_inmemory_store_roundtrips_a_tenant() -> None:
    store = InMemoryFleetStore()
    f = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH, store=store)
    f.onboard(steady_tenant())
    f.set_status_from_json("acme", OVERLAY_STATUS)

    back = Fleet.load(store, jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    assert set(back.tenants) == {"acme"}
    bt = back.tenants["acme"]
    assert bt.config.minimum_cash == Money.from_decimal("10000.00")
    assert bt.inputs.opening.available == Money.from_decimal("120000.00")
    # status survived
    assert back.statuses["acme"].connectors is not None
    assert back.statuses["acme"].connectors.needs_attention == 1


def test_sql_store_survives_a_restart_end_to_end() -> None:
    conn = sqlite3.connect(":memory:")
    store = SqlFleetStore(conn)
    store.create_schema()

    f = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH, store=store)
    f.onboard(steady_tenant())
    f.onboard(at_risk_tenant())
    f.set_status_from_json("bright", OVERLAY_STATUS)

    # "restart": brand-new store object over the same connection, rehydrate
    store2 = SqlFleetStore(conn)
    back = Fleet.load(store2, jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    assert set(back.tenants) == {"acme", "bright"}

    report = build_ops_report(back, datetime(2026, 9, 2, 12, 0, tzinfo=MT))
    assert report.total == 2
    bright = next(r for r in report.rows if r.tenant_id == "bright")
    assert bright.books_current is False  # status rehydrated
    assert "5/7" in bright.close_summary
    # the forecast still runs off the rehydrated inputs
    assert bright.cash_today == "40000.00"


def test_onboard_from_dto_persists_and_rehydrates() -> None:
    store = InMemoryFleetStore()
    f = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH, store=store)
    dto = {
        "contract": "forecast-inputs/1", "currency": "USD",
        "opening": {"as_of": "2026-08-31", "available": {"minor": 5000000, "currency": "USD"},
                    "restricted": {"minor": 0, "currency": "USD"}, "verified": False},
        "invoices": [], "customer_histories": [], "bills": [],
        "recurring": [], "payroll": [], "debt": [], "one_time": [], "pipeline": [],
    }
    f.onboard_from_dto("northwind", "Northwind", "o@n.com", dto, Money.from_decimal("20000.00"))
    back = Fleet.load(store, jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    assert back.tenants["northwind"].inputs.opening.available == Money.from_decimal("50000.00")
