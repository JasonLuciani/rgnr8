"""Forecast backtesting — turn "trust our forecast" into a measured claim.

A backtest replays past **published** forecasts against what actually happened.
Forecast error grows with how far out you look, so the harness groups every
prediction by its **horizon** (weeks-ahead = the week's position in that
forecast's 13-week projection) and reports accuracy per horizon: mean absolute
error, mean absolute percentage error (MAPE, in basis points), signed bias
(do we systematically over- or under-forecast), and RMSE. It also scores the
headline call — did we correctly predict the minimum-cash **breaches** that
occurred, and how often did we cry wolf.

Everything is exact integer arithmetic on minor units — MAPE as integer basis
points, RMSE via ``math.isqrt`` — so a backtest report is deterministic and
reproducible, the same discipline as the forecast core.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from .money import Money
from .result import ForecastResult


@dataclass(frozen=True, slots=True)
class WeekObservation:
    """One predicted-vs-actual closing balance at a given horizon (weeks ahead)."""

    horizon: int  # 1..N (position within the forecast's projection)
    predicted: Money
    actual: Money

    @property
    def error_minor(self) -> int:
        """Signed error: predicted − actual, in minor units."""
        return self.predicted.minor_units - self.actual.minor_units


@dataclass(frozen=True, slots=True)
class BacktestCase:
    """A single published forecast scored against realized actuals."""

    as_of: date
    currency: str
    observations: tuple[WeekObservation, ...]
    predicted_breach: bool | None = None
    actual_breach: bool | None = None


def case_from_forecast(
    published: ForecastResult,
    actual_closings: Sequence[Money],
    *,
    actual_breach: bool | None = None,
) -> BacktestCase:
    """Build a case by zipping a published forecast's weekly closings with the
    realized closing balances (aligned by week; truncated to the shorter)."""
    weeks = published.projection.weeks
    obs = tuple(
        WeekObservation(horizon=w.index, predicted=w.closing, actual=a)
        for w, a in zip(weeks, actual_closings)
    )
    return BacktestCase(
        as_of=published.projection.as_of,
        currency=published.projection.currency,
        observations=obs,
        predicted_breach=published.projection.breach.breached,
        actual_breach=actual_breach,
    )


@dataclass(frozen=True, slots=True)
class HorizonStat:
    horizon: int
    n: int
    mae_minor: int  # mean absolute error
    mape_bps: int  # mean absolute percentage error, basis points (100 = 1%)
    bias_minor: int  # mean signed error (pred − actual); >0 = over-forecast
    rmse_minor: int  # root-mean-square error

    def mae(self, currency: str = "USD") -> Money:
        return Money(self.mae_minor, currency)


@dataclass(frozen=True, slots=True)
class BreachScore:
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int

    @property
    def scored(self) -> int:
        return self.true_positive + self.false_positive + self.false_negative + self.true_negative

    @property
    def precision_bps(self) -> int | None:
        denom = self.true_positive + self.false_positive
        return (self.true_positive * 10000) // denom if denom else None

    @property
    def recall_bps(self) -> int | None:
        denom = self.true_positive + self.false_negative
        return (self.true_positive * 10000) // denom if denom else None


@dataclass(frozen=True, slots=True)
class BacktestReport:
    currency: str
    cases: int
    observations: int
    by_horizon: tuple[HorizonStat, ...]
    overall_mae_minor: int
    overall_mape_bps: int
    overall_bias_minor: int
    breach: BreachScore | None = None
    # convenience: horizons where MAPE crosses a tolerance (accuracy "shelf life")
    reliable_through_horizon: int | None = None


def _horizon_stat(horizon: int, errs: list[int], actuals: list[int]) -> HorizonStat:
    n = len(errs)
    abs_errs = [e if e >= 0 else -e for e in errs]
    mae = sum(abs_errs) // n
    bias = sum(errs) // n
    rmse = math.isqrt(sum(e * e for e in errs) // n)
    # MAPE in basis points, per-observation then averaged; skip zero actuals.
    pcts = [
        (ae * 10000) // (a if a >= 0 else -a)
        for ae, a in zip(abs_errs, actuals)
        if a != 0
    ]
    mape = sum(pcts) // len(pcts) if pcts else 0
    return HorizonStat(horizon=horizon, n=n, mae_minor=mae, mape_bps=mape, bias_minor=bias, rmse_minor=rmse)


def run_backtest(
    cases: Sequence[BacktestCase],
    *,
    reliable_mape_bps: int = 500,
) -> BacktestReport:
    """Aggregate cases into a per-horizon accuracy report. ``reliable_mape_bps``
    (default 5%) sets the threshold used to report the horizon through which the
    forecast stays within tolerance — its practical 'shelf life'."""
    currency = cases[0].currency if cases else "USD"
    by_h_err: dict[int, list[int]] = {}
    by_h_actual: dict[int, list[int]] = {}
    all_errs: list[int] = []
    all_actuals: list[int] = []
    n_obs = 0

    for c in cases:
        for o in c.observations:
            by_h_err.setdefault(o.horizon, []).append(o.error_minor)
            by_h_actual.setdefault(o.horizon, []).append(o.actual.minor_units)
            all_errs.append(o.error_minor)
            all_actuals.append(o.actual.minor_units)
            n_obs += 1

    horizons = sorted(by_h_err)
    stats = tuple(_horizon_stat(h, by_h_err[h], by_h_actual[h]) for h in horizons)

    overall_mae = (sum(e if e >= 0 else -e for e in all_errs) // n_obs) if n_obs else 0
    overall_bias = (sum(all_errs) // n_obs) if n_obs else 0
    overall_pcts = [
        ((e if e >= 0 else -e) * 10000) // (a if a >= 0 else -a)
        for e, a in zip(all_errs, all_actuals)
        if a != 0
    ]
    overall_mape = (sum(overall_pcts) // len(overall_pcts)) if overall_pcts else 0

    # shelf life: the last leading horizon whose MAPE stays within tolerance
    reliable_through: int | None = None
    for s in stats:
        if s.mape_bps <= reliable_mape_bps:
            reliable_through = s.horizon
        else:
            break

    breach = _breach_score(cases)

    return BacktestReport(
        currency=currency,
        cases=len(cases),
        observations=n_obs,
        by_horizon=stats,
        overall_mae_minor=overall_mae,
        overall_mape_bps=overall_mape,
        overall_bias_minor=overall_bias,
        breach=breach,
        reliable_through_horizon=reliable_through,
    )


def _breach_score(cases: Sequence[BacktestCase]) -> BreachScore | None:
    tp = fp = fn = tn = 0
    scored = False
    for c in cases:
        if c.predicted_breach is None or c.actual_breach is None:
            continue
        scored = True
        if c.predicted_breach and c.actual_breach:
            tp += 1
        elif c.predicted_breach and not c.actual_breach:
            fp += 1
        elif not c.predicted_breach and c.actual_breach:
            fn += 1
        else:
            tn += 1
    return BreachScore(tp, fp, fn, tn) if scored else None


def _bps_pct(bps: int) -> str:
    return f"{bps / 100:.2f}%"


def render_backtest_text(report: BacktestReport) -> str:
    lines = [
        "FORECAST BACKTEST",
        f"  {report.cases} forecast(s), {report.observations} week-observations · {report.currency}",
        f"  overall MAPE {_bps_pct(report.overall_mape_bps)} · "
        f"MAE {Money(report.overall_mae_minor, report.currency).to_decimal_string()} · "
        f"bias {Money(report.overall_bias_minor, report.currency).to_decimal_string()} "
        f"({'over' if report.overall_bias_minor > 0 else 'under'}-forecast)",
    ]
    if report.reliable_through_horizon is not None:
        lines.append(f"  reliable through week {report.reliable_through_horizon} (within tolerance)")
    lines.append("  horizon   n   MAPE     MAE        bias        RMSE")
    for s in report.by_horizon:
        lines.append(
            f"   wk {s.horizon:>2}  {s.n:>3}  {_bps_pct(s.mape_bps):>6}  "
            f"{Money(s.mae_minor, report.currency).to_decimal_string():>9}  "
            f"{Money(s.bias_minor, report.currency).to_decimal_string():>10}  "
            f"{Money(s.rmse_minor, report.currency).to_decimal_string():>9}"
        )
    if report.breach is not None:
        b = report.breach
        p = _bps_pct(b.precision_bps) if b.precision_bps is not None else "n/a"
        r = _bps_pct(b.recall_bps) if b.recall_bps is not None else "n/a"
        lines.append(f"  breach calls: precision {p} · recall {r} (TP {b.true_positive} FP {b.false_positive} FN {b.false_negative} TN {b.true_negative})")
    return "\n".join(lines)
