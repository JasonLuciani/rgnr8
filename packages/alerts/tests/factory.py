"""Deterministic `ForecastResult` fixtures for the alert tests.

Builds a real `Projection`/`ForecastResult` directly from week (opening, closing)
pairs so each test controls the trough, floor, and breach exactly — no need to
run the full forecast engine.
"""

from __future__ import annotations

from datetime import date, timedelta

from rgnr8_forecast import (
    ForecastResult,
    ForecastVersion,
    Money,
    PublicationStatus,
    Scenario,
)
from rgnr8_forecast.projection import Breach, CashTrough, Projection, WeekLine

_AS_OF = date(2026, 1, 5)  # a Monday


def _week(index: int, start: date, opening: int, closing: int, ccy: str) -> WeekLine:
    net = closing - opening
    inflows = net if net >= 0 else 0
    outflows = -net if net < 0 else 0
    return WeekLine(
        index=index,
        start=start,
        end=start + timedelta(days=6),
        opening=Money(opening, ccy),
        inflows=Money(inflows, ccy),
        outflows=Money(outflows, ccy),
        net=Money(net, ccy),
        closing=Money(closing, ccy),
        confidence=90,
        flow_seqs=(),
    )


def build_forecast(
    *,
    weeks: list[tuple[int, int]],
    floor_minor: int,
    opening_minor: int | None = None,
    breach_week: int | None = None,
    breach_shortfall_minor: int = 0,
    currency: str = "USD",
    as_of: date = _AS_OF,
) -> ForecastResult:
    """Assemble a `ForecastResult` from ``(opening, closing)`` week pairs.

    The trough is the lowest closing across all weeks. If ``breach_week`` is set,
    a breach is recorded for that 1-based week with the given worst shortfall.
    """
    ccy = currency
    opening_available = Money(
        opening_minor if opening_minor is not None else weeks[0][0], ccy
    )

    week_lines: list[WeekLine] = []
    for i, (opening, closing) in enumerate(weeks):
        start = as_of + timedelta(days=7 * i)
        week_lines.append(_week(i + 1, start, opening, closing, ccy))

    trough_line = min(week_lines, key=lambda w: w.closing.minor_units)
    trough = CashTrough(
        on_date=trough_line.end, balance=Money(trough_line.closing.minor_units, ccy)
    )

    if breach_week is not None:
        first_date = as_of + timedelta(days=7 * (breach_week - 1) + 2)
        breach = Breach(
            breached=True,
            floor=Money(floor_minor, ccy),
            worst_shortfall=Money(breach_shortfall_minor, ccy),
            first_date=first_date,
            weeks_until=breach_week,
            recovery_date=None,
        )
    else:
        breach = Breach(
            breached=False,
            floor=Money(floor_minor, ccy),
            worst_shortfall=Money(0, ccy),
            first_date=None,
            weeks_until=None,
            recovery_date=None,
        )

    total_in = sum(w.inflows.minor_units for w in week_lines)
    total_out = sum(w.outflows.minor_units for w in week_lines)

    projection = Projection(
        currency=ccy,
        as_of=as_of,
        opening_available=opening_available,
        restricted=Money(0, ccy),
        effective_floor=Money(floor_minor, ccy),
        weeks=tuple(week_lines),
        total_inflows=Money(total_in, ccy),
        total_outflows=Money(total_out, ccy),
        ending_balance=Money(week_lines[-1].closing.minor_units, ccy),
        trough=trough,
        breach=breach,
    )

    version = ForecastVersion(
        engine_version="test",
        assumption_version="test",
        mapping_version="test",
        model_version="test",
        timezone="UTC",
        rounding_policy="half_even",
        scenario=Scenario.BASE,
        input_fingerprint="0" * 64,
        status=PublicationStatus.PRELIMINARY,
    )

    return ForecastResult(
        scenario=Scenario.BASE,
        version=version,
        projection=projection,
        flows=(),
        overall_confidence=90,
        data_quality=(),
        headline="test",
        recommended_action=None,
    )
