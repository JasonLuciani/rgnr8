"""The hybrid forecast: facts from the books, assumptions from the owner.

RGNR8 held two pictures of the same business that could disagree with no way to
tell which was wrong. These tests pin the seam between them: what the books
settle is taken from the books; what only a person can know is left alone; and
neither is ever silently substituted for the other.
"""

from datetime import date
from typing import Any

from rgnr8_forecast import (
    Bill, CashPosition, Category, Confidence, Direction, ForecastConfig,
    ForecastInputs, Invoice, Money, OneTimeItem, PipelineOpportunity, Provenance,
)
from rgnr8_web import (
    InMemoryUserDirectory, JwtAuthenticator, LedgerClient, LedgerResponse,
    Request, Role, User, WebApp, sign_jwt,
)
from rgnr8_web.ledger_forecast import (
    forecast_from_ledger, merge_forecast_inputs, provenance_split, read_ledger_facts,
)

SECRET = "hybrid-secret"
NOW = 1_760_000_000
AS_OF = date(2026, 8, 31)


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
        {"code": "1000", "name": "Business Checking", "type": "ASSET", "subtype": "BANK"},
        {"code": "1010", "name": "Savings", "type": "ASSET", "subtype": "CASH"},
        {"code": "1200", "name": "Accounts Receivable", "type": "ASSET",
         "subtype": "ACCOUNTS_RECEIVABLE"},
        {"code": "4100", "name": "Consulting Income", "type": "REVENUE"},
    ],
}

TRIAL_BALANCE = {
    "currency": "USD", "in_balance": True,
    "total_debit_minor": "0", "total_credit_minor": "0",
    "rows": [
        {"code": "1000", "name": "Business Checking", "type": "ASSET",
         "debit_minor": "1225100", "credit_minor": "0"},
        {"code": "1010", "name": "Savings", "type": "ASSET",
         "debit_minor": "500000", "credit_minor": "0"},
        {"code": "1200", "name": "Accounts Receivable", "type": "ASSET",
         "debit_minor": "800000", "credit_minor": "0"},
        {"code": "4100", "name": "Consulting Income", "type": "REVENUE",
         "debit_minor": "0", "credit_minor": "2000000"},
    ],
}

INVOICES = {
    "documents": [
        {"id": "INV-1", "party_id": "halcyon", "date": "2026-08-01",
         "due_date": "2026-09-15", "total_minor": "800000", "open_minor": "800000",
         "status": "OPEN"},
        {"id": "INV-0", "party_id": "old", "date": "2026-06-01",
         "due_date": "2026-07-01", "total_minor": "100000", "open_minor": "0",
         "status": "PAID"},
    ],
}

BILLS = {
    "documents": [
        {"id": "BILL-1", "party_id": "copyshop", "date": "2026-08-04",
         "due_date": "2026-09-04", "total_minor": "34999", "open_minor": "34999",
         "status": "OPEN"},
    ],
}

LIABILITIES = {"account_code": "2300", "owed_minor": "251200",
               "posted_runs": 1, "draft_runs": 0}

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/accounts": (200, ACCOUNTS),
    "GET /t/acme/trial-balance": (200, TRIAL_BALANCE),
    "GET /t/acme/invoices": (200, INVOICES),
    "GET /t/acme/bills": (200, BILLS),
    "GET /t/acme/payroll/liabilities": (200, LIABILITIES),
}


def _client(routes: dict[str, tuple[int, dict[str, Any]]] | None = None) -> LedgerClient:
    return LedgerClient(FakeTransport(routes if routes is not None else dict(DEFAULT_ROUTES)))


