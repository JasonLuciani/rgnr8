"""Debt, fixed-asset, and financial-health screens.

Each surfaces one of the new engines to the owner: what you owe (and when you're
free), what you own (and its book value), and whether you're healthy. The forms
take plain dollars/percents and must hand the service integer minor units and
micro-rates — the conversion is the thing that's easy to get wrong, so it's
asserted directly against what the transport received.
"""

import json
from datetime import date
from typing import Any
from urllib.parse import urlencode

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryAuditLog,
    InMemoryUserDirectory,
    JwtAuthenticator,
    LedgerClient,
    LedgerResponse,
    Request,
    Role,
    User,
    WebApp,
    sign_jwt,
)

SECRET = "dbt-secret"
NOW = 1_760_000_000


class FakeTransport:
    def __init__(self, routes: dict[str, tuple[int, dict[str, Any]]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, str]] = []

    def request(self, method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        self.calls.append((method, path, body))
        status, payload = self.routes.get(
            f"{method} {path.split('?')[0]}", (404, {"error": "not found"})
        )
        return LedgerResponse(status, payload)


DEBT_DASH = {
    "contract": "debt-dashboard/1", "as_of": "2026-08-19", "currency": "USD",
    "totals": {
        "total_principal_minor": "2000000", "weighted_avg_rate_micro": "90000",
        "weighted_avg_rate_pct": "9.0000", "monthly_debt_service_minor": "86066",
        "annual_debt_service_minor": "1032792", "loan_count": 2,
    },
    "loans": [
        {"id": "truck", "lender": "First Bank", "kind": "TERM",
         "current_principal_minor": "1000000", "annual_rate_micro": "60000",
         "next_payment_date": "2026-09-15", "next_payment_minor": "86066",
         "payoff_date": "2027-01-15", "maturity_within_90_days": False,
         "interest_paid_ytd_minor": "5000", "principal_paid_ytd_minor": "81066"},
    ],
}

LOAN_DETAIL = {
    "tenant": "acme",
    "loan": {"id": "truck", "lender": "First Bank", "kind": "TERM",
             "current_principal_minor": "1000000", "original_principal_minor": "1000000",
             "annual_rate_micro": "60000", "payment_minor": "0"},
    "schedule": [
        {"period": 1, "due_date": "2026-09-15", "payment_minor": "86066",
         "principal_minor": "81066", "interest_minor": "5000", "balance_minor": "918934"},
    ],
    "payments": [],
}

PAYOFF = {"contract": "payoff-plan/1", "loan_id": "truck",
          "extra_per_period_minor": "20000", "base_periods": 12,
          "accelerated_periods": 10, "periods_saved": 2, "interest_saved_minor": "1500"}

ASSET_REG = {
    "contract": "fixed-asset-register/1", "currency": "USD",
    "assets": [
        {"id": "truck", "name": "Work Truck", "category": "Vehicles",
         "method": "STRAIGHT_LINE", "cost_minor": "1200000", "salvage_minor": "0",
         "accumulated_minor": "25000", "book_value_minor": "1175000", "status": "ACTIVE"},
    ],
    "totals": {"cost_minor": "1200000", "accumulated_minor": "25000",
               "book_value_minor": "1175000", "active_count": 1},
}

ASSET_DETAIL = {
    "tenant": "acme",
    "asset": {"id": "truck", "name": "Work Truck", "method": "STRAIGHT_LINE",
              "cost_minor": "1200000", "accumulated_minor": "25000",
              "book_value_minor": "1175000", "status": "ACTIVE"},
    "schedule": [
        {"period": 1, "through_date": "2026-02-28", "expense_minor": "25000",
         "accumulated_minor": "25000", "book_value_minor": "1175000"},
    ],
}

RATIOS = {
    "contract": "financial-ratios/1", "as_of": "2026-08-19", "currency": "USD",
    "inputs": {"revenue_minor": "500000", "net_income_minor": "170000",
               "total_assets_minor": "900000", "total_liabilities_minor": "300000",
               "equity_minor": "600000", "ebitda_minor": "200000",
               "annual_debt_service_minor": "1032792"},
    "liquidity": {"current_ratio": "5.90", "quick_ratio": "5.90",
                  "working_capital_minor": "490000",
                  "health": "healthy — comfortably covers short-term obligations"},
    "leverage": {"debt_to_equity": "0.50", "debt_to_assets": "0.33",
                 "interest_coverage": "18.00", "dscr": "0.19",
                 "health": "at risk — earnings do not cover debt service"},
    "profitability": {"gross_margin_pct": "60.00", "net_margin_pct": "34.00",
                      "return_on_assets_pct": "18.88", "return_on_equity_pct": "28.33",
                      "health": "strong — over 10% of revenue reaches the bottom line"},
    "covenants": [{"loan_id": "truck", "min_dscr_micro": "1250000",
                   "actual_dscr_micro": "190000", "breached": True}],
}

ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/debt": (200, DEBT_DASH),
    "GET /t/acme/debt/loans/truck": (200, LOAN_DETAIL),
    "GET /t/acme/debt/loans/truck/payoff": (200, PAYOFF),
    "POST /t/acme/debt/loans": (201, {"loan": DEBT_DASH["loans"][0], "entry_id": "acme:1"}),
    "POST /t/acme/debt/payments": (201, {"loan": DEBT_DASH["loans"][0],
                                         "principal_minor": "81066", "interest_minor": "5000"}),
    "GET /t/acme/assets": (200, ASSET_REG),
    "GET /t/acme/assets/truck": (200, ASSET_DETAIL),
    "POST /t/acme/assets": (201, {"asset": ASSET_REG["assets"][0], "entry_id": "acme:2"}),
    "POST /t/acme/assets/truck/depreciate": (201, {"asset": ASSET_REG["assets"][0],
                                                   "posted_minor": "25000", "entry_id": "acme:3"}),
    "GET /t/acme/ratios": (200, RATIOS),
}


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("0.00"))
    )


