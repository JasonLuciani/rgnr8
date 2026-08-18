"""The owner-facing books surface: chart of accounts, trial balance, register,
statements, and posting a journal entry — all against a fake ledger transport."""

import json
from datetime import date
from typing import Any

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryAuditLog, InMemoryUserDirectory, JwtAuthenticator, LedgerClient,
    LedgerResponse, Request, Role, User, WebApp, sign_jwt,
)

SECRET = "books-secret"
NOW = 1_760_000_000


class FakeTransport:
    """Records calls and replays canned responses keyed by 'METHOD path'."""

    def __init__(self, routes: dict[str, tuple[int, dict[str, Any]]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, str]] = []

    def request(self, method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        self.calls.append((method, path, body))
        key = f"{method} {path.split('?')[0]}"
        status, payload = self.routes.get(key, (404, {"error": "not found"}))
        return LedgerResponse(status, payload)


ACCOUNTS = {
    "tenant": "acme",
    "accounts": [
        {"code": "1000", "name": "Business Checking", "type": "ASSET", "subtype": "BANK"},
        {"code": "4000", "name": "Services Income", "type": "REVENUE", "subtype": "INCOME"},
        {"code": "6300", "name": "Rent & Lease", "type": "EXPENSE", "subtype": "EXPENSE"},
    ],
}
TRIAL_BALANCE = {
    "tenant": "acme", "currency": "USD", "in_balance": True,
    "total_debit_minor": "500000", "total_credit_minor": "500000",
    "rows": [
        {"code": "1000", "name": "Business Checking", "type": "ASSET",
         "debit_minor": "300000", "credit_minor": "0"},
        {"code": "4000", "name": "Services Income", "type": "REVENUE",
         "debit_minor": "0", "credit_minor": "500000"},
    ],
}
REGISTER = {
    "tenant": "acme", "code": "1000", "name": "Business Checking", "type": "ASSET",
    "opening_minor": "0", "closing_minor": "300000",
    "total_debit_minor": "500000", "total_credit_minor": "200000",
    "rows": [
        {"entry_id": "acme:1", "date": "2026-08-05", "memo": "Cash sale",
         "debit_minor": "500000", "credit_minor": "0", "balance_minor": "500000"},
        {"entry_id": "acme:2", "date": "2026-08-10", "memo": "Pay rent",
         "debit_minor": "0", "credit_minor": "200000", "balance_minor": "300000"},
    ],
}
STATEMENTS = {
    "contract": "financial-statements/1", "period": "2026-08-01..2026-08-31", "currency": "USD",
    "income_statement": {
        "revenue": [{"code": "4000", "name": "Services Income", "amount": 500000}],
        "expenses": [{"code": "6300", "name": "Rent & Lease", "amount": 200000}],
        "total_revenue": 500000, "total_expenses": 200000, "net_income": 300000,
    },
    "balance_sheet": {
        "assets": [{"code": "1000", "name": "Business Checking", "amount": 300000}],
        "liabilities": [], "equity": [{"code": "3900", "name": "Retained Earnings", "amount": 300000}],
        "total_assets": 300000, "total_liabilities": 0, "total_equity": 300000, "balanced": True,
    },
    "cash_flow": {"operating": [], "investing": [], "financing": [],
                  "net_change": 300000, "ending_cash": 300000},
}

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/accounts": (200, ACCOUNTS),
    "GET /t/acme/trial-balance": (200, TRIAL_BALANCE),
    "GET /t/acme/accounts/1000/register": (200, REGISTER),
    "GET /t/acme/statements": (200, STATEMENTS),
    "POST /t/acme/entries": (201, {"tenant": "acme", "entry": {"id": "acme:3"}}),
}


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("3000.00"))
    )


def _app(routes: dict[str, tuple[int, dict[str, Any]]] | None = None,
         *, with_ledger: bool = True) -> tuple[WebApp, FakeTransport, InMemoryAuditLog]:
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
    if with_ledger:
        app.set_ledger(LedgerClient(transport, token="svc-token"))
    return app, transport, audit


def _req(app: WebApp, path: str, sub: str = "u-owner", method: str = "GET", body: str = "") -> Any:
    tok = sign_jwt({"sub": sub, "tenant": "acme", "exp": NOW + 3600}, SECRET)
    headers = {"authorization": f"Bearer {tok}"}
    if method == "POST":
        headers["content-type"] = "application/x-www-form-urlencoded"
    return app.handle(Request(method, path, headers, body))


# --- reads -------------------------------------------------------------------

def test_books_home_shows_trial_balance_and_entry_form() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books")
    assert r.status == 200
    assert "Trial Balance" in r.body
    assert "$3,000.00" in r.body          # cash 300000 minor, formatted exactly
    assert "In balance" in r.body
    assert "Record a transaction" in r.body   # owner may post


