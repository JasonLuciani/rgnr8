"""Splitting one commingled account into two balanced sets of books.

Deterministic throughout: fixed transactions and rules, so routing, balance, and
the audit trail are all exact.
"""

from __future__ import annotations

from rgnr8_categorize import (
    BookRouter,
    BookRule,
    Categorizer,
    LearnedModel,
    Rule,
    RuleSet,
    Txn,
    split_books,
)


def _router() -> BookRouter:
    # Business: payroll processor and the merchant deposits. Everything else
    # (groceries, the mortgage) falls through to the personal book.
    return BookRouter(
        rules=[
            BookRule("business", Rule("payroll", "Payroll", counterparty="GUSTO")),
            BookRule("business", Rule("card-deposits", "Sales", description_regex="STRIPE")),
        ],
        default_book="personal",
    )


def _statement() -> list[Txn]:
    return [
        Txn("t1", "STRIPE PAYOUT", "STRIPE", 250000),      # business inflow
        Txn("t2", "GUSTO PAYROLL", "GUSTO", -120000),       # business outflow
        Txn("t3", "WHOLE FOODS", "WHOLEFOODS", -8000),      # personal
        Txn("t4", "MORTGAGE", "BIGBANK", -300000),          # personal
    ]


def test_routes_each_transaction_to_the_right_book() -> None:
    result = split_books(_statement(), _router())
    business = result.book("business")
    personal = result.book("personal")
    assert business is not None and personal is not None
    assert {e.txn_id for e in business.entries} == {"t1", "t2"}
    assert {e.txn_id for e in personal.entries} == {"t3", "t4"}


def test_each_set_of_books_balances() -> None:
    result = split_books(_statement(), _router())
    assert result.all_balanced is True
    for b in result.books:
        assert b.debits_minor == b.credits_minor
        assert b.debits_minor > 0  # non-trivial


def test_net_cash_per_book_matches_the_routed_flows() -> None:
    result = split_books(_statement(), _router())
    # business: +250,000 - 120,000 = +130,000 ; personal: -8,000 - 300,000 = -308,000
    assert result.book("business").net_cash_minor == 130000
    assert result.book("personal").net_cash_minor == -308000


def test_default_book_catches_unmatched_and_records_no_rule() -> None:
    result = split_books(_statement(), _router())
    personal = result.book("personal")
    assert all(e.routed_by is None for e in personal.entries)
    business = result.book("business")
    assert {e.routed_by for e in business.entries} == {"payroll", "card-deposits"}


def test_audit_trail_shows_where_every_transaction_landed() -> None:
    result = split_books(_statement(), _router())
    trail = {row["txn_id"]: row for row in result.audit_trail()}
    assert len(trail) == 4
    assert trail["t1"]["book"] == "business" and trail["t1"]["routed_by"] == "card-deposits"
    assert trail["t2"]["book"] == "business" and trail["t2"]["routed_by"] == "payroll"
    assert trail["t3"]["book"] == "personal" and trail["t3"]["routed_by"] == "(default)"


def test_categorizer_attaches_a_category_to_each_routed_entry() -> None:
    ruleset = RuleSet([
        Rule("stripe-sales", "Sales", description_regex="STRIPE"),
        Rule("gusto-wages", "Wages", counterparty="GUSTO"),
        Rule("food", "Meals", counterparty="WHOLEFOODS"),
    ])
    cat = Categorizer(ruleset, LearnedModel.from_history([]))
    result = split_books(_statement(), _router(), categorizer=cat)
    by_id = {row["txn_id"]: row for row in result.audit_trail()}
    assert by_id["t1"]["category"] == "Sales"
    assert by_id["t2"]["category"] == "Wages"
    assert by_id["t3"]["category"] == "Meals"
    # categorized books still balance (postings hit real accounts, not one plug)
    assert result.all_balanced is True


def test_uncategorized_entries_land_on_an_explicit_suspense_line() -> None:
    # No categorizer → every entry is Uncategorized, but the books still balance
    # because the suspense line is a real posting, not a silent plug.
    result = split_books(_statement(), _router())
    assert all(e.category == "Uncategorized" for b in result.books for e in b.entries)
    assert result.all_balanced is True


def test_empty_books_are_omitted() -> None:
    # A router whose only rule never matches yields just the default book.
    router = BookRouter(
        rules=[BookRule("business", Rule("never", "X", counterparty="NOPE"))],
        default_book="personal",
    )
    result = split_books(_statement(), router)
    assert [b.book for b in result.books] == ["personal"]
    assert result.entry_count == 4


def test_first_matching_rule_wins() -> None:
    # Two rules could match the same txn; the earlier one decides the book.
    router = BookRouter(
        rules=[
            BookRule("client-a", Rule("first", "X", description_regex="STRIPE")),
            BookRule("client-b", Rule("second", "Y", counterparty="STRIPE")),
        ],
        default_book="unassigned",
    )
    result = split_books([Txn("t1", "STRIPE PAYOUT", "STRIPE", 250000)], router)
    assert result.book("client-a") is not None
    assert result.book("client-b") is None
