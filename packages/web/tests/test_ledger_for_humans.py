"""Ledger and reports for humans (issues 25-27).

The Ledger opened on a trial balance with account codes sorted as text ("10"
before "9"); Reports listed the operator's own usage and reconciliation checks
next to the owner's; Business health showed revenue with no period, verdicts in
lower case and a bare "—" for ratios that can't be worked out.
"""

from __future__ import annotations

import re

from test_books import _app, _req

from rgnr8_web.books_screens import code_order
from rgnr8_web.health_screens import render_health
from rgnr8_web.screens import OWNER_REPORTS


def test_account_codes_sort_the_way_people_read_them() -> None:
    codes = ["10", "9", "1000-01", "1000", "2", "84", "Undeposited"]
    assert sorted(codes, key=code_order) == ["2", "9", "10", "84", "1000", "1000-01", "Undeposited"]


def test_the_ledger_opens_on_accounts_grouped_by_type() -> None:
    app, _, _ = _app()
    page = _req(app, "/t/acme/books").body
    main = page[page.index('<main class="shell">'):]
    assert re.findall(r"<h1>(.*?)</h1>", main) == ["Ledger"]
    assert re.search(r'aria-current="page">Accounts<', main)
    # Assets, then income, then expenses, each with its total.
    assert main.index(">Assets<") < main.index(">Income<") < main.index(">Expenses<")
    assert "Total income" in main and "Business Checking" in main
    # Income reads as a positive amount earned, not a negative debit balance.
    income = main[main.index(">Income<"):main.index("Total income")]
    assert "$5,000.00" in income and "-$5,000.00" not in income
    assert "In balance" in main


def test_the_trial_balance_is_its_own_tab() -> None:
    app, _, _ = _app()
    main = _req(app, "/t/acme/books/trial-balance").body
    assert re.search(r'aria-current="page">Trial balance<', main)
    assert "Trial balance" in main and "In balance" in main and "Totals" in main


def test_reports_lead_with_the_six_an_owner_uses() -> None:
    app, _, _ = _app()
    main = _req(app, "/t/acme/reports").body
    more = main.index("More reports")
    assert len(OWNER_REPORTS) == 6
    for title in ("Cash Flow Outlook", "Weekly Owner Briefing", "Runway"):
        assert main.index(title) < more, title
    for internal in ("Usage &amp; Billing", "Reconciliation / Trust"):
        assert main.index(internal) > more, internal


def test_health_says_what_period_revenue_covers_and_why_a_ratio_is_missing() -> None:
    html = render_health("acme", {
        "as_of": "2026-10-29",
        "liquidity": {"current_ratio": None, "health": "no current liabilities"},
        "leverage": {"debt_to_equity": "0.5", "health": "conservative — more equity than debt"},
        "profitability": {"gross_margin_pct": None, "health": "no revenue yet"},
        "inputs": {"revenue_minor": "0"},
    })
    assert "Revenue, through 2026-10-29" in html
    assert "Not enough data · no current liabilities" in html
    assert "Conservative — more equity than debt" in html and ">conservative" not in html
    assert ">—<" not in html