def test_chart_of_accounts_lists_the_clients_accounts() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/accounts")
    assert r.status == 200
    assert "Chart of Accounts (3)" in r.body
    assert "Business Checking" in r.body and "1000" in r.body


def test_register_shows_running_balance() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/accounts/1000")
    assert r.status == 200
    assert "Cash sale" in r.body and "Pay rent" in r.body
    assert "$5,000.00" in r.body and "$3,000.00" in r.body


def test_statements_come_from_the_posted_books() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/books/statements?from=2026-08-01&to=2026-08-31")
    assert r.status == 200
    assert "Income Statement" in r.body and "Balance Sheet" in r.body
    assert "Net income" in r.body and "$3,000.00" in r.body
    assert "Balanced" in r.body
    assert any("statements?from=2026-08-01&to=2026-08-31" in c[1] for c in transport.calls)


def test_statements_default_to_the_current_month_when_unspecified() -> None:
    app, transport, _a = _app()
    assert _req(app, "/t/acme/books/statements").status == 200
    call = [c for c in transport.calls if "/statements" in c[1]][0]
    assert "from=2026-08-01" in call[1] and "to=2026-08-31" in call[1]


# --- writes ------------------------------------------------------------------

def test_owner_posts_a_balanced_entry() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/books/entries", method="POST",
             body="date=2026-08-15&memo=Consulting+fee&code1=1000&debit1=1,250.00"
                  "&code2=4000&credit2=1250.00")
    assert r.status == 302
    assert "posted=" in str(r.headers.get("Location", ""))
    post = [c for c in transport.calls if c[0] == "POST"][0]
    payload = json.loads(post[2])
    assert payload["date"] == "2026-08-15"
    assert payload["memo"] == "Consulting fee"
    # exact minor units — "1,250.00" parsed without a float
    assert payload["lines"] == [
        {"code": "1000", "side": "DEBIT", "amount_minor": "125000"},
        {"code": "4000", "side": "CREDIT", "amount_minor": "125000"},
    ]
    assert any(e.action == "journal.posted" for e in audit.events(tenant_id="acme"))


def test_a_viewer_cannot_post_and_sees_no_form() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/books/entries", sub="u-view", method="POST",
             body="date=2026-08-15&code1=1000&debit1=10&code2=4000&credit2=10")
    assert r.status == 403
    assert not [c for c in transport.calls if c[0] == "POST"]
    home = _req(app, "/t/acme/books", sub="u-view")
    assert home.status == 200
    assert "Record a transaction" not in home.body


def test_a_rejected_entry_surfaces_the_ledgers_reason() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/entries"] = (400, {"error": "entry does not balance"})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/entries", method="POST",
             body="date=2026-08-15&code1=1000&debit1=10&code2=4000&credit2=9")
    assert r.status == 302
    assert "err=" in str(r.headers.get("Location", ""))
    assert "balance" in str(r.headers.get("Location", ""))


def test_a_locked_period_is_reported_back_to_the_owner() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/entries"] = (409, {"error": "period 2026-08 is closed"})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/entries", method="POST",
             body="date=2026-08-15&code1=1000&debit1=10&code2=4000&credit2=10")
    assert "closed" in str(r.headers.get("Location", ""))


def test_malformed_amounts_are_refused_before_reaching_the_ledger() -> None:
    app, transport, _a = _app()
    # both a debit and a credit on the same line
    r = _req(app, "/t/acme/books/entries", method="POST",
             body="date=2026-08-15&code1=1000&debit1=10&credit1=10&code2=4000&credit2=10")
    assert "err=" in str(r.headers.get("Location", ""))
    # sub-cent precision
    r2 = _req(app, "/t/acme/books/entries", method="POST",
              body="date=2026-08-15&code1=1000&debit1=10.001&code2=4000&credit2=10")
    assert "err=" in str(r2.headers.get("Location", ""))
    # a single line is not an entry
    r3 = _req(app, "/t/acme/books/entries", method="POST",
              body="date=2026-08-15&code1=1000&debit1=10")
    assert "err=" in str(r3.headers.get("Location", ""))
    assert not [c for c in transport.calls if c[0] == "POST"]


# --- degradation -------------------------------------------------------------

def test_books_degrade_honestly_when_the_ledger_is_unreachable() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/trial-balance"] = (503, {"error": "ledger service unreachable"})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books")
    assert r.status == 200
    assert "Books unavailable" in r.body
    assert "unreachable" in r.body


def test_books_say_so_when_no_ledger_is_configured() -> None:
    app, _t, _a = _app(with_ledger=False)
    r = _req(app, "/t/acme/books")
    assert r.status == 200
    assert "Books unavailable" in r.body


def test_books_nav_appears_for_a_member() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books")
    assert '/t/acme/books"' in r.body  # nav link rendered
