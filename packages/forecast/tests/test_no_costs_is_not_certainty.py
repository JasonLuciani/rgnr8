"""A forecast with nothing leaving the account is not a confident forecast.

Found on the first real go-live: the books came across exactly — $101,939.72
of cash, $168k of costs so far this year in the trial balance — but nothing in
the forecast was set to leave the account. The screen drew a flat line for
thirteen weeks, said "Cash stays above your $25,000.00 floor for all 13 weeks",
and signed it "Confidence 100/100".

The 100 came from `_overall_confidence`: with no flows, the confidence-weighted
total is 0/0, and the code answered that with full certainty. An outside review
had named the same failure a few hours earlier — "missing information becomes
confident financial advice" — on a different screen.
"""

from datetime import date

from factory import AS_OF, usd
from rgnr8_forecast import (
    CashPosition,
    Category,
    Direction,
    ForecastConfig,
    ForecastInputs,
    Frequency,
    Invoice,
    Recurrence,
    RecurringItem,
    run_forecast,
)
from rgnr8_forecast.result import NO_COSTS_CONFIDENCE_CAP

CONFIG = ForecastConfig(minimum_cash=usd("25000.00"))


def _cash_only() -> ForecastInputs:
    return ForecastInputs(opening=CashPosition(as_of=AS_OF, available=usd("101939.72")))


def test_nothing_in_the_forecast_is_not_full_confidence() -> None:
    r = run_forecast(_cash_only(), CONFIG)
    assert r.overall_confidence <= NO_COSTS_CONFIDENCE_CAP


def test_it_says_first_that_there_are_no_costs() -> None:
    r = run_forecast(_cash_only(), CONFIG)
    assert r.data_quality and r.data_quality[0].startswith("no costs in the outlook")


def test_the_headline_does_not_claim_the_cash_lasts() -> None:
    r = run_forecast(_cash_only(), CONFIG)
    assert "stays above" not in r.headline
    assert "can't say how long" in r.headline
    assert "$101,939.72" in r.headline


def test_money_coming_in_but_none_going_out_is_still_capped() -> None:
    inputs = ForecastInputs(
        opening=CashPosition(as_of=AS_OF, available=usd("50000.00")),
        invoices=(Invoice("INV-1", "acme", date(2026, 8, 1), date(2026, 8, 20), usd("5000.00")),),
    )
    r = run_forecast(inputs, CONFIG)
    assert r.overall_confidence <= NO_COSTS_CONFIDENCE_CAP
    assert r.data_quality[0].startswith("no costs in the outlook")


def test_a_forecast_with_costs_in_it_is_untouched() -> None:
    inputs = ForecastInputs(
        opening=CashPosition(as_of=AS_OF, available=usd("80000.00")),
        recurring=(RecurringItem("Rent", Category.RENT, Direction.OUTFLOW, usd("4000.00"),
                                 Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 1))),),
    )
    r = run_forecast(inputs, ForecastConfig(minimum_cash=usd("10000.00")))
    assert not any(n.startswith("no costs") for n in r.data_quality)
    assert "stays above" in r.headline
    assert r.overall_confidence > NO_COSTS_CONFIDENCE_CAP