def _assumptions() -> ForecastInputs:
    """What an owner typed: a hoped-for deal, a planned purchase, and — wrongly —
    an invoice and a bank balance they also have in the books."""
    return ForecastInputs(
        opening=CashPosition(as_of=AS_OF, available=Money.from_decimal("999.99")),
        invoices=(Invoice(
            id="TYPED-1", customer_id="halcyon", issue_date=date(2026, 8, 1),
            due_date=date(2026, 9, 15), open_amount=Money.from_decimal("8000.00"),
        ),),
        bills=(Bill(
            id="TYPED-BILL", vendor_id="copyshop", due_date=date(2026, 9, 4),
            amount=Money.from_decimal("349.99"),
        ),),
        pipeline=(PipelineOpportunity(
            label="Prospect retainer", customer_id="prospect",
            amount=Money.from_decimal("15000.00"),
            expected_date=date(2026, 9, 20), probability_bps=6000,
        ),),
        one_time=(OneTimeItem(
            label="New oven", category=Category.CAPITAL_EXPENDITURE,
            direction=Direction.OUTFLOW, amount=Money.from_decimal("4000.00"),
            on_date=date(2026, 9, 10), confidence=Confidence.PLANNED,
        ),),
    )


# --- reading the facts -------------------------------------------------------

def test_cash_is_the_bank_and_cash_accounts_only() -> None:
    facts = read_ledger_facts(_client(), "acme", AS_OF)
    # checking 12,251 + savings 5,000 — receivables are NOT cash
    assert facts.cash.to_decimal_string() == "17251.00"
    assert facts.cash_accounts == 2
    assert facts.ok


def test_only_open_documents_become_forecast_items() -> None:
    facts = read_ledger_facts(_client(), "acme", AS_OF)
    assert [i.id for i in facts.invoices] == ["INV-1"], "the paid one is gone"
    assert facts.invoices[0].open_amount.to_decimal_string() == "8000.00"
    assert [b.id for b in facts.bills] == ["BILL-1"]


def test_every_fact_is_stamped_as_coming_from_the_books() -> None:
    facts = read_ledger_facts(_client(), "acme", AS_OF)
    assert facts.invoices[0].provenance is not None
    assert facts.invoices[0].provenance.source_system == "ledger"
    assert facts.bills[0].provenance.source_system == "ledger"


def test_payroll_liability_is_treated_as_money_already_owed() -> None:
    facts = read_ledger_facts(_client(), "acme", AS_OF)
    assert facts.payroll_liability.to_decimal_string() == "2512.00"
    merged = merge_forecast_inputs(_assumptions(), facts, AS_OF)
    payroll = [i for i in merged.one_time
               if i.provenance and i.provenance.source_type == "payroll-liability"]
    assert len(payroll) == 1
    assert payroll[0].amount.to_decimal_string() == "2512.00"
    assert payroll[0].direction == Direction.OUTFLOW


def test_a_broken_ledger_is_reported_rather_than_read_as_zero() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/invoices"] = (503, {"error": "connection refused"})
    facts = read_ledger_facts(_client(routes), "acme", AS_OF)
    assert not facts.ok
    assert any("invoices" in p for p in facts.problems)


# --- merging -----------------------------------------------------------------

def test_the_books_replace_receivables_rather_than_adding_to_them() -> None:
    """A typed invoice and the real one are the same money. Merging would
    double-count it, which is the failure this whole change exists to stop."""
    facts = read_ledger_facts(_client(), "acme", AS_OF)
    merged = merge_forecast_inputs(_assumptions(), facts, AS_OF)
    assert [i.id for i in merged.invoices] == ["INV-1"]
    assert [b.id for b in merged.bills] == ["BILL-1"]


def test_opening_cash_comes_from_the_books_not_the_typed_figure() -> None:
    facts = read_ledger_facts(_client(), "acme", AS_OF)
    merged = merge_forecast_inputs(_assumptions(), facts, AS_OF)
    assert merged.opening.available.to_decimal_string() == "17251.00"
    assert merged.opening.verified is True


