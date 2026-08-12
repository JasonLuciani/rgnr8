from datetime import date

from rgnr8_forecast import (
    BacktestCase,
    Money,
    WeekObservation,
    render_backtest_text,
    run_backtest,
)


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def obs(h: int, predicted: str, actual: str) -> WeekObservation:
    return WeekObservation(horizon=h, predicted=usd(predicted), actual=usd(actual))


def test_perfect_forecast_has_zero_error() -> None:
    case = BacktestCase(
        as_of=date(2026, 8, 1),
        currency="USD",
        observations=(obs(1, "100.00", "100.00"), obs(2, "90.00", "90.00")),
    )
    r = run_backtest([case])
    assert r.overall_mape_bps == 0
    assert r.overall_mae_minor == 0
    assert r.by_horizon[0].rmse_minor == 0


def test_bias_direction_and_mape() -> None:
    # predicted always 10% above actual → +bias, MAPE ~1000 bps
    case = BacktestCase(
        as_of=date(2026, 8, 1),
        currency="USD",
        observations=(obs(1, "110.00", "100.00"), obs(2, "220.00", "200.00")),
    )
    r = run_backtest([case])
    assert r.overall_bias_minor > 0  # over-forecast
    assert r.overall_mape_bps == 1000  # exactly 10%
    assert r.by_horizon[0].mae_minor == 1000  # $10.00 error at wk1 (minor units)


def test_error_grows_with_horizon_and_shelf_life() -> None:
    # wk1 within 2%, wk2 within 4%, wk3 at 8% → reliable through wk2 at 5% tol
    cases = [
        BacktestCase(
            as_of=date(2026, 8, 1),
            currency="USD",
            observations=(
                obs(1, "102.00", "100.00"),
                obs(2, "104.00", "100.00"),
                obs(3, "108.00", "100.00"),
            ),
        )
    ]
    r = run_backtest(cases, reliable_mape_bps=500)
    mape_by_h = {s.horizon: s.mape_bps for s in r.by_horizon}
    assert mape_by_h[1] == 200 and mape_by_h[2] == 400 and mape_by_h[3] == 800
    assert r.reliable_through_horizon == 2


def test_aggregates_across_multiple_forecasts_by_horizon() -> None:
    c1 = BacktestCase(date(2026, 8, 1), "USD", (obs(1, "100.00", "100.00"),))
    c2 = BacktestCase(date(2026, 8, 8), "USD", (obs(1, "120.00", "100.00"),))
    r = run_backtest([c1, c2])
    assert r.cases == 2 and r.observations == 2
    # wk1 MAPE averages 0% and 20% → 10%
    assert r.by_horizon[0].n == 2
    assert r.by_horizon[0].mape_bps == 1000


def test_breach_precision_and_recall() -> None:
    cases = [
        BacktestCase(date(2026, 8, 1), "USD", (obs(1, "1.00", "1.00"),), predicted_breach=True, actual_breach=True),   # TP
        BacktestCase(date(2026, 8, 8), "USD", (obs(1, "1.00", "1.00"),), predicted_breach=True, actual_breach=False),  # FP
        BacktestCase(date(2026, 8, 15), "USD", (obs(1, "1.00", "1.00"),), predicted_breach=False, actual_breach=True), # FN
        BacktestCase(date(2026, 8, 22), "USD", (obs(1, "1.00", "1.00"),), predicted_breach=False, actual_breach=False),# TN
    ]
    r = run_backtest(cases)
    assert r.breach is not None
    assert r.breach.true_positive == 1 and r.breach.false_positive == 1
    assert r.breach.precision_bps == 5000  # 1/(1+1) = 50%
    assert r.breach.recall_bps == 5000  # 1/(1+1) = 50%


def test_render_text_summarizes_the_report() -> None:
    case = BacktestCase(date(2026, 8, 1), "USD", (obs(1, "110.00", "100.00"),), predicted_breach=True, actual_breach=True)
    text = render_backtest_text(run_backtest([case]))
    assert "FORECAST BACKTEST" in text
    assert "MAPE" in text
    assert "wk  1" in text
