"""Cash that could not be determined is not cash of zero.

The ledger-outage work held one rule:

    the owner may be shown figures computed without their books,
    but never without being told that is what they are looking at.

That rule was enforced only for the *loud* failure — a service that raises, or
answers 5xx. This file is about the quiet one. A ledger that answers every call
perfectly, whose chart of accounts simply holds no bank or cash account — the
state of a QuickBooks company part-way through its first import, and the state
the audit's finding 6 describes — reported `cash=Money(0)` and recorded no
problem at all. `facts.ok` was therefore True, the fallback in
`merge_forecast_inputs` did not fire, and the owner's own saved opening balance
was replaced on the product's flagship screen by

    Cash today   $0.00

Not an error page, not a banner: a confident zero, in the largest type on the
page, on the one number the product exists to show.

"We could not work out your cash" and "you have no cash" are different
sentences and only one of them is true here. This is `attention.py`'s rule 2 —
*not knowing is not the same as nothing* — applied to money instead of work,
which is what Gate 2 item 11 asks for.
"""

from __future__ import annotations

import re
from datetime import date

from factory import _inputs, app_with_two_tenants

from rgnr8_web import LedgerClient, Request
from rgnr8_web.ledger_client import LedgerResponse
from rgnr8_web.ledger_forecast import merge_forecast_inputs, read_ledger_facts

TOKEN = "super-secret-service-token"
AS_OF = date(2026, 8, 3)


class _NoBankAccounts:
    """A ledger answering correctly about a chart that holds no cash account.

    Every call succeeds. Nothing is broken. There is simply nothing to read the
    cash from — which is a different thing from reading it and finding zero.
    """

    def request(self, method, path, body, headers):
        if "/accounts" in path:
            # Income and expense only: a part-migrated chart.
            return LedgerResponse(200, {"accounts": [
                {"code": "4000", "name": "Sales", "subtype": "INCOME"},
                {"code": "6000", "name": "Rent", "subtype": "EXPENSE"},
            ]})
        if "/trial-balance" in path:
            return LedgerResponse(200, {"rows": [
                {"code": "4000", "debit_minor": "0", "credit_minor": "150000"},
                {"code": "6000", "debit_minor": "150000", "credit_minor": "0"},
            ]})
        if path.endswith("/invoices") or path.endswith("/bills"):
            return LedgerResponse(200, {"documents": []})
        if "/payroll/liabilities" in path:
            return LedgerResponse(200, {"owed_minor": "0"})
        return LedgerResponse(200, {})


class _WithBankAccount(_NoBankAccounts):
    """The same ledger once a bank account exists — the control case."""

    def request(self, method, path, body, headers):
        if "/accounts" in path:
            return LedgerResponse(200, {"accounts": [
                {"code": "1000", "name": "Checking", "subtype": "BANK"},
            ]})
        if "/trial-balance" in path:
            return LedgerResponse(200, {"rows": [
                {"code": "1000", "debit_minor": "3450025", "credit_minor": "0"},
            ]})
        return super().request(method, path, body, headers)


def _app(transport) -> object:
    app = app_with_two_tenants()
    app.set_ledger(LedgerClient(transport, token=TOKEN))
    return app


def _get(app, path: str):
    return app.handle(Request("GET", path, {"authorization": "Bearer tok-acme"}, ""))


def _hero(body: str) -> str:
    """The markup immediately around the one figure the screen is about."""
    i = body.index("Cash today")
    return body[i:i + 400]


# --- what the books actually said -------------------------------------------


def test_a_chart_with_no_cash_account_is_recorded_as_a_problem() -> None:
    """`cash_accounts == 0` has to reach `facts.ok`, or every caller has to
    remember to check it separately — and not one of them did."""
    facts = read_ledger_facts(
        LedgerClient(_NoBankAccounts(), token=TOKEN), "acme", AS_OF)
    assert facts.cash_accounts == 0
    assert not facts.ok, "a chart with nothing to read cash from is not a clean read"
    assert any("bank" in p.lower() or "cash" in p.lower() for p in facts.problems)


