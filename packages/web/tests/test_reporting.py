"""The general ledger and budget screens.

What matters here is that the report reads correctly: the ledger reconciles on
screen the way it does in the service, and a budget miss on revenue is shown as
a miss rather than congratulated as an under-run.
"""

import json
from datetime import date
from typing import Any

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

SECRET = "report-secret"
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


ACCOUNTS = {
    "accounts": [
        {"code": "1000", "name": "Business Checking", "type": "ASSET"},
        {"code": "4100", "name": "Consulting Income", "type": "REVENUE"},
        {"code": "6300", "name": "Rent & Lease", "type": "EXPENSE"},
        {"code": "6500", "name": "Software", "type": "EXPENSE"},
    ],
}

GL = {
    "contract": "gl-detail/1", "currency": "USD", "from": "", "to": "",
    "total_debit_minor": "1574900", "total_credit_minor": "1574900",
    "accounts": [
        {
            "code": "1000", "name": "Business Checking", "type": "ASSET",
            "opening_minor": "400000", "closing_minor": "1225100",
            "total_debit_minor": "1200000", "total_credit_minor": "374900",
            "rows": [
                {"entry_id": "acme:2", "sequence": 2, "date": "2026-08-03",
                 "memo": "August retainer", "debit_minor": "1200000",
                 "credit_minor": "0", "balance_minor": "1600000"},
                {"entry_id": "acme:3", "sequence": 3, "date": "2026-08-09",
                 "memo": "Studio rent", "debit_minor": "0",
                 "credit_minor": "350000", "balance_minor": "1250000"},
            ],
        },
    ],
}

BUDGET = {
    "contract": "budget-vs-actual/1", "period": "2026-08", "currency": "USD",
    "budgeted_accounts": 3,
    "total_budget_minor": "1890000", "total_actual_minor": "1574900",
    "total_variance_minor": "-315100",
    "lines": [
        {"code": "4100", "name": "Consulting Income", "account_class": "REVENUE",
         "budget_minor": "1500000", "actual_minor": "1200000",
         "variance_minor": "-300000", "favorable": False},
        {"code": "6300", "name": "Rent & Lease", "account_class": "EXPENSE",
         "budget_minor": "350000", "actual_minor": "350000",
         "variance_minor": "0", "favorable": True},
        {"code": "6500", "name": "Software", "account_class": "EXPENSE",
         "budget_minor": "40000", "actual_minor": "24900",
         "variance_minor": "-15100", "favorable": True},
    ],
}

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/accounts": (200, ACCOUNTS),
    "GET /t/acme/gl": (200, GL),
    "GET /t/acme/budget/2026-08": (200, BUDGET),
    "POST /t/acme/budget": (201, {"period": "2026-08", "saved": 3}),
}


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("3000.00"))
    )


def _app(
    routes: dict[str, tuple[int, dict[str, Any]]] | None = None,
) -> tuple[WebApp, FakeTransport, InMemoryAuditLog]:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    users.upsert_user(User("u-view", "view@acme.com", "Viewer"))
    users.set_membership("u-view", "acme", Role.VIEWER)
    audit = InMemoryAuditLog()
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW),
                 users=users, audit=audit)
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("1000.00")), token="unused")
    transport = FakeTransport(routes if routes is not None else dict(DEFAULT_ROUTES))
    app.set_ledger(LedgerClient(transport, token="svc-token"))
    return app, transport, audit


def _req(app: WebApp, path: str, sub: str = "u-owner",
         method: str = "GET", body: str = "") -> Any:
    tok = sign_jwt({"sub": sub, "tenant": "acme", "exp": NOW + 3600}, SECRET)
    headers = {"authorization": f"Bearer {tok}"}
    if method == "POST":
        headers["content-type"] = "application/x-www-form-urlencoded"
    return app.handle(Request(method, path, headers, body))


# --- general ledger ----------------------------------------------------------

def test_the_ledger_shows_every_posting_with_its_running_balance() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/gl")
    assert r.status == 200
    assert "1000 — Business Checking" in r.body
    assert "August retainer" in r.body and "Studio rent" in r.body
    assert "acme:2" in r.body                       # the entry it came from
    assert "$16,000.00" in r.body and "$12,500.00" in r.body
    assert "Opening balance" in r.body and "$4,000.00" in r.body
    assert "Closing balance" in r.body and "$12,251.00" in r.body


