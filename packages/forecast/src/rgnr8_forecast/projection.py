"""The direct-method projection: opening cash + dated flows -> liquidity path.

Begins with verified available cash and walks dated inflows/outflows day by day.
It does not derive cash from a forecasted income statement. Produces weekly
buckets, the cash trough (the lowest point and when it happens), and whether/
when the minimum-cash floor is breached.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .dates import week_buckets
from .enums import CONFIDENCE_WEIGHT, Direction
from .flow import CashFlow
from .models import ForecastConfig
from .money import Money


@dataclass(frozen=True, slots=True)
class WeekLine:
    index: int
    start: date
    end: date
    opening: Money
    inflows: Money
    outflows: Money  # positive magnitude
    net: Money
    closing: Money
    confidence: int  # 0..100
    flow_seqs: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CashTrough:
    on_date: date
    balance: Money


@dataclass(frozen=True, slots=True)
class Breach:
    breached: bool
    floor: Money
    worst_shortfall: Money  # floor - min balance, >= 0 (0 if never breached)
    first_date: date | None = None
    weeks_until: int | None = None
    recovery_date: date | None = None


@dataclass(frozen=True, slots=True)
class Projection:
    currency: str
    as_of: date
    opening_available: Money
    restricted: Money
    effective_floor: Money
    weeks: tuple[WeekLine, ...]
    total_inflows: Money
    total_outflows: Money
    ending_balance: Money
    trough: CashTrough
    breach: Breach


def compute_projection(
    opening_available: Money,
    restricted: Money,
    flows: list[CashFlow],
    config: ForecastConfig,
    as_of: date,
) -> Projection:
    ccy = config.currency
    weeks = week_buckets(as_of, config.horizon_weeks)
    horizon_end = weeks[-1].end

    # Daily signed deltas and per-day inflow/outflow magnitudes (minor units).
    daily_delta: dict[date, int] = {}
    daily_in: dict[date, int] = {}
    daily_out: dict[date, int] = {}
    # Per-week confidence accumulation (weight by |amount|).
    for f in flows:
        if not (as_of <= f.on_date <= horizon_end):
            continue
        daily_delta[f.on_date] = daily_delta.get(f.on_date, 0) + f.signed_minor
        if f.direction is Direction.INFLOW:
            daily_in[f.on_date] = daily_in.get(f.on_date, 0) + f.amount.minor_units
        else:
            daily_out[f.on_date] = daily_out.get(f.on_date, 0) + f.amount.minor_units

    floor = config.minimum_cash + restricted

    # Walk every day, tracking the running balance and the trough.
    balance = opening_available.minor_units
    trough_val = balance
    trough_date = as_of
    first_breach: date | None = None
    recovery: date | None = None
    worst_shortfall = 0

    # candidate trough at opening (before day-0 flows already included below via day loop)
    total_days = config.horizon_weeks * 7
    for i in range(total_days):
        day = as_of + timedelta(days=i)
        balance += daily_delta.get(day, 0)
        if balance < trough_val:
            trough_val = balance
            trough_date = day
        if balance < floor.minor_units:
            if first_breach is None:
                first_breach = day
            recovery = None  # still below floor
            shortfall = floor.minor_units - balance
            if shortfall > worst_shortfall:
                worst_shortfall = shortfall
        elif first_breach is not None and recovery is None:
            recovery = day

    # Build weekly lines.
    week_lines: list[WeekLine] = []
    running = opening_available.minor_units
    seqs_by_week: dict[int, list[int]] = {w.index: [] for w in weeks}
    for f in flows:
        for w in weeks:
            if w.contains(f.on_date):
                seqs_by_week[w.index].append(f.seq)
                break

    for w in weeks:
        opening = running
        w_in = sum(daily_in.get(w.start + timedelta(days=k), 0) for k in range(7))
        w_out = sum(daily_out.get(w.start + timedelta(days=k), 0) for k in range(7))
        net = w_in - w_out
        running = opening + net
        week_flows = [f for f in flows if w.contains(f.on_date)]
        conf = _confidence(week_flows)
        week_lines.append(
            WeekLine(
                index=w.index,
                start=w.start,
                end=w.end,
                opening=Money(opening, ccy),
                inflows=Money(w_in, ccy),
                outflows=Money(w_out, ccy),
                net=Money(net, ccy),
                closing=Money(running, ccy),
                confidence=conf,
                flow_seqs=tuple(sorted(seqs_by_week[w.index])),
            )
        )

    total_in = sum(daily_in.values())
    total_out = sum(daily_out.values())

    return Projection(
        currency=ccy,
        as_of=as_of,
        opening_available=opening_available,
        restricted=restricted,
        effective_floor=floor,
        weeks=tuple(week_lines),
        total_inflows=Money(total_in, ccy),
        total_outflows=Money(total_out, ccy),
        ending_balance=Money(running, ccy),
        trough=CashTrough(on_date=trough_date, balance=Money(trough_val, ccy)),
        breach=Breach(
            breached=first_breach is not None,
            floor=floor,
            worst_shortfall=Money(worst_shortfall, ccy),
            first_date=first_breach,
            weeks_until=(
                ((first_breach - as_of).days // 7) + 1 if first_breach is not None else None
            ),
            recovery_date=recovery,
        ),
    )


def _confidence(flows: list[CashFlow]) -> int:
    """Amount-weighted confidence (0..100). No flows -> 100 (just carrying cash)."""
    total_weight = 0
    weighted = 0
    for f in flows:
        w = f.amount.minor_units
        total_weight += w
        weighted += w * CONFIDENCE_WEIGHT[f.confidence]
    if total_weight == 0:
        return 100
    return round(weighted / total_weight)
