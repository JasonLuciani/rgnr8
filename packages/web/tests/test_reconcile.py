"""The owner-facing bank reconciliation screens, against a fake ledger transport.

These cover the routing, permission gate, and exact money handling on the web
side. The reconciliation *rules* (what ties out, what may be finished) live in
the ledger service and are tested there.
"""

from datetime import date
from typing import Any

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryAuditLog, InMemoryUserDirectory, JwtAuthenticator, LedgerClient,
    LedgerResponse, Request, Role, User, WebApp, sign_jwt,
)

SECRET = "recon-secret"
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
    "tenant": "acme",
    "accounts": [
        {"code": "1000", "name": "Business Checking", "type": "ASSET", "subtype": "BANK"},
        {"code": "1010", "name": "Savings", "type": "ASSET", "subtype": "CASH"},
        {"code": "4000", "name": "Services Income", "type": "REVENUE", "subtype": "INCOME"},
        {"code": "6300", "name": "Rent & Lease", "type": "EXPENSE", "subtype": "EXPENSE"},
    ],
}

VIEW = {
    "account_code": "1000", "account_name": "Business Checking",
    "statement_date": "2026-08-31", "statement_balance_minor": "300000",
    "reconciled_through": None,
    "reconciled_balance_minor": "0", "cleared_this_session_minor": "0",
    "cleared_balance_minor": "0", "difference_minor": "300000", "can_finish": False,
    "lines": [
        {"entry_id": "acme:1", "date": "2026-08-05", "memo": "Cash sale",
         "amount_minor": "500000", "status": "UNCLEARED"},
        {"entry_id": "acme:2", "date": "2026-08-10", "memo": "Pay rent",
         "amount_minor": "-200000", "status": "UNCLEARED"},
    ],
}

BALANCED_VIEW = dict(
    VIEW,
    cleared_this_session_minor="300000",
    cleared_balance_minor="300000",
    difference_minor="0",
    can_finish=True,
    lines=[dict(line, status="CLEARED") for line in VIEW["lines"]],  # type: ignore[arg-type]
)

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/accounts": (200, ACCOUNTS),
    "GET /t/acme/trial-balance": (
        200,
        {"tenant": "acme", "currency": "USD", "in_balance": True,
         "total_debit_minor": "0", "total_credit_minor": "0", "rows": []},
    ),
    "GET /t/acme/accounts/1000/reconcile": (200, VIEW),
    "POST /t/acme/accounts/1000/reconcile/toggle": (200, BALANCED_VIEW),
    "POST /t/acme/accounts/1000/reconcile/finish": (
        200,
        {"account_code": "1000", "statement_date": "2026-08-31",
         "reconciled_entries": 2, "reconciled_through": "2026-08-31"},
    ),
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


# --- picking an account ------------------------------------------------------

def test_only_bank_and_cash_accounts_are_offered_for_reconciliation() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/reconcile")
    assert r.status == 200
    assert "Business Checking" in r.body and "Savings" in r.body
    assert "Services Income" not in r.body and "Rent &amp; Lease" not in r.body


def test_books_home_links_to_reconciliation() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books")
    assert "/t/acme/books/reconcile" in r.body


# --- the worksheet -----------------------------------------------------------

def test_the_worksheet_shows_the_lines_and_the_difference() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/books/reconcile/1000?statement_date=2026-08-31"
                  "&statement_balance=3,000.00")
    assert r.status == 200
    assert "Reconcile 1000" in r.body
    assert "Cash sale" in r.body and "Pay rent" in r.body
    assert "$5,000.00" in r.body and "-$2,000.00" in r.body
    assert "$3,000.00" in r.body            # the difference, still open
    assert "Finish reconciliation" not in r.body
    assert "never" in r.body                # never reconciled before
    # "3,000.00" reached the service as exact minor units, never a float
    call = [c for c in transport.calls if "/reconcile" in c[1]][0]
    assert "statement_balance_minor=300000" in call[1]
    assert "statement_date=2026-08-31" in call[1]