def _app() -> tuple[WebApp, FakeTransport, InMemoryAuditLog]:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    users.upsert_user(User("u-view", "view@acme.com", "Viewer"))
    users.set_membership("u-view", "acme", Role.VIEWER)
    audit = InMemoryAuditLog()
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW),
                 users=users, audit=audit)
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("0.00")), token="unused")
    transport = FakeTransport(dict(ROUTES))
    app.set_ledger(LedgerClient(transport, token="svc-token"))
    return app, transport, audit


def _req(app: WebApp, path: str, sub: str = "u-owner",
         method: str = "GET", form: dict[str, str] | None = None) -> Any:
    tok = sign_jwt({"sub": sub, "tenant": "acme", "exp": NOW + 3600}, SECRET)
    headers = {"authorization": f"Bearer {tok}"}
    body = ""
    if method == "POST":
        headers["content-type"] = "application/x-www-form-urlencoded"
        body = urlencode(form or {})
    return app.handle(Request(method, path, headers, body))


def _sent(transport: FakeTransport, needle: str) -> dict[str, Any]:
    return json.loads([c for c in transport.calls if c[0] == "POST" and needle in c[1]][-1][2])


# --- debt --------------------------------------------------------------------

def test_debt_dashboard_leads_with_what_you_owe() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/debt")
    assert r.status == 200
    assert "$20,000.00" in r.body   # total owed
    assert "First Bank" in r.body
    assert "9.00%" in r.body        # blended rate
    assert "Add a loan" in r.body   # owner can write


def test_adding_a_loan_converts_dollars_and_percent_to_minor_and_micro() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/debt/loans", method="POST", form={
        "id": "truck", "lender": "First Bank", "kind": "TERM",
        "original_principal": "10,000.00", "annual_rate_pct": "6.5",
        "start_date": "2026-01-15", "term_periods": "60", "frequency": "MONTHLY",
        "proceeds_to_code": "1000", "min_dscr": "1.25",
    })
    assert r.status in (302, 303)
    sent = _sent(transport, "/debt/loans")
    assert sent["original_principal_minor"] == "1000000"  # $10,000.00
    assert sent["annual_rate_micro"] == "65000"           # 6.5%
    assert sent["min_dscr_micro"] == "1250000"            # 1.25x
    assert sent["proceeds_to_code"] == "1000"


def test_recording_a_payment_converts_the_amount() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/debt/payments", method="POST", form={
        "loan_id": "truck", "date": "2026-02-15", "amount": "860.66", "paid_from_code": "1000",
    })
    assert r.status in (302, 303)
    sent = _sent(transport, "/debt/payments")
    assert sent["amount_minor"] == "86066"
    assert sent["loan_id"] == "truck"


def test_loan_detail_shows_the_amortization_schedule_and_payoff() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/debt/truck?extra=200")
    assert r.status == 200
    assert "Amortization schedule" in r.body
    assert "$860.66" in r.body               # a schedule payment
    assert "2 fewer payments" in r.body or "fewer payments" in r.body  # payoff plan rendered


def test_a_viewer_sees_debt_but_no_write_forms() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/debt", sub="u-view")
    assert r.status == 200
    assert "First Bank" in r.body
    assert "Add a loan" not in r.body
    assert "Post journal permission" in r.body


# --- fixed assets ------------------------------------------------------------

def test_asset_register_shows_book_value() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/assets")
    assert r.status == 200
    assert "Work Truck" in r.body
    assert "$11,750.00" in r.body   # net book value
    assert "Add an asset" in r.body


def test_adding_an_asset_converts_cost_and_units() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/assets", method="POST", form={
        "id": "truck", "name": "Work Truck", "in_service_date": "2026-01-31",
        "method": "UNITS_OF_PRODUCTION", "cost": "12,000.00", "salvage": "0",
        "total_units": "100", "paid_from_code": "1000",
    })
    assert r.status in (302, 303)
    sent = _sent(transport, "POST /t/acme/assets" if False else "/assets")
    assert sent["cost_minor"] == "1200000"
    assert sent["total_units_milli"] == "100000"  # 100 units × 1000


def test_running_depreciation_posts_the_through_date() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/assets/truck/depreciate", method="POST",
             form={"through_date": "2026-02-28"})
    assert r.status in (302, 303)
    sent = _sent(transport, "/assets/truck/depreciate")
    assert sent["through_date"] == "2026-02-28"


def test_asset_detail_shows_the_schedule() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/assets/truck")
    assert r.status == 200
    assert "Depreciation schedule" in r.body
    assert "Run depreciation" in r.body


# --- financial health --------------------------------------------------------

def test_health_dashboard_reads_out_in_plain_language() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/health")
    assert r.status == 200
    assert "5.90" in r.body                                    # current ratio
    assert "comfortably covers short-term" in r.body           # liquidity health
    assert "at risk" in r.body                                 # leverage health
    assert "Covenant alerts" in r.body                         # covenant breach surfaced
    assert "60.00" in r.body                                   # gross margin


def test_health_is_visible_to_a_viewer() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/health", sub="u-view")
    assert r.status == 200
    assert "strong" in r.body
