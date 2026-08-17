"""Owner-facing rendering of the TS ledger report contracts (aging/budget/
retained-earnings), surfaced under /t/<tenant>/reports/<id>."""

import json
from datetime import date

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryUserDirectory, JwtAuthenticator, Request, Role, User, WebApp, sign_jwt,
    render_owner_report,
)

SECRET = "owner-reports-secret"
NOW = 1_760_000_000


def _inputs() -> ForecastInputs:
    return ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("50000.00")))


def _app() -> WebApp:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users)
    app.add_tenant("acme", "Acme Co", _inputs(), ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    return app


def _get(app: WebApp, path: str, sub: str = "u-owner") -> object:
    tok = sign_jwt({"sub": sub, "tenant": "acme", "exp": NOW + 3600}, SECRET)
    return app.handle(Request("GET", path, {"authorization": f"Bearer {tok}"}, ""))


AGING = {
    "contract": "aging/1", "kind": "AR", "as_of": "2026-08-31",
    "bucket_labels": ["Current", "1-30", "31-60", "61-90", "90+"],
    "rows": [{"party_id": "acme-corp", "buckets_minor": ["100000", "0", "40000", "0", "0"], "total_minor": "140000"}],
    "column_totals_minor": ["100000", "0", "40000", "0", "0"], "grand_total_minor": "140000",
}
BUDGET = {
    "contract": "budget-vs-actual/1", "period": "2026-08", "currency": "USD",
    "lines": [{"code": "4000", "name": "Sales", "account_class": "revenue",
               "budget_minor": "2500000", "actual_minor": "3000000", "variance_minor": "500000",
               "favorable": True, "pct_of_budget": 120.0}],
    "total_budget_minor": "2500000", "total_actual_minor": "3000000", "total_variance_minor": "500000",
}
RE = {"contract": "retained-earnings/1", "currency": "USD", "beginning_minor": "2000000",
      "net_income_minor": "1300000", "distributions_minor": "500000", "ending_minor": "2800000"}


def test_renderers_format_money_exactly():
    ar = render_owner_report("aging_ar", AGING)
    assert "$1,400.00" in ar and "Receivables Aging" in ar and "acme-corp" in ar
    b = render_owner_report("budget", BUDGET)
    assert "$30,000.00" in b and "favorable" in b and "120%" in b
    r = render_owner_report("retained_earnings", RE)
    assert "$28,000.00" in r and "Ending retained earnings" in r
    # distributions shown as a negative
    assert "-$5,000.00" in r


def test_owner_report_route_renders_when_data_present():
    app = _app()
    app.add_owner_report("acme", "aging_ar", AGING)
    r = _get(app, "/t/acme/reports/receivables-aging")
    assert r.status == 200
    assert "Receivables Aging" in r.body and "$1,400.00" in r.body


def test_owner_report_route_degrades_gracefully_when_absent():
    app = _app()
    r = _get(app, "/t/acme/reports/budget")  # no data attached
    assert r.status == 200
    assert "isn't available yet" in r.body


def test_owner_reports_gated_by_permission():
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-none", "none@acme.com", "Nobody"))  # no membership
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users)
    app.add_tenant("acme", "Acme Co", _inputs(), ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    app.add_owner_report("acme", "aging_ar", AGING)
    r = _get(app, "/t/acme/reports/receivables-aging", sub="u-none")
    assert r.status == 403


def test_add_owner_report_rejects_unknown_kind():
    app = _app()
    try:
        app.add_owner_report("acme", "wizardry", AGING)
        assert False, "expected ValueError"
    except ValueError:
        pass
