"""The chart of accounts and the trial balance share one sort: statement order by
account subtype, then the account's real number, then its name. Sub-accounts sit
directly under their parent, indented. A QuickBooks internal id never shows as an
account number.
"""

from __future__ import annotations

import re

from test_account_numbers_and_fiscal_year import _app, _main, _shown
from test_books import DEFAULT_ROUTES, TRIAL_BALANCE, _req

from rgnr8_web.books_screens import (
    arrange_accounts,
    detail_type_label,
    render_chart_of_accounts,
    render_trial_balance,
)
from rgnr8_web.store import TenantState

# A complete numbered chart, in no particular order, covering every statement section.
FULL_CHART = [
    {"code": "3900", "name": "Retained Earnings", "type": "EQUITY", "subtype": "RETAINED_EARNINGS"},
    {"code": "4000", "name": "Sales", "type": "REVENUE", "subtype": "INCOME"},
    {"code": "6000", "name": "Rent", "type": "EXPENSE", "subtype": "EXPENSE"},
    {"code": "5000", "name": "Materials", "type": "EXPENSE", "subtype": "COST_OF_GOODS_SOLD"},
    {"code": "8000", "name": "Interest Earned", "type": "REVENUE", "subtype": "OTHER_INCOME"},
    {"code": "9000", "name": "Loss on Sale", "type": "EXPENSE", "subtype": "OTHER_EXPENSE"},
    {"code": "2100", "name": "Visa", "type": "LIABILITY", "subtype": "CREDIT_CARD"},
    {"code": "2000", "name": "Accounts Payable", "type": "LIABILITY", "subtype": "ACCOUNTS_PAYABLE"},
    {"code": "2500", "name": "Term Loan", "type": "LIABILITY", "subtype": "LONG_TERM_LIABILITY"},
    {"code": "2300", "name": "Sales Tax Payable", "type": "LIABILITY",
     "subtype": "SALES_TAX_PAYABLE"},
    {"code": "1700", "name": "Office Equipment", "type": "ASSET", "subtype": "FIXED_ASSET"},
    {"code": "1900", "name": "Other Deposits", "type": "ASSET", "subtype": "OTHER_ASSET"},
    {"code": "1500", "name": "Prepaid Rent", "type": "ASSET", "subtype": "OTHER_CURRENT_ASSET"},
    {"code": "1200", "name": "Accounts Receivable", "type": "ASSET",
     "subtype": "ACCOUNTS_RECEIVABLE"},
    {"code": "1000", "name": "Operating Bank", "type": "ASSET", "subtype": "BANK"},
]

ORDER_OF_TYPES = [
    "Operating Bank",          # Bank
    "Accounts Receivable",     # Accounts receivable
    "Prepaid Rent",            # Other current assets
    "Office Equipment",        # Fixed assets
    "Other Deposits",          # Other assets
    "Accounts Payable",        # Accounts payable
    "Visa",                    # Credit card
    "Sales Tax Payable",       # Other current liabilities
    "Term Loan",               # Long-term liabilities
    "Retained Earnings",       # Equity
    "Sales",                   # Income
    "Materials",               # Cost of goods sold
    "Rent",                    # Expenses
    "Interest Earned",         # Other income
    "Loss on Sale",            # Other expense
]


def _names(html: str) -> list[str]:
    # The chart's own account links (an href with no further path): not the
    # "New account" action, nor an account's Edit link.
    return re.findall(r'books/accounts/(?!new")[^"/]+">([^<]+)</a>', html)


# --- the one sort ------------------------------------------------------------------

def test_the_chart_follows_statement_order_by_subtype() -> None:
    chart = render_chart_of_accounts("acme", {"accounts": FULL_CHART})
    assert _names(chart) == ORDER_OF_TYPES


def test_retained_earnings_sorts_under_equity_not_first_on_the_trial_balance() -> None:
    rows = [
        {"code": "3900", "name": "Retained Earnings", "type": "EQUITY",
         "subtype": "RETAINED_EARNINGS", "debit_minor": "0", "credit_minor": "100"},
        {"code": "1000", "name": "Operating Bank", "type": "ASSET", "subtype": "BANK",
         "debit_minor": "100", "credit_minor": "0"},
        {"code": "4000", "name": "Sales", "type": "REVENUE", "subtype": "INCOME",
         "debit_minor": "0", "credit_minor": "50"},
    ]
    tb = render_trial_balance("acme", {"rows": rows, "currency": "USD"})
    assert _names(tb) == ["Operating Bank", "Retained Earnings", "Sales"]


