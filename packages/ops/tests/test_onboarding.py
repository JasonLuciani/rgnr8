"""Stage 1 (overlay) onboarding, end to end on the Python side.

Feeds `Fleet.onboard_from_dto` a `forecast-inputs/1` payload shaped exactly like
the TS overlay engine (`buildOverlayForecastInputs`) emits — opening cash from
bank balances, open AR as invoices, open AP as bills — and proves the tenant is
provisioned everywhere and shows up in the fleet dashboard with a real forecast.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from rgnr8_forecast import Money
from rgnr8_ops import Fleet, build_ops_report
from rgnr8_web import Request, verify_jwt

SECRET = "onboard-secret"
NOW_EPOCH = 1_760_000_000
MT = ZoneInfo("America/Denver")

# The exact JSON the TS overlay engine produces from a QBO snapshot.
OVERLAY_DTO = {
    "contract": "forecast-inputs/1",
    "currency": "USD",
    "opening": {
        "as_of": "2026-08-31",
        "available": {"minor": 6321055, "currency": "USD"},
        "restricted": {"minor": 0, "currency": "USD"},
        "verified": False,
    },
    "invoices": [
        {
            "id": "INV-101",
            "customer_id": "C-7",
            "issue_date": "2026-07-20",
            "due_date": "2026-08-19",
            "open_amount": {"minor": 1800000, "currency": "USD"},
            "status": "OPEN",
        }
    ],
    "customer_histories": [],
    "bills": [
        {
            "id": "BILL-201",
            "vendor_id": "V-3",
            "due_date": "2026-08-14",
            "amount": {"minor": 600000, "currency": "USD"},
            "scheduled_date": None,
        }
    ],
    "recurring": [],
    "payroll": [],
    "debt": [],
    "one_time": [],
    "pipeline": [],
}


def _fleet() -> Fleet:
    return Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH)


def test_onboard_from_overlay_dto_registers_everywhere() -> None:
    f = _fleet()
    bt = f.onboard_from_dto(
        "northwind", "Northwind LLC", "owner@northwind.com",
        OVERLAY_DTO, Money.from_decimal("20000.00"),
    )
    assert bt.tenant_id == "northwind"
    # opening cash survived the DTO round-trip: 63210.55
    assert bt.inputs.opening.available == Money.from_decimal("63210.55")
    assert len(bt.inputs.invoices) == 1
    assert len(bt.inputs.bills) == 1
    # registered on all three subsystems
    assert "northwind" in f.tenants
    assert f.tenant_source.resolve("northwind") is not None
    assert {s.tenant_id for s in f.subscriptions.list()} == {"northwind"}


def test_onboarded_tenant_serves_a_forecast_over_the_web() -> None:
    f = _fleet()
    f.onboard_from_dto(
        "northwind", "Northwind LLC", "owner@northwind.com",
        OVERLAY_DTO, Money.from_decimal("20000.00"),
    )
    app = f.web_app()
    token = f.mint_token("northwind")
    claims = verify_jwt(token, SECRET, now=NOW_EPOCH)
    assert claims["tenant"] == "northwind"
    r = app.handle(Request("GET", "/api/northwind/today", {"authorization": f"Bearer {token}"}))
    assert r.status == 200


def test_onboarded_tenant_appears_in_the_fleet_dashboard() -> None:
    f = _fleet()
    f.onboard_from_dto(
        "northwind", "Northwind LLC", "owner@northwind.com",
        OVERLAY_DTO, Money.from_decimal("20000.00"),
    )
    report = build_ops_report(f, datetime(2026, 9, 2, 12, 0, tzinfo=MT))
    assert report.total == 1
    row = report.rows[0]
    assert row.tenant_id == "northwind"
    # a real forecast ran off the overlay inputs
    assert row.cash_today == "63210.55"
    assert row.status in ("STABLE", "WATCH", "AT_RISK")


def test_onboard_from_dto_accepts_json_text() -> None:
    import json

    f = _fleet()
    f.onboard_from_dto(
        "northwind", "Northwind LLC", "owner@northwind.com",
        json.dumps(OVERLAY_DTO), Money.from_decimal("20000.00"),
    )
    assert f.tenant_source.resolve("northwind") is not None
