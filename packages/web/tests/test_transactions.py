"""The bank transactions register: rendering, summary, and role-gated categorize."""

from datetime import date

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    BankTransaction,
    InMemoryUserDirectory,
    JwtAuthenticator,
    Request,
    Role,
    User,
    WebApp,
    render_transactions,
    sign_jwt,
    summarize,
)

SECRET = "txn-secret"
NOW = 1_760_000_000


def _feed() -> list[BankTransaction]:
    return [
        BankTransaction("t1", "2026-08-15", "Deposit — Northwind client", Money.from_decimal("13210.55"),
                        category="Sales income", status="matched", counterparty="Northwind LLC"),
        BankTransaction("t2", "2026-08-16", "GUSTO PAYROLL", Money.from_decimal("-18000.00"),
                        category="Payroll", status="matched"),
        BankTransaction("t3", "2026-08-17", "SQ *COFFEE SUPPLY", Money.from_decimal("-142.30"),
                        category="Uncategorized", status="review"),
        BankTransaction("t4", "2026-08-18", "CHECK 1042", Money.from_decimal("-2500.00"),
                        category="Uncategorized", status="unmatched"),
    ]


def test_summary_counts_and_totals() -> None:
    s = summarize(_feed())
    assert s.total == 4
    assert s.matched == 2
    assert s.review == 1        # the uncategorized card charge
    assert s.unmatched == 1     # the check not yet in the books
    assert s.inflow == Money.from_decimal("13210.55")
    assert s.outflow == Money.from_decimal("-20642.30")


def test_render_leads_with_review_queue() -> None:
    html = render_transactions("acme", "Checking", _feed(), can_categorize=True)
    assert "For review" in html
    assert "SQ *COFFEE SUPPLY" in html and "GUSTO PAYROLL" in html
    assert "13,210.55" in html
    assert "categorize(this)" in html  # interactive when allowed
    # read-only render has no controls
    ro = render_transactions("acme", "Checking", _feed(), can_categorize=False)
    assert "categorize(this)" not in ro
    assert "http://" not in html and "https://" not in html  # self-contained


def _app() -> WebApp:
    users = InMemoryUserDirectory()
    users.upsert_user(User("book@acme.com", "book@acme.com", "Ben Books"))
    users.set_membership("book@acme.com", "acme", Role.BOOKKEEPER)
    users.upsert_user(User("view@acme.com", "view@acme.com", "Val Viewer"))
    users.set_membership("view@acme.com", "acme", Role.VIEWER)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users)
    app.add_tenant("acme", "Acme Co",
                   ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("50000.00"))),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    app.add_transactions("acme", _feed(), account_name="Checking")
    return app


def _tok(sub: str) -> str:
    return sign_jwt({"sub": sub, "tenant": "acme", "exp": NOW + 3600}, SECRET)


def test_bookkeeper_can_view_and_categorize() -> None:
    app = _app()
    h = {"authorization": f"Bearer {_tok('book@acme.com')}"}
    page = app.handle(Request("GET", "/t/acme/transactions", h))
    assert page.status == 200 and "Bank transactions" in page.body
    # categorize the uncategorized card charge → it becomes matched
    r = app.handle(Request("POST", "/api/acme/transactions", {**h, "content-type": "application/json"},
                           '{"id":"t3","category":"Software & SaaS"}'))
    assert r.status == 200
    import json
    body = json.loads(r.body)
    assert body["category"] == "Software & SaaS" and body["status"] == "matched"


def test_viewer_can_read_but_not_categorize() -> None:
    app = _app()
    h = {"authorization": f"Bearer {_tok('view@acme.com')}"}
    assert app.handle(Request("GET", "/t/acme/transactions", h)).status == 200  # read allowed
    r = app.handle(Request("POST", "/api/acme/transactions", {**h, "content-type": "application/json"},
                           '{"id":"t3","category":"Payroll"}'))
    assert r.status == 403  # viewer lacks categorize_transactions


def test_accept_moves_a_review_line_to_matched() -> None:
    app = _app()
    h = {"authorization": f"Bearer {_tok('book@acme.com')}", "content-type": "application/json"}
    r = app.handle(Request("POST", "/api/acme/transactions", h, '{"id":"t4","accept":true}'))
    import json
    assert json.loads(r.body)["status"] == "matched"
