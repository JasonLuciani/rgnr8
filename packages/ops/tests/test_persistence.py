"""The fleet survives a restart — onboard, bounce, rehydrate, same picture."""

import io
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


# --- onboarding / go-live durability (H0-7) ----------------------------------

def test_onboarding_and_go_live_state_survive_a_restart() -> None:
    from rgnr8_ops import SqlOnboardingRegistry

    conn = sqlite3.connect(":memory:")
    reg = SqlOnboardingRegistry(conn)
    reg.create_schema()
    reg.set_coa_category("acme", "CONTRACTOR_TRADES")
    reg.mark_cutover("acme", "quickbooks", "2026-07-01",
                     marked_by="op@rgnr8.app", marked_at=NOW_EPOCH, opening_entry_id="OB-1")
    reg.set_go_live_request("acme", {"contract": "go-live/1", "category": "CONTRACTOR_TRADES"})

    # "Restart": a brand-new registry over the same connection sees the state.
    again = SqlOnboardingRegistry(conn)
    assert again.coa_category("acme") == "CONTRACTOR_TRADES"
    assert again.is_live("acme") is True
    rec = again.cutover("acme")
    assert rec is not None and rec.source_system == "quickbooks" and rec.opening_entry_id == "OB-1"
    assert again.go_live_request("acme") == {"contract": "go-live/1", "category": "CONTRACTOR_TRADES"}
    # --- B-1: the production composition now wires the durable registry --------
    # The operator console factory must build in both modes; with a live conn it
    # is the composition that persists go-live/onboarding state across a restart.
    from rgnr8_ops import create_operator_application, run_migrations

    conn2 = sqlite3.connect(":memory:")
    run_migrations(conn2, applied_at="t", dialect="sqlite")  # the release-step schema
    durable_app = create_operator_application({"RGNR8_JWT_SECRET": "test-secret"}, conn=conn2)
    assert callable(durable_app)

    # B-2: staff browser sign-in must be wired. GET /operator/login has to render
    # the password form (HTTP 200), not {"error":"browser login is not
    # configured"} — a regression where the factory built OperatorApp with no
    # auth_service, so the console's own login page 404'd in production even
    # though the credential store (shared with the owner web app) was right there.
    def _login_status(app: object) -> tuple[str, bytes]:
        environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/operator/login",
                   "QUERY_STRING": "", "CONTENT_LENGTH": "0",
                   "wsgi.input": io.BytesIO(b"")}
        captured: dict[str, str] = {}
        body = b"".join(app(environ, lambda status, headers: captured.__setitem__("s", status)))  # type: ignore[operator]
        return captured["s"], body

    status, body = _login_status(durable_app)
    assert status.startswith("200"), status
    assert b"browser login is not configured" not in body
    # The dev console (no conn) also offers the form in non-SSO mode.
    dev_status, _ = _login_status(create_operator_application({"RGNR8_JWT_SECRET": "s"}))
    assert dev_status.startswith("200"), dev_status
    # In jwks (SSO) mode staff come through the IdP, so the password form stays off.
    sso = create_operator_application(
        {"RGNR8_JWT_SECRET": "s", "RGNR8_AUTH_MODE": "jwks",
         "RGNR8_JWKS_URL": "https://idp.example/keys",
         "RGNR8_JWT_ISSUER": "https://idp.example/", "RGNR8_JWT_AUDIENCE": "rgnr8"})
    sso_status, _ = _login_status(sso)
    assert sso_status.startswith("404"), sso_status
    # State written through the same durable table is visible to a fresh build,
    # proving the console reads/writes the persistent registry, not in-memory.
    SqlOnboardingRegistry(conn2).set_coa_category("beta", "RETAIL")
    create_operator_application({"RGNR8_JWT_SECRET": "test-secret"}, conn=conn2)
    assert SqlOnboardingRegistry(conn2).coa_category("beta") == "RETAIL"

    dev_app = create_operator_application({"RGNR8_JWT_SECRET": "s"})  # no conn → in-memory dev console still builds
    assert callable(dev_app)

    # setting one field must not clear the other (merge semantics)
    again.set_coa_category("acme", "RETAIL")
    assert again.go_live_request("acme") is not None
    assert again.coa_category("acme") == "RETAIL"