def test_a_balanced_worksheet_offers_the_finish_button() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/accounts/1000/reconcile"] = (200, BALANCED_VIEW)
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/reconcile/1000?statement_date=2026-08-31"
                  "&statement_balance=3000.00")
    assert "Finish reconciliation" in r.body
    assert "Ticked" in r.body


def test_a_malformed_statement_balance_is_reported_not_guessed() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/reconcile/1000?statement_date=2026-08-31"
                  "&statement_balance=three+thousand")
    assert r.status == 200
    assert "not a valid amount" in r.body


def test_the_worksheet_surfaces_a_ledger_refusal() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/accounts/1000/reconcile"] = (400, {"error": "unknown account code 1000"})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/reconcile/1000?statement_date=2026-08-31")
    assert "unknown account code 1000" in r.body


# --- ticking and finishing ---------------------------------------------------

def test_ticking_a_line_calls_the_service_and_returns_to_the_worksheet() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/books/reconcile/1000/toggle", method="POST",
             body="entry_id=acme:1&cleared=1&statement_date=2026-08-31"
                  "&statement_balance_minor=300000")
    assert r.status == 302
    loc = str(r.headers.get("Location", ""))
    assert loc.startswith("/t/acme/books/reconcile/1000?")
    assert "statement_balance_minor=300000" in loc
    post = [c for c in transport.calls if c[0] == "POST"][0]
    assert '"entry_id": "acme:1"' in post[2] and '"cleared": true' in post[2]


def test_unticking_sends_cleared_false() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/books/reconcile/1000/toggle", method="POST",
         body="entry_id=acme:1&cleared=0&statement_date=2026-08-31"
              "&statement_balance_minor=300000")
    post = [c for c in transport.calls if c[0] == "POST"][0]
    assert '"cleared": false' in post[2]


def test_finishing_records_an_audit_event_and_says_how_many_cleared() -> None:
    app, _t, audit = _app()
    r = _req(app, "/t/acme/books/reconcile/1000/finish", method="POST",
             body="statement_date=2026-08-31&statement_balance_minor=300000")
    assert r.status == 302
    loc = str(r.headers.get("Location", ""))
    assert "done=" in loc and "Reconciled%202%20transactions" in loc
    assert any(e.action == "bank.reconciled" for e in audit.events(tenant_id="acme"))


def test_a_refused_finish_surfaces_the_reason_and_logs_nothing() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/accounts/1000/reconcile/finish"] = (
        400, {"error": "reconciliation is off by $50.00"}
    )
    app, _t, audit = _app(routes)
    r = _req(app, "/t/acme/books/reconcile/1000/finish", method="POST",
             body="statement_date=2026-08-31&statement_balance_minor=300000")
    assert "err=" in str(r.headers.get("Location", ""))
    assert "off%20by" in str(r.headers.get("Location", ""))
    assert not [e for e in audit.events(tenant_id="acme") if e.action == "bank.reconciled"]


# --- permissions -------------------------------------------------------------

def test_a_viewer_can_look_but_not_tick() -> None:
    app, transport, _a = _app()
    page = _req(app, "/t/acme/books/reconcile/1000?statement_date=2026-08-31"
                     "&statement_balance=3000.00", sub="u-view")
    assert page.status == 200
    assert "Cash sale" in page.body
    assert "reconcile/1000/toggle" not in page.body
    assert "Finish reconciliation" not in page.body

    blocked = _req(app, "/t/acme/books/reconcile/1000/toggle", sub="u-view", method="POST",
                   body="entry_id=acme:1&cleared=1&statement_date=2026-08-31"
                        "&statement_balance_minor=300000")
    assert blocked.status == 403
    assert not [c for c in transport.calls if c[0] == "POST"]


def test_reconciled_lines_render_locked_and_offer_no_tick() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/accounts/1000/reconcile"] = (
        200,
        dict(VIEW, reconciled_through="2026-07-31", reconciled_balance_minor="500000",
             lines=[dict(VIEW["lines"][0], status="RECONCILED")]),  # type: ignore[index]
    )
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/reconcile/1000?statement_date=2026-08-31"
                  "&statement_balance=3000.00")
    assert "Reconciled" in r.body
    assert "2026-07-31" in r.body
    assert "reconcile/1000/toggle" not in r.body
