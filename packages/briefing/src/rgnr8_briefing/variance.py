"""Variance: compare current actuals to a prior *published* forecast.

The retention loop compares actual results with prior expectations. Because the
forecast engine freezes published versions, we can hold a published forecast's
weekly closing balances against what actually happened and report how accurate
it was — the basis for the "did the action change the outcome" narrative and the
forecast-accuracy case study.
"""

from __future__ import annotations

from dataclasses import dataclass

from rgnr8_forecast import ForecastResult, Money, PublicationStatus


@dataclass(frozen=True, slots=True)
class WeekActual:
    """The actual closing cash for an elapsed week (index matches the forecast)."""

    index: int
    closing: Money


@dataclass(frozen=True, slots=True)
class WeekVariance:
    index: int
    expected: Money
    actual: Money
    delta: Money  # actual - expected (positive = better than forecast)
    pct_bps: int | None  # delta / |expected|, in basis points; None if expected is 0


@dataclass(frozen=True, slots=True)
class VarianceReport:
    currency: str
    weeks: tuple[WeekVariance, ...]
    mean_abs_pct_bps: int | None
    within_tolerance: bool
    tolerance_bps: int
    forecast_version_id: str
    narrative: str


def compute_variance(
    published: ForecastResult,
    actuals: list[WeekActual],
    tolerance_bps: int = 1000,
) -> VarianceReport:
    """Compare actuals to the published forecast's weekly closings.

    ``tolerance_bps`` is the mean-absolute-percent-error threshold (default 10%)
    under which the forecast is considered to have tracked reality.
    """
    ccy = published.projection.currency
    by_index = {w.index: w for w in published.projection.weeks}

    rows: list[WeekVariance] = []
    abs_pcts: list[int] = []
    for a in sorted(actuals, key=lambda x: x.index):
        wk = by_index.get(a.index)
        if wk is None:
            continue
        expected = wk.closing
        delta = a.closing - expected
        if expected.minor_units != 0:
            pct = round(delta.minor_units * 10000 / abs(expected.minor_units))
            abs_pcts.append(abs(pct))
            pct_bps: int | None = pct
        else:
            pct_bps = None
        rows.append(WeekVariance(index=a.index, expected=expected, actual=a.closing, delta=delta, pct_bps=pct_bps))

    mean_abs = round(sum(abs_pcts) / len(abs_pcts)) if abs_pcts else None
    within = mean_abs is not None and mean_abs <= tolerance_bps

    if not rows:
        narrative = "No elapsed weeks to compare yet."
    elif mean_abs is None:
        narrative = f"Compared {len(rows)} week(s); expected balances were zero, so percent error is undefined."
    else:
        direction = "tracked" if within else "diverged from"
        worst = max(rows, key=lambda r: abs(r.delta.minor_units))
        narrative = (
            f"Over {len(rows)} elapsed week(s), actuals {direction} the forecast "
            f"(mean error {mean_abs / 100:.1f}%). Largest gap was week {worst.index}: "
            f"actual {ccy} {worst.actual.to_decimal_string()} vs expected "
            f"{ccy} {worst.expected.to_decimal_string()} "
            f"({'+' if worst.delta.minor_units >= 0 else ''}{ccy} {worst.delta.to_decimal_string()})."
        )
        if published.status is not PublicationStatus.PUBLISHED:
            narrative += " (Note: compared against a non-published forecast.)"

    return VarianceReport(
        currency=ccy,
        weeks=tuple(rows),
        mean_abs_pct_bps=mean_abs,
        within_tolerance=within,
        tolerance_bps=tolerance_bps,
        forecast_version_id=published.version.version_id,
        narrative=narrative,
    )


def render_variance_text(report: VarianceReport) -> str:
    ccy = report.currency
    lines = ["VS. LAST PUBLISHED FORECAST", "  " + report.narrative]
    for w in report.weeks:
        pct = f"{w.pct_bps / 100:+.1f}%" if w.pct_bps is not None else "n/a"
        lines.append(
            f"  wk{w.index:<2} expected {ccy} {w.expected.to_decimal_string():>12}  "
            f"actual {ccy} {w.actual.to_decimal_string():>12}  ({pct})"
        )
    return "\n".join(lines)