def test_a_chart_with_a_bank_account_is_still_a_clean_read() -> None:
    facts = read_ledger_facts(
        LedgerClient(_WithBankAccount(), token=TOKEN), "acme", AS_OF)
    assert facts.cash_accounts == 1
    assert facts.ok
    assert facts.cash.minor_units == 3450025


# --- what the forecast was built on -----------------------------------------


def test_the_owners_opening_stands_when_cash_could_not_be_determined() -> None:
    """The fallback was gated on `not facts.ok`, and a clean read of a chart
    with nothing in it is `ok`. So the owner's $20,000 became $0.00."""
    merged = merge_forecast_inputs(
        _inputs("20000.00"),
        read_ledger_facts(LedgerClient(_NoBankAccounts(), token=TOKEN), "acme", AS_OF),
        AS_OF,
    )
    assert merged.opening.available.minor_units == 2000000


def test_a_real_balance_still_replaces_the_owners_guess() -> None:
    """The fix must not turn every figure into an assumption."""
    merged = merge_forecast_inputs(
        _inputs("20000.00"),
        read_ledger_facts(LedgerClient(_WithBankAccount(), token=TOKEN), "acme", AS_OF),
        AS_OF,
    )
    assert merged.opening.available.minor_units == 3450025
    assert merged.opening.verified


# --- what the owner sees ----------------------------------------------------


def test_the_cash_page_does_not_report_zero_when_it_does_not_know() -> None:
    body = _get(_app(_NoBankAccounts()), "/t/acme").body
    assert "$20,000.00" in body, "the owner's own opening should stand"
    assert "$0.00" not in _hero(body)


def test_the_cash_page_says_why_the_figure_is_not_from_the_books() -> None:
    body = _get(_app(_NoBankAccounts()), "/t/acme").body
    assert "Working from your saved assumptions" in body


def test_the_queue_leads_with_the_books_not_being_readable() -> None:
    body = _get(_app(_NoBankAccounts()), "/t/acme").body
    heads = [re.sub(r"<[^>]+>", "", m).strip()
             for m in re.findall(r'class="h">(.*?)</div>', body, re.S)]
    assert heads, "the queue should not be empty"
    assert heads[0] == "Your books could not be read in full"


def test_a_real_bank_balance_is_shown_without_the_assumption_banner() -> None:
    body = _get(_app(_WithBankAccount()), "/t/acme").body
    assert "$34,500.25" in body
    assert "Working from your saved assumptions" not in body


# --- the label belongs on the figure ----------------------------------------


def test_the_hero_figure_itself_carries_its_provenance() -> None:
    """A banner above a number is not a label on it.

    The number is what gets screenshotted, pasted into a board pack and read
    down the phone; the banner stays behind on the page. Every books screen in
    the product already badges its figures — the one screen that mixes fact and
    assumption in a single number did not.
    """
    body = _get(_app(_NoBankAccounts()), "/t/acme").body
    assert "Forecast" in _hero(body)


def test_a_figure_from_the_books_is_labelled_posted_not_forecast() -> None:
    body = _get(_app(_WithBankAccount()), "/t/acme").body
    hero = _hero(body)
    assert "Posted" in hero
    assert "Forecast" not in hero


# --- the queue reads like something a person wrote --------------------------


def test_a_queue_line_starts_as_a_sentence() -> None:
    """Ledger problems and data-quality notes are clauses by construction —
    their other callers embed them mid-line. In the queue each one opens a line
    of its own, where a lower-case start reads as a leaked internal string."""
    body = _get(_app(_NoBankAccounts()), "/t/acme").body
    details = [re.sub(r"<[^>]+>", "", m).strip()
               for m in re.findall(r'class="m">(.*?)</div>', body, re.S)]
    assert details, "the queue should have detail lines"
    for d in details:
        assert d[0].isupper(), f"queue line does not start as a sentence: {d!r}"


def test_the_badge_is_absent_when_there_are_no_books_at_all() -> None:
    """A deployment with no ledger wired has nothing to compare against, and a
    lone "Forecast" badge on an assumptions-only product would be noise on
    every screen rather than information."""
    hero = _hero(app_with_two_tenants().handle(
        Request("GET", "/t/acme", {"authorization": "Bearer tok-acme"}, "")).body)
    assert "Forecast" not in hero
    assert "Posted" not in hero