def test_a_retained_earnings_id_under_quickbooks_ids_still_sorts_under_equity() -> None:
    accounts = [
        {"code": "1", "name": "Retained Earnings", "type": "EQUITY", "subtype": "RETAINED_EARNINGS"},
        {"code": "57", "name": "Checking", "type": "ASSET", "subtype": "BANK"},
        {"code": "103", "name": "Services", "type": "REVENUE", "subtype": "INCOME"},
    ]
    ids = frozenset({"1", "57", "103"})
    chart = render_chart_of_accounts("acme", {"accounts": accounts}, id_codes=ids)
    assert _names(chart) == ["Checking", "Retained Earnings", "Services"]


def test_within_a_detail_type_numbers_come_first_in_numeric_order_then_names() -> None:
    accounts = [
        {"code": "6500", "name": "Utilities", "type": "EXPENSE", "subtype": "EXPENSE"},
        {"code": "600", "name": "Payroll", "type": "EXPENSE", "subtype": "EXPENSE"},
        {"code": "12", "name": "Rent", "type": "EXPENSE", "subtype": "EXPENSE"},
    ]
    chart = render_chart_of_accounts("acme", {"accounts": accounts},
                                     id_codes=frozenset({"12"}))
    assert _names(chart) == ["Payroll", "Utilities", "Rent"]


def test_an_account_with_no_subtype_falls_back_to_its_type_position() -> None:
    accounts = [
        {"code": "4000", "name": "Sales", "type": "REVENUE"},
        {"code": "1000", "name": "Cash", "type": "ASSET"},
        {"code": "6000", "name": "Rent", "type": "EXPENSE"},
    ]
    assert _names(render_chart_of_accounts("acme", {"accounts": accounts})) == \
        ["Cash", "Sales", "Rent"]


# --- sub-accounts ----------------------------------------------------------------------

SUB_ACCOUNTS = [
    {"code": "1000", "name": "Operating Bank", "type": "ASSET", "subtype": "BANK"},
    {"code": "1010", "name": "Payroll Account", "type": "ASSET", "subtype": "BANK",
     "parent_code": "1000"},
    {"code": "1020", "name": "Savings", "type": "ASSET", "subtype": "BANK"},
    {"code": "1011", "name": "Payroll Taxes", "type": "ASSET", "subtype": "BANK",
     "parent_code": "1010"},
]


def test_arrange_puts_each_sub_account_directly_under_its_parent() -> None:
    arranged = arrange_accounts(SUB_ACCOUNTS)
    assert [(d, a["code"]) for d, a in arranged] == [
        (0, "1000"), (1, "1010"), (2, "1011"), (0, "1020")]


def test_a_sub_account_renders_under_its_parent_and_indented() -> None:
    html = render_chart_of_accounts("acme", {"accounts": SUB_ACCOUNTS})
    names = _names(html)
    assert names == ["Operating Bank", "Payroll Account", "Payroll Taxes", "Savings"]
    assert 'style="padding-left:1.5em"' in html            # depth 1
    assert 'style="padding-left:3.0em"' in html            # depth 2
    top = html.index(">Operating Bank<")
    child = html.index(">Payroll Account<")
    assert top < child < html.index(">Savings<")


def test_a_sub_account_whose_parent_is_not_listed_sits_at_the_top_level() -> None:
    orphan = [{"code": "1010", "name": "Payroll Account", "type": "ASSET", "subtype": "BANK",
               "parent_code": "1000"}]
    assert arrange_accounts(orphan) == [(0, orphan[0])]


def test_a_parent_cycle_is_shown_rather_than_dropped() -> None:
    cycle = [{"code": "1", "name": "A", "type": "ASSET", "subtype": "BANK", "parent_code": "2"},
             {"code": "2", "name": "B", "type": "ASSET", "subtype": "BANK", "parent_code": "1"}]
    assert sorted(a["code"] for _d, a in arrange_accounts(cycle)) == ["1", "2"]


# --- numbers -----------------------------------------------------------------------------

