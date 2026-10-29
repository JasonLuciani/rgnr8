"""The Monday answer: does the home screen say how long the cash lasts?

With no costs in the outlook it couldn't (confidence 20/100). The fix reads what
the business actually spent from QuickBooks and adds it in one step (issue 14).
Around it: action cards that name their fix instead of repeating one title
(16), no "0 open item(s)" or version string (17), and a chart summary in words
with the floor/low point/cushion as a quiet line, not tiles (18).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from factory import app_with_two_tenants

from rgnr8_forecast import Money
from rgnr8_web import Request, WebApp
from rgnr8_web.app import _provenance_note
from rgnr8_web.attention import Signals, gather
from rgnr8_web.cost_baseline import CostBaseline, window
from rgnr8_web.ledger_forecast import LedgerFacts
from rgnr8_web.store import InMemoryTenantStore, TenantState

OWNER = {"authorization": "Bearer tok-acme"}


class _QboClient:
    def __init__(self, lines: list[tuple[str, int]]) -> None:
        self.lines = lines
        self.asked: list[tuple[str, str]] = []

    def expense_lines(self, *, start: str, end: str) -> list[tuple[str, int]]:
        self.asked.append((start, end))
        return self.lines


class _Qbo:
    """Just enough of QboConnectService for the costs page."""

    def __init__(self, client: "_QboClient | None") -> None:
        self.client = client

    def api_client(self, tenant_id: str) -> "_QboClient | None":
        return self.client


SPEND = [("Rent", 1_200_000), ("Software", 90_000), ("Wages", 3_600_000), ("Rent", 0)]


def _app(lines: "list[tuple[str, int]] | None" = SPEND, store: Any = None) -> WebApp:
    app = app_with_two_tenants()
    if store is not None:
        app._store = store
    app._qbo = _Qbo(_QboClient(lines) if lines is not None else None)
    return app


def _get(app: WebApp, path: str) -> Any:
    return app.handle(Request("GET", path, OWNER, ""))


def _post(app: WebApp, path: str, body: str) -> Any:
    return app.handle(Request("POST", path, {**OWNER, "content-type":
                                             "application/x-www-form-urlencoded"}, body))


# --- issue 14: a cost baseline from actual spending --------------------------


def test_the_window_is_the_last_three_full_months() -> None:
    assert window(date(2026, 10, 29)) == (date(2026, 7, 1), date(2026, 9, 30))
    assert window(date(2026, 1, 5)) == (date(2025, 10, 1), date(2025, 12, 31))


def test_the_costs_page_offers_the_monthly_average_from_quickbooks() -> None:
    app = _app()
    page = _get(app, "/t/acme/costs").body
    # (12,000 + 900 + 36,000) / 3 months = 16,300 a month
    assert "$16,300.00 a month" in page
    assert "Add $16,300.00 a month to my outlook" in page
    assert page.index("Wages") < page.index("Rent") < page.index("Software")  # largest first


def test_adding_it_is_one_step_and_the_outlook_then_has_costs() -> None:
    app = _app()
    before = app._forecast(app._tenants["acme"])
    r = _post(app, "/t/acme/costs", "action=add")
    assert r.status == 302 and dict(r.extra_headers)["Location"] == "/t/acme"
    after = app._forecast(app._tenants["acme"])
    labels = [i.label for i in app._effective_inputs(app._tenants["acme"]).recurring]
    assert any(label.startswith("Running costs (average of Jul") for label in labels)
    assert after.projection.total_outflows > before.projection.total_outflows


def test_the_amount_is_read_again_from_quickbooks_not_taken_from_the_form() -> None:
    app = _app()
    _post(app, "/t/acme/costs", "action=add&monthly_minor=1")
    saved = CostBaseline.from_dict(app._store.load("acme").cost_baseline)
    assert saved is not None and saved.monthly == Money(1_630_000)


def test_a_baseline_added_on_one_worker_counts_on_the_other() -> None:
    store = InMemoryTenantStore()
    worker_a, worker_b = _app(store=store), _app(store=store)
    worker_b._forecast(worker_b._tenants["acme"])  # B has its state loaded already
    _post(worker_a, "/t/acme/costs", "action=add")
    labels = [i.label for i in worker_b._effective_inputs(worker_b._tenants["acme"]).recurring]
    assert any(label.startswith("Running costs") for label in labels)


def test_it_can_be_taken_back_out() -> None:
    app = _app()
    _post(app, "/t/acme/costs", "action=add")
    assert "Take it out of the outlook" in _get(app, "/t/acme/costs").body
    _post(app, "/t/acme/costs", "action=remove")
    assert app._store.load("acme").cost_baseline is None
    assert "Add $16,300.00 a month" in _get(app, "/t/acme/costs").body


def test_without_quickbooks_or_spending_it_says_why_and_adds_nothing() -> None:
    page = _get(_app(lines=None), "/t/acme/costs").body
    assert "Connect QuickBooks first" in page and "to my outlook" not in page
    quiet = _app(lines=[])
    assert "shows no spending from Jul to Sep 2026" in _get(quiet, "/t/acme/costs").body
    _post(quiet, "/t/acme/costs", "action=add")
    assert quiet._store.load("acme").cost_baseline is None


def test_a_garbled_saved_baseline_is_ignored_not_a_crash() -> None:
    store = InMemoryTenantStore()
    store.save("acme", TenantState(cost_baseline={"monthly_minor": "lots"}))
    app = _app(store=store)
    assert _get(app, "/t/acme").status == 200


# --- issue 16: each card names its fix ---------------------------------------


def _signals(notes: tuple[str, ...]) -> Signals:
    return Signals(tenant="acme", floor_breached=False, breach_weeks_until=None,
                   breach_shortfall=None, primary_action="", ledger_problems=(),
                   data_quality=notes, books_connected=True, review_count=0, overdue_count=0,
                   overdue_total=None, worst_customer="", worst_days=0, close_period="",
                   close_overdue=0, close_blocked=0)


def test_data_quality_cards_have_their_own_titles_and_fixes() -> None:
    items = gather(_signals((
        "no costs in the outlook — nothing is set to leave the account",
        "opening cash is not yet verified/reconciled",
        "no receivables provided — inflows may be understated",
    )))
    titles = [i.headline for i in items]
    assert len(titles) == len(set(titles)), titles
    assert "Worth a second look at the numbers" not in titles
    by_title = {i.headline: i for i in items}
    assert by_title["No costs in your outlook yet"].href == "/t/acme/costs"
    assert by_title["Cash today hasn't been checked against the bank"].href == "/t/acme/books/reconcile"
    assert by_title["No open invoices in the outlook"].href == "/t/acme/invoices"


def test_an_unknown_note_is_shown_as_itself() -> None:
    (item,) = gather(_signals(("3 customer(s) have no payment history; default timing used",
                               "something new the forecast learned to say")))[:1]
    assert item.headline == "Payment timing is a guess for 3 customers"
    assert gather(_signals(("something new",)))[0].headline == "Something new"


# --- issue 17: no "0 open item(s)", no version string -------------------------


def test_the_books_banner_counts_in_plain_words() -> None:
    facts = LedgerFacts(cash=Money(0), cash_accounts=1)
    assert "0 open item" not in _provenance_note(facts, {"from_the_books": 0, "assumed": 0})
    assert "Cash on hand comes straight from your books." in _provenance_note(
        facts, {"from_the_books": 0, "assumed": 0})
    assert "Cash on hand and 1 open item come" in _provenance_note(facts, {"from_the_books": 1})
    assert "2 planned items are your own assumptions" in _provenance_note(
        facts, {"from_the_books": 3, "assumed": 2})


def test_the_home_screen_shows_no_version_string() -> None:
    page = _get(_app(), "/t/acme").body
    assert "· version" not in page and "+base." not in page


# --- issue 18: the chart in words; supporting figures stay quiet --------------


def test_the_chart_has_a_summary_in_words_and_the_tiles_are_one_line() -> None:
    page = _get(_app(), "/t/acme").body
    assert 'id="cash-chart-summary"' in page and 'aria-describedby="cash-chart-summary"' in page
    assert "Cash starts at $20,000.00 today, reaches its lowest point of" in page
    assert '<div class="tiles">' not in page
    # The floor and the room to spare are in that same sentence, so the low
    # point is said once (it used to be repeated in a line of facts and in the
    # cash card's headline).
    assert re.search(r"It first falls below your \$10,000\.00 floor in week 1 "
                     r"\(around Aug 7\), short by up to \$124,200\.00\.", page)
    assert page.count("lowest point") == 1 and "Projected low point" not in page


def test_the_page_only_says_there_are_no_costs_when_there_are_none() -> None:
    from rgnr8_forecast import ForecastInputs

    app = _app()
    assert "already has some costs in it" in _get(app, "/t/acme/costs").body  # rent + payroll
    t = app._tenants["acme"]
    t.inputs = ForecastInputs(opening=t.inputs.opening)
    page = _get(app, "/t/acme/costs").body
    assert "Your outlook has no costs in it" in page and "already has some costs" not in page


def test_the_outlook_sentence_says_what_the_floor_means_in_each_case() -> None:
    from types import SimpleNamespace

    from rgnr8_web.screens import _chart_summary

    def proj(low: str, *, breached: bool = False) -> Any:
        return SimpleNamespace(
            opening_available=Money.from_decimal("20000.00"), ending_balance=Money.from_decimal("18000.00"),
            effective_floor=Money.from_decimal("10000.00"), weeks=[None] * 13,
            trough=SimpleNamespace(balance=Money.from_decimal(low), on_date=date(2026, 9, 1)),
            breach=SimpleNamespace(breached=breached, first_date=None, weeks_until=None,
                                   worst_shortfall=None))

    held = _chart_summary(proj("14640.00"))
    assert held == ("Cash starts at $20,000.00 today, reaches its lowest point of $14,640.00 on "
                    "Sep 1, and ends the 13 weeks at $18,000.00. It stays above your $10,000.00 "
                    "floor for all 13 weeks, with $4,640.00 to spare at the low point.")
    # With nothing spent, "stays above the floor" would be true and meaningless.
    assert "no costs are in the outlook" in _chart_summary(proj("20000.00"), has_costs=False)
    assert "lowest point" not in _chart_summary(proj("20000.00"), has_costs=False)
    assert "below your $10,000.00 minimum-cash floor" in _chart_summary(
        proj("5000.00", breached=True))
