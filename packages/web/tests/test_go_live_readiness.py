"""Two things that stood between a real QuickBooks go-live and a correct screen.

Both were found by signing in to production and reading the cash page, not by
the suite â€” which, between them, they had sailed past for two months.

1. The books were read as of the day the tenant was created.

   Every forecast is anchored to `inputs.opening.as_of`, the owner's saved
   opening date. For an assumptions-only tenant that is right: the opening
   balance is true *on that day*. For a tenant whose books are read live it
   meant the trial balance was read as it stood on 2026-08-24, the result was
   labelled "Cash today" with a POSTED badge, and the 13-week outlook started
   two months in the past. A go-live posts its opening balances dated *today*,
   so after one the page would have shown the pre-go-live balance â€” zero â€” as
   a posted fact.

2. Every company migrated from QuickBooks was told its books couldn't be read.

   The facts reader asked for payroll liabilities in account 2300, the account
   RGNR8's own payroll posts to. A QuickBooks chart has QuickBooks' codes and no
   2300; the ledger answered "unknown account code 2300", and that became a red
   "the books couldn't be read just now" banner on every page load, forever.
"""

from __future__ import annotations

from datetime import date

from factory import _inputs, usd

from rgnr8_web import LedgerClient, Request, WebApp
from rgnr8_web.ledger_client import LedgerResponse
from rgnr8_web.ledger_forecast import read_ledger_facts
from rgnr8_forecast import ForecastConfig

SAVED = date(2026, 8, 3)     # the fixture tenant's saved opening date
TODAY = date(2026, 10, 28)   # the real calendar
CUTOVER = "2026-10-28"       # go-live posts its opening balances today


class _MigratedFromQuickBooks:
    """A ledger after a go-live: QuickBooks' account codes, no 2300, and an
    opening balance posted on the cutover date â€” so a trial balance read
    *before* that date is genuinely empty."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def request(self, method, path, body, headers):
        self.paths.append(path)
        if path.endswith("/accounts"):
            return LedgerResponse(200, {"accounts": [
                {"code": "35", "name": "Checking", "type": "ASSET", "subtype": "BANK"},
                {"code": "33", "name": "Accounts Payable (A/P)", "type": "LIABILITY",
                 "subtype": "ACCOUNTS_PAYABLE"},
                {"code": "1", "name": "Services", "type": "REVENUE"},
            ]})
        if "/trial-balance" in path:
            to = path.split("to=")[1][:10] if "to=" in path else "9999-12-31"
            rows = []
            if to >= CUTOVER:
                rows = [{"code": "35", "debit_minor": "4210000", "credit_minor": "0"}]
            return LedgerResponse(200, {"rows": rows})
        if path.endswith("/invoices") or path.endswith("/bills"):
            return LedgerResponse(200, {"documents": []})
        if "/payroll/liabilities" in path:
            return LedgerResponse(400, {"error": "unknown account code 2300"})
        return LedgerResponse(200, {})


def _app(transport, *, business_date=lambda: TODAY) -> WebApp:
    app = WebApp(business_date=business_date)
    app.add_tenant("acme", "Acme Co", _inputs("20000.00"),
                   ForecastConfig(minimum_cash=usd("10000.00")), token="tok-acme")
    app.set_ledger(LedgerClient(transport, token="t"))
    return app


def _get(app, path: str) -> str:
    return app.handle(Request("GET", path, {"authorization": "Bearer tok-acme"}, "")).body


# --- 1. the books are read as of today --------------------------------------


def test_books_read_live_are_read_as_of_today() -> None:
    t = _MigratedFromQuickBooks()
    _get(_app(t), "/t/acme")
    tb = [p for p in t.paths if "/trial-balance" in p]
    assert tb, "the cash page should read the trial balance"
    assert all(f"to={TODAY.isoformat()}" in p for p in tb), tb


def test_cash_after_a_go_live_shows_the_posted_opening_not_zero() -> None:
    body = _get(_app(_MigratedFromQuickBooks()), "/t/acme")
    assert "$42,100.00" in body
    hero = body[body.index("Cash today"):body.index("Cash today") + 400]
    assert "Posted" in hero
    assert "$0.00" not in hero


def test_the_outlook_starts_today_not_on_the_tenants_creation_date() -> None:
    body = _get(_app(_MigratedFromQuickBooks()), "/t/acme")
    assert f"as of {TODAY.isoformat()}" in body
    assert f"as of {SAVED.isoformat()}" not in body


def test_without_books_the_owners_saved_date_still_anchors_the_outlook() -> None:
    """An assumptions-only tenant's opening balance is true on the day they
    said it was; moving it to today would quietly re-date their figure."""
    app = WebApp(business_date=lambda: TODAY)
    app.add_tenant("acme", "Acme Co", _inputs("20000.00"),
                   ForecastConfig(minimum_cash=usd("10000.00")), token="tok-acme")
    body = _get(app, "/t/acme")
    assert f"as of {SAVED.isoformat()}" in body


def test_with_no_clock_wired_behaviour_is_unchanged() -> None:
    """The default keeps every existing fixture meaningful; production wires
    the real date at the composition root."""
    t = _MigratedFromQuickBooks()
    _get(_app(t, business_date=None), "/t/acme")
    assert any(f"to={SAVED.isoformat()}" in p for p in t.paths if "/trial-balance" in p)


# --- 2. a QuickBooks chart is not an unreadable one -------------------------


def test_a_chart_without_the_payroll_account_is_not_a_read_failure() -> None:
    t = _MigratedFromQuickBooks()
    facts = read_ledger_facts(LedgerClient(t, token="t"), "acme", TODAY)
    assert facts.ok, facts.problems
    assert facts.payroll_liability.minor_units == 0


def test_payroll_liabilities_are_not_even_asked_for_without_the_account() -> None:
    t = _MigratedFromQuickBooks()
    read_ledger_facts(LedgerClient(t, token="t"), "acme", TODAY)
    assert not any("/payroll/liabilities" in p for p in t.paths)


def test_a_migrated_company_sees_no_unreadable_books_banner() -> None:
    body = _get(_app(_MigratedFromQuickBooks()), "/t/acme")
    assert "the books couldn" not in body
    assert "Your books could not be read in full" not in body