def test_the_ledger_says_plainly_whether_it_balances() -> None:
    app, _t, _a = _app()
    assert "Debits equal credits across every account" in _req(app, "/t/acme/books/gl").body

    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/gl"] = (200, dict(GL, total_credit_minor="1"))
    app2, _t2, _a2 = _app(routes)
    assert "OUT OF BALANCE" in _req(app2, "/t/acme/books/gl").body


def test_the_date_window_and_account_filter_reach_the_service() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/books/gl?from=2026-08-01&to=2026-08-31&codes=6300,6500")
    call = [c for c in transport.calls if "/gl" in c[1]][0]
    assert "from=2026-08-01" in call[1] and "to=2026-08-31" in call[1]
    assert "codes=6300%2C6500" in call[1]


def test_an_empty_period_says_so_rather_than_rendering_nothing() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/gl"] = (200, dict(GL, accounts=[], total_debit_minor="0",
                                          total_credit_minor="0"))
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/gl")
    assert "Nothing was posted in this period" in r.body


def test_books_home_links_to_the_ledger_and_the_budget() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/trial-balance"] = (200, {
        "currency": "USD", "in_balance": True, "rows": [],
        "total_debit_minor": "0", "total_credit_minor": "0",
    })
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books")
    assert "/t/acme/books/gl" in r.body
    assert "/t/acme/books/budget" in r.body


# --- budget vs actual --------------------------------------------------------

def test_a_revenue_miss_is_shown_as_a_miss_not_an_under_run() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/budget?period=2026-08")
    assert r.status == 200
    # revenue: budgeted 15,000, earned 12,000
    assert "$15,000.00" in r.body and "$12,000.00" in r.body
    assert "worse than planned" in r.body
    # software: spent less than budgeted — that IS a win
    assert "better than planned" in r.body


def test_being_exactly_on_plan_reads_as_on_plan() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/budget?period=2026-08")
    assert "on plan" in r.body


def test_no_budget_says_there_is_nothing_to_compare_against() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/budget/2026-08"] = (200, dict(BUDGET, budgeted_accounts=0))
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/budget?period=2026-08")
    assert "No budget set for 2026-08" in r.body
    assert "nothing to compare them against" in r.body


def test_the_period_defaults_to_the_current_month() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/books/budget")
    assert any("/budget/2026-08" in c[1] for c in transport.calls)


def test_the_budget_form_only_offers_income_and_expense_accounts() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/budget?period=2026-08")
    assert 'name="amount_4100"' in r.body
    assert 'name="amount_6300"' in r.body
    assert 'name="amount_1000"' not in r.body, "you don't budget a bank balance"


def test_the_form_is_prefilled_so_revising_is_editing_not_retyping() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/budget?period=2026-08")
    assert 'name="amount_4100" value="15000.00"' in r.body


def test_saving_parses_amounts_exactly_and_skips_blanks() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/books/budget", method="POST",
             body="period=2026-08&amount_4100=15,000.00&amount_6300=350.00&amount_6500=")
    assert r.status == 302
    sent = json.loads([c for c in transport.calls if c[0] == "POST"][0][2])
    assert sent["period"] == "2026-08"
    assert sorted(sent["lines"], key=lambda x: x["account_code"]) == [
        {"account_code": "4100", "amount_minor": "1500000"},
        {"account_code": "6300", "amount_minor": "35000"},
    ]
    assert "Budget%20saved" in str(r.headers.get("Location", ""))
    assert any(e.action == "budget.saved" for e in audit.events(tenant_id="acme"))


def test_an_all_blank_budget_never_reaches_the_service() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/books/budget", method="POST",
             body="period=2026-08&amount_4100=&amount_6300=")
    assert "err=" in str(r.headers.get("Location", ""))
    assert not [c for c in transport.calls if c[0] == "POST"]


def test_a_malformed_figure_is_reported_not_guessed() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/books/budget", method="POST",
             body="period=2026-08&amount_4100=fifteen+thousand")
    assert "not%20a%20valid%20amount" in str(r.headers.get("Location", ""))
    assert not [c for c in transport.calls if c[0] == "POST"]


def test_a_viewer_can_read_both_reports_but_set_no_budget() -> None:
    app, transport, _a = _app()
    assert _req(app, "/t/acme/books/gl", sub="u-view").status == 200
    page = _req(app, "/t/acme/books/budget?period=2026-08", sub="u-view")
    assert page.status == 200
    assert "worse than planned" in page.body
    assert "Set the budget" not in page.body
    assert _req(app, "/t/acme/books/budget", sub="u-view", method="POST",
                body="period=2026-08&amount_4100=1").status == 403
    assert not [c for c in transport.calls if c[0] == "POST"]
