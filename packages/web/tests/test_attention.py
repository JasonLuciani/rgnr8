"""The action queue's ranking policy.

These tests are mostly about the two rules in `attention`'s docstring, because
those are product judgements rather than mechanics: trust outranks urgency, and
not knowing is never rendered as nothing. If either rule is ever deliberately
changed, these are the tests that should have to change with it.
"""

from __future__ import annotations

from rgnr8_forecast import Money

from rgnr8_web.attention import Signals, gather


def _keys(items) -> list[str]:
    return [i.key for i in items]


# --- rule 1: trust outranks urgency -----------------------------------------


def test_unreadable_books_outrank_an_imminent_floor_breach() -> None:
    # A breach this week is the most urgent thing the forecast can say. It is
    # still second, because it was computed from books we just admitted we
    # could not read -- acting on it could be acting on fiction.
    items = gather(Signals(
        tenant="acme",
        ledger_problems=("2 of 3 cash accounts could not be read",),
        floor_breached=True,
        breach_weeks_until=0,
        breach_shortfall=Money(-1_420_000),
        review_count=0,
    ))
    assert _keys(items)[:2] == ["ledger-problems", "floor-breach"]


def test_a_huge_overdue_pile_cannot_climb_out_of_its_band() -> None:
    # The within-band adjustment must never let a big number jump a band.
    items = gather(Signals(
        tenant="acme",
        overdue_count=500,
        overdue_total=Money(99_000_000),
        books_connected=False,
        review_count=0,
    ))
    assert _keys(items)[0] == "no-books"
    kinds = [i.kind for i in items]
    assert kinds == sorted(kinds, key=lambda k: {"trust": 0, "money": 1, "work": 2}[k])


def test_a_nearer_breach_outranks_a_further_one() -> None:
    near = gather(Signals(tenant="a", floor_breached=True, breach_weeks_until=1, review_count=0))
    far = gather(Signals(tenant="a", floor_breached=True, breach_weeks_until=11, review_count=0))
    assert near[0].urgency > far[0].urgency


def test_the_breach_headline_says_when_in_plain_words() -> None:
    now = gather(Signals(tenant="a", floor_breached=True, breach_weeks_until=0, review_count=0))
    assert now[0].headline.startswith("This week")
    one = gather(Signals(tenant="a", floor_breached=True, breach_weeks_until=1, review_count=0))
    assert "In 1 week," in one[0].headline and "1 weeks" not in one[0].headline
    unknown = gather(Signals(tenant="a", floor_breached=True, review_count=0))
    assert "next 13 weeks" in unknown[0].headline


# --- rule 2: not knowing is not nothing -------------------------------------


def test_an_unwired_bank_feed_is_an_item_not_a_silence() -> None:
    items = gather(Signals(tenant="acme"))  # review_count defaults to None
    assert "feed-unknown" in _keys(items)


def test_a_checked_and_clear_feed_says_nothing() -> None:
    # The difference between these two tests is the whole point: 0 means
    # somebody looked, None means nobody did.
    items = gather(Signals(tenant="acme", review_count=0))
    assert "feed-unknown" not in _keys(items)
    assert "review-queue" not in _keys(items)


def test_an_empty_queue_means_every_source_was_actually_checked() -> None:
    clear = gather(Signals(tenant="acme", review_count=0))
    assert clear == ()


# --- the individual sources -------------------------------------------------


def test_every_source_can_raise_an_item() -> None:
    items = gather(Signals(
        tenant="acme",
        ledger_problems=("bank feed stale",),
        books_connected=False,
        data_quality=("Payroll is an estimate",),
        floor_breached=True,
        breach_weeks_until=3,
        review_count=12,
        overdue_count=4,
        overdue_total=Money(1_800_000),
        worst_customer="Riverside LLC",
        worst_days=61,
        close_period="2026-08",
        close_overdue=2,
        close_blocked=1,
    ))
    assert set(_keys(items)) == {
        "ledger-problems", "no-books", "data-quality-0", "floor-breach",
        "overdue-ar", "review-queue", "close-blocked", "close-overdue",
    }
    # Strictly ordered, most urgent first.
    urgencies = [i.urgency for i in items]
    assert urgencies == sorted(urgencies, reverse=True)


def test_data_quality_notes_are_capped_so_the_queue_stays_a_queue() -> None:
    # A forecast with fourteen caveats must not bury the floor breach under
    # fourteen rows of small print.
    items = gather(Signals(
        tenant="a",
        data_quality=tuple(f"note {n}" for n in range(14)),
        review_count=0,
    ))
    assert len([k for k in _keys(items) if k.startswith("data-quality")]) == 3


def test_the_overdue_item_names_the_worst_offender_and_the_total() -> None:
    items = gather(Signals(
        tenant="a", review_count=0, overdue_count=3, overdue_total=Money(450_000),
        worst_customer="Riverside LLC", worst_days=61,
    ))
    item = next(i for i in items if i.key == "overdue-ar")
    assert item.headline == "3 invoices overdue"
    assert "$4,500.00" in item.detail and "Riverside LLC is 61 days late" in item.detail


def test_singular_and_plural_read_like_english() -> None:
    one = gather(Signals(tenant="a", review_count=1, overdue_count=1, close_overdue=1))
    text = " ".join(i.headline for i in one)
    assert "1 bank line waiting" in text
    assert "1 invoice overdue" in text
    assert "1 close task overdue" in text


def test_the_briefings_primary_action_only_stands_alone_without_a_breach() -> None:
    # With a breach it becomes the breach's detail line rather than a second,
    # competing "do this" row.
    with_breach = gather(Signals(
        tenant="a", review_count=0, floor_breached=True, breach_weeks_until=2,
        primary_action="Delay the Riverside PO by two weeks",
    ))
    assert "primary-action" not in _keys(with_breach)
    assert with_breach[0].detail == "Delay the Riverside PO by two weeks"

    without = gather(Signals(
        tenant="a", review_count=0, primary_action="Delay the Riverside PO by two weeks",
    ))
    assert _keys(without) == ["primary-action"]


# --- stability --------------------------------------------------------------


def test_the_order_is_stable_for_the_same_data() -> None:
    # A list that reshuffles between two renders of identical data is a list an
    # owner stops trusting.
    s = Signals(tenant="a", review_count=0, close_overdue=1, close_blocked=1,
                data_quality=("a", "b", "c"))
    assert _keys(gather(s)) == _keys(gather(s))


def test_links_are_scoped_to_the_tenant() -> None:
    for item in gather(Signals(
        tenant="acme", ledger_problems=("x",), floor_breached=True,
        overdue_count=1, review_count=2, close_overdue=1, close_blocked=1,
    )):
        assert item.href.startswith("/t/acme")
