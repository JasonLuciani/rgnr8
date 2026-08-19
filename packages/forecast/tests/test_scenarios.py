from datetime import date

from factory import AS_OF, usd
from rgnr8_forecast import (
    CashPosition,
    CustomerHistory,
    ForecastConfig,
    ForecastInputs,
    Invoice,
    PipelineOpportunity,
    Scenario,
    run_all_scenarios,
)


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=AS_OF, available=usd("30000.00")),
        invoices=(
            Invoice("A", "beta", date(2026, 3, 1), date(2026, 4, 1), usd("15000.00")),  # aged
            Invoice("B", "beta", date(2026, 8, 1), date(2026, 8, 20), usd("8000.00")),
        ),
        customer_histories=(CustomerHistory("beta", override_days_late=5),),
        pipeline=(PipelineOpportunity("Deal", "gamma", date(2026, 9, 1), usd("20000.00"), 5000),),
    )


def test_scenarios_are_ordered_downside_le_base_le_upside() -> None:
    cmp = run_all_scenarios(_inputs(), ForecastConfig(minimum_cash=usd("5000.00")))
    down = cmp.downside.projection.trough.balance
    base = cmp.base.projection.trough.balance
    up = cmp.upside.projection.trough.balance
    assert down <= base <= up


def test_comparison_helpers() -> None:
    cmp = run_all_scenarios(_inputs(), ForecastConfig(minimum_cash=usd("5000.00")))
    assert set(cmp.as_dict().keys()) == {Scenario.BASE, Scenario.DOWNSIDE, Scenario.UPSIDE}
    assert len(cmp.summary_lines()) == 3
    lo, hi = cmp.trough_spread()
    assert lo <= hi


def test_upside_includes_pipeline_base_does_not() -> None:
    cmp = run_all_scenarios(_inputs(), ForecastConfig(minimum_cash=usd("5000.00")))
    base_in = cmp.base.projection.total_inflows
    up_in = cmp.upside.projection.total_inflows
    assert up_in > base_in  # pipeline adds probability-weighted inflow