def test_a_quickbooks_id_never_appears_in_the_number_column() -> None:
    accounts = [
        {"code": "57", "name": "Checking", "type": "ASSET", "subtype": "BANK"},
        {"code": "4000", "name": "Sales", "type": "REVENUE", "subtype": "INCOME"},
    ]
    ids = frozenset({"57"})
    chart = render_chart_of_accounts("acme", {"accounts": accounts}, id_codes=ids)
    assert '<td class="muted">57</td>' not in chart
    assert '<td class="muted">4000</td>' in chart
    tb = render_trial_balance("acme", {"rows": [
        {"code": "57", "name": "Checking", "type": "ASSET", "subtype": "BANK",
         "debit_minor": "1", "credit_minor": "0"},
        {"code": "4000", "name": "Sales", "type": "REVENUE", "subtype": "INCOME",
         "debit_minor": "0", "credit_minor": "1"}]}, id_codes=ids)
    assert '<td class="muted">57</td>' not in tb
    assert '<td class="muted">4000</td>' in tb


def test_a_chart_with_no_real_numbers_drops_the_number_column() -> None:
    accounts = [{"code": "57", "name": "Checking", "type": "ASSET", "subtype": "BANK"}]
    chart = render_chart_of_accounts("acme", {"accounts": accounts}, id_codes=frozenset({"57"}))
    assert "<th>No.</th>" not in chart


# --- columns ---------------------------------------------------------------------------

def test_the_chart_columns_are_name_type_detail_type_and_balance() -> None:
    tb = {"currency": "USD", "rows": [
        {"code": "1000", "name": "Operating Bank", "type": "ASSET", "subtype": "BANK",
         "debit_minor": "30000", "credit_minor": "0"},
        {"code": "2000", "name": "Accounts Payable", "type": "LIABILITY",
         "subtype": "ACCOUNTS_PAYABLE", "debit_minor": "0", "credit_minor": "12500"},
    ]}
    accounts = [
        {"code": "1000", "name": "Operating Bank", "type": "ASSET", "subtype": "BANK"},
        {"code": "2000", "name": "Accounts Payable", "type": "LIABILITY",
         "subtype": "ACCOUNTS_PAYABLE"},
    ]
    html = render_chart_of_accounts("acme", {"accounts": accounts}, tb=tb)
    assert "<th>Account</th><th>Type</th><th>Detail type</th>" in html
    assert '<th class="num">Balance</th>' in html
    assert "<td>Asset</td>" in html and "<td>Liability</td>" in html
    assert '<td class="muted">Accounts payable</td>' in html
    # What you owe reads as a positive amount, as on the Ledger's accounts card.
    assert '<td class="num">$300.00</td>' in html
    assert '<td class="num">$125.00</td>' in html


def test_the_balance_column_says_so_when_the_trial_balance_is_unavailable() -> None:
    html = render_chart_of_accounts("acme", {"accounts": FULL_CHART[:1]}, tb=None)
    assert '<td class="num muted">—</td>' in html


def test_detail_type_labels_read_as_words() -> None:
    assert detail_type_label("ACCOUNTS_RECEIVABLE") == "Accounts receivable"
    assert detail_type_label("COST_OF_GOODS_SOLD") == "Cost of goods sold"
    assert detail_type_label(None) == ""


# --- the screens -------------------------------------------------------------------

def test_the_chart_page_reads_the_trial_balance_for_its_balance_column() -> None:
    accounts = {"tenant": "acme", "accounts": FULL_CHART}
    tb = {**TRIAL_BALANCE, "rows": [
        {"code": "1000", "name": "Operating Bank", "type": "ASSET", "subtype": "BANK",
         "debit_minor": "300000", "credit_minor": "0"}]}
    app, _ = _app({**DEFAULT_ROUTES, "GET /t/acme/accounts": (200, accounts),
                   "GET /t/acme/trial-balance": (200, tb)}, state=TenantState(qbo_id_codes=[]))
    page = _main(_req(app, "/t/acme/books/accounts").body)
    assert _names(page) == ORDER_OF_TYPES
    assert '<td class="num">$3,000.00</td>' in page


def test_the_chart_page_still_renders_when_the_trial_balance_cannot_be_read() -> None:
    accounts = {"tenant": "acme", "accounts": FULL_CHART[:2]}
    app, _ = _app({**DEFAULT_ROUTES, "GET /t/acme/accounts": (200, accounts),
                   "GET /t/acme/trial-balance": (503, {"error": "down"})},
                  state=TenantState(qbo_id_codes=[]))
    page = _main(_req(app, "/t/acme/books/accounts").body)
    assert "Chart of Accounts (2)" in _shown(page)
    assert '<td class="num muted">—</td>' in page