def test_forward_looking_assumptions_are_left_exactly_alone() -> None:
    facts = read_ledger_facts(_client(), "acme", AS_OF)
    merged = merge_forecast_inputs(_assumptions(), facts, AS_OF)
    assert [p.label for p in merged.pipeline] == ["Prospect retainer"], "no ledger knows this"
    planned = [i for i in merged.one_time if i.label == "New oven"]
    assert len(planned) == 1
    assert planned[0].amount.to_decimal_string() == "4000.00"


def test_an_unreachable_ledger_leaves_the_assumptions_untouched() -> None:
    """Stale assumptions are wrong. Silently-zeroed facts are worse."""
    routes = {k: (503, {"error": "down"}) for k in DEFAULT_ROUTES}
    facts = read_ledger_facts(_client(routes), "acme", AS_OF)
    merged = merge_forecast_inputs(_assumptions(), facts, AS_OF)
    assert merged.opening.available.to_decimal_string() == "999.99"
    assert [i.id for i in merged.invoices] == ["TYPED-1"]


def test_the_split_says_how_much_is_known_versus_guessed() -> None:
    merged, _facts = forecast_from_ledger(_client(), "acme", _assumptions(), AS_OF)
    split = provenance_split(merged)
    # invoice + bill + payroll liability
    assert split["from_the_books"] == 3
    # the pipeline deal and the planned oven
    assert split["assumed"] == 2


def test_re_reading_is_idempotent_and_does_not_stack_payroll() -> None:
    client = _client()
    once, facts = forecast_from_ledger(client, "acme", _assumptions(), AS_OF)
    twice, _ = forecast_from_ledger(client, "acme", once, AS_OF)
    payroll = [i for i in twice.one_time
               if i.provenance and i.provenance.source_type == "payroll-liability"]
    assert len(payroll) == 1, "the liability appears once, not once per refresh"
    assert len(twice.invoices) == len(once.invoices)


# --- through the app ---------------------------------------------------------

def _app(routes: dict[str, tuple[int, dict[str, Any]]] | None = None,
         *, with_ledger: bool = True) -> WebApp:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users)
    app.add_tenant("acme", "Acme Co", _assumptions(),
                   ForecastConfig(minimum_cash=Money.from_decimal("1000.00")), token="unused")
    if with_ledger:
        app.set_ledger(_client(routes))
    return app


def _req(app: WebApp, path: str) -> Any:
    tok = sign_jwt({"sub": "u-owner", "tenant": "acme", "exp": NOW + 3600}, SECRET)
    return app.handle(Request("GET", path, {"authorization": f"Bearer {tok}"}, ""))


def test_the_cash_screen_shows_the_books_balance_and_says_where_it_came_from() -> None:
    r = _req(_app(), "/t/acme")
    assert r.status == 200
    assert "$17,251.00" in r.body
    assert "come straight from your books" in r.body
    assert "your assumptions" in r.body


def test_the_cash_screen_says_plainly_when_the_books_are_unreachable() -> None:
    routes = {k: (503, {"error": "down"}) for k in DEFAULT_ROUTES}
    r = _req(_app(routes), "/t/acme")
    assert r.status == 200
    assert "Working from your saved assumptions" in r.body
    assert "$999.99" in r.body


def test_without_a_ledger_the_forecast_is_the_owners_alone() -> None:
    r = _req(_app(with_ledger=False), "/t/acme")
    assert r.status == 200
    assert "$999.99" in r.body
    assert "come straight from your books" not in r.body


def test_the_forecast_reflects_a_change_in_the_books_immediately() -> None:
    """A cached forecast would show yesterday's cash the moment after a posting."""
    routes = dict(DEFAULT_ROUTES)
    app = _app(routes)
    assert "$17,251.00" in _req(app, "/t/acme").body

    moved = dict(TRIAL_BALANCE)
    moved["rows"] = [
        dict(r, debit_minor="2225100") if r["code"] == "1000" else r
        for r in TRIAL_BALANCE["rows"]  # type: ignore[union-attr]
    ]
    routes["GET /t/acme/trial-balance"] = (200, moved)
    assert "$27,251.00" in _req(app, "/t/acme").body
