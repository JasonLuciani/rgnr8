"""Contract test: the books statements screen reads the ledger's real response.

The fixture is the same file packages/ledger-service/test/statements-contract.test.ts
checks the ledger against. The ledger test fails if the response changes shape; this
test fails if the screen reads field names the ledger does not send, which is how the
income statement and balance sheet came to show "None" on every line.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

from rgnr8_web.books_screens import render_books_statements

FIXTURE = Path(__file__).parent / "contracts" / "statements.json"


def _contract() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _html() -> str:
    return render_books_statements("2026-08-01 to 2026-08-31", _contract())


def test_fixture_is_the_financial_statements_contract() -> None:
    assert _contract()["contract"] == "financial-statements/1"


def test_screen_shows_the_real_numbers_and_never_none() -> None:
    html = _html()
    assert "None" not in html
    # Totals and the net loss; the net loss and the overdrawn cash are negative.
    for figure in ("$5,000.00", "$8,000.00", "-$3,000.00"):
        assert figure in html
    assert html.count('class="num neg"') >= 2


def test_screen_shows_every_statement_line_by_its_label() -> None:
    html = _html()
    contract = _contract()
    income = contract["income_statement"]
    bs = contract["balance_sheet"]
    assert isinstance(income, dict) and isinstance(bs, dict)
    lines = [*income["revenue"], *income["expenses"],
             *bs["assets"], *bs["liabilities"], *bs["equity"]]
    assert lines, "fixture carries no statement lines"
    for line in lines:
        assert escape(line["label"]) in html, line["label"]


def test_statement_totals_from_the_contract_are_on_screen() -> None:
    html = _html()
    contract = _contract()
    income = contract["income_statement"]
    bs = contract["balance_sheet"]
    assert isinstance(income, dict) and isinstance(bs, dict)
    # Every total the screen prints is the ledger's own integer minor units, formatted.
    assert income["total_revenue"] == 500000 and "$5,000.00" in html
    assert income["total_expenses"] == 800000 and "$8,000.00" in html
    assert income["net_income"] == -300000 and "-$3,000.00" in html
    assert bs["total_assets"] == -300000 and bs["balanced"] is True
    assert "Balanced" in html
