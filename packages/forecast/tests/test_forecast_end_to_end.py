from datetime import date

from rgnr8_forecast import (
    CashPosition,
    Category,
    CustomerHistory,
    Direction,
    ForecastConfig,
    ForecastInputs,
    Frequency,
    Invoice,
    Money,
    PayrollSchedule,
    Recurrence,
    RecurringItem,
    Scenario,
    run_forecast,
)
from factory import AS_OF, history, usd


def _healthy_inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=AS_OF, available=usd("80000.00")),
        invoices=(
            Invoice("INV-1", "acme", date(2026, 8, 1), date(2026, 8, 20), usd("15000.00")),
            Invoice("INV-2", "acme", date(2026, 8, 10), date(2026, 8, 31), usd("12000.00")),
        ),
        customer_histories=(history("acme", [5, 6, 4, 7, 5]),),
        recurring=(
            RecurringItem("Rent", Category.RENT, Direction.OUTFLOW, usd("4000.00"),
                          Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 1))),
        ),
    )


def _stressed_inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=AS_OF, available=usd("20000.00")),
        payroll=(
            PayrollSchedule("Team", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 8, 7)),
                            usd("16000.00"), usd("4200.00")),
        ),
    )


def test_healthy_business_stays_above_floor() -> None:
    r = run_forecast(_healthy_inputs(), ForecastConfig(minimum_cash=usd("10000.00")))
    assert not r.projection.breach.breached
    assert "stays above" in r.headline
    assert r.recommended_action is None
    assert 0 <= r.overall_confidence <= 100


def test_stressed_business_breaches_and_gets_an_action() -> None:
    r = run_forecast(_stressed_inputs(), ForecastConfig(minimum_cash=usd("10000.00")))
    assert r.projection.breach.breached
    assert "fall below" in r.headline
    assert r.recommended_action is not None


def test_every_number_traces_to_a_flow() -> None:
    r = run_forecast(_healthy_inputs(), ForecastConfig(minimum_cash=usd("10000.00")))
    # each weekly bucket references the flows that compose it
    for w in r.projection.weeks:
        for seq in w.flow_seqs:
            f = r.flow(seq)
            assert f is not None
            assert w.start <= f.on_date <= w.end


def test_data_quality_flags_unverified_and_missing_history() -> None:
    inputs = ForecastInputs(
        opening=CashPosition(as_of=AS_OF, available=usd("10000.00"), verified=False),
        invoices=(Invoice("X", "nohist", date(2026, 8, 1), date(2026, 8, 15), usd("1000.00")),),
    )
    r = run_forecast(inputs, ForecastConfig())
    assert any("not yet verified" in n for n in r.data_quality)
    assert any("no payment history" in n for n in r.data_quality)
    assert r.overall_confidence <= 60  # capped when opening unverified


def test_weekly_closings_reconcile_to_ending_balance() -> None:
    r = run_forecast(_healthy_inputs(), ForecastConfig(minimum_cash=usd("10000.00")))
    assert r.projection.weeks[-1].closing == r.projection.ending_balance
    # opening of each week equals prior week's closing
    weeks = r.projection.weeks
    for i in range(1, len(weeks)):
        assert weeks[i].opening == weeks[i - 1].closing
