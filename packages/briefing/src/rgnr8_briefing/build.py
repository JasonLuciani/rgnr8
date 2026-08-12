"""Assemble a WeeklyBriefing from a ForecastResult.

Each numeric fact is constructed together with the evidence that backs it, so
``validate_briefing`` reconciles cleanly. Status thresholds are deliberately
simple and tunable — a controller should review the WATCH band before launch.
"""

from __future__ import annotations

from rgnr8_forecast import Category, CashFlow, Direction, ForecastResult, Money

from .models import (
    Driver,
    Evidence,
    Fact,
    StatusLevel,
    WeeklyBriefing,
    WeekGlance,
)

# WATCH when the trough cushion is thinner than this share of the floor.
WATCH_CUSHION_BPS = 1500  # 15%
WATCH_CONFIDENCE = 60


def _status_for_balance(closing: Money, floor: Money, watch_band: Money) -> StatusLevel:
    if closing < floor:
        return StatusLevel.AT_RISK
    if (closing - floor) < watch_band:
        return StatusLevel.WATCH
    return StatusLevel.STABLE


def build_briefing(forecast: ForecastResult) -> WeeklyBriefing:
    p = forecast.projection
    ccy = p.currency
    floor = p.effective_floor
    watch_band = floor.scale_by(WATCH_CUSHION_BPS, 10000)
    cushion = p.trough.balance - floor

    # --- overall status ------------------------------------------------------
    if p.breach.breached:
        status = StatusLevel.AT_RISK
        reason = (
            f"Projected to fall below the {ccy} {floor.to_decimal_string()} floor "
            f"in week {p.breach.weeks_until}."
        )
    elif cushion < watch_band or forecast.overall_confidence < WATCH_CONFIDENCE or forecast.data_quality:
        status = StatusLevel.WATCH
        bits = []
        if cushion < watch_band:
            bits.append(f"cushion at the low point is only {ccy} {cushion.to_decimal_string()}")
        if forecast.overall_confidence < WATCH_CONFIDENCE:
            bits.append(f"confidence is {forecast.overall_confidence}/100")
        if forecast.data_quality:
            bits.append("there are open data-quality items")
        reason = "Stays above the floor, but " + "; ".join(bits) + "."
    else:
        status = StatusLevel.STABLE
        reason = (
            f"Cash stays comfortably above the {ccy} {floor.to_decimal_string()} floor "
            f"for all {len(p.weeks)} weeks."
        )

    # --- facts (each with backing evidence) ----------------------------------
    facts: list[Fact] = [
        Fact("cash_today", "Cash available today", Evidence("field", field="opening_available"),
             amount=p.opening_available),
        Fact("floor", "Your minimum-cash floor", Evidence("field", field="effective_floor"),
             amount=floor,
             text=(f"includes {ccy} {p.restricted.to_decimal_string()} restricted"
                   if p.restricted.is_positive else None)),
        Fact("low_point", "Projected low point", Evidence("field", field="trough_balance"),
             amount=p.trough.balance, text=f"around {p.trough.on_date.isoformat()}",
             confidence=forecast.overall_confidence),
        Fact("cushion", "Cushion at the low point", Evidence("field", field="cushion_at_trough"),
             amount=cushion),
        Fact("total_in", "Money in over 13 weeks", Evidence("field", field="total_inflows"),
             amount=p.total_inflows),
        Fact("total_out", "Money out over 13 weeks", Evidence("field", field="total_outflows"),
             amount=p.total_outflows),
    ]
    if p.breach.breached and p.breach.first_date is not None:
        facts.append(
            Fact("shortfall", "Largest projected shortfall",
                 Evidence("field", field="worst_shortfall"),
                 amount=p.breach.worst_shortfall,
                 text=f"first around {p.breach.first_date.isoformat()} (week {p.breach.weeks_until})")
        )

    biggest_in = _largest(forecast.flows, Direction.INFLOW)
    if biggest_in is not None:
        facts.append(
            Fact("largest_receipt", "Largest upcoming receipt",
                 Evidence("flows", flow_seqs=(biggest_in.seq,)),
                 amount=biggest_in.amount,
                 text=f"{biggest_in.origin_id} on {biggest_in.on_date.isoformat()}",
                 confidence=None)
        )
    biggest_out = _largest(forecast.flows, Direction.OUTFLOW)
    if biggest_out is not None:
        facts.append(
            Fact("largest_outflow", "Largest upcoming outflow",
                 Evidence("flows", flow_seqs=(biggest_out.seq,)),
                 amount=biggest_out.amount,
                 text=f"{biggest_out.category.value} on {biggest_out.on_date.isoformat()}")
        )

    # --- drivers: what is draining cash --------------------------------------
    drivers = _outflow_drivers(forecast.flows, p.total_outflows, ccy)

    # --- week glance ---------------------------------------------------------
    glance = tuple(
        WeekGlance(
            index=w.index,
            start=w.start,
            closing=w.closing,
            status=_status_for_balance(w.closing, floor, watch_band),
            confidence=w.confidence,
        )
        for w in p.weeks
    )

    return WeeklyBriefing(
        as_of=p.as_of,
        currency=ccy,
        period_label=f"{p.as_of.isoformat()} – {p.weeks[-1].end.isoformat()}",
        status=status,
        status_reason=reason,
        headline=forecast.headline,
        primary_action=forecast.recommended_action,
        facts=tuple(facts),
        drivers=drivers,
        week_glance=glance,
        data_quality=forecast.data_quality,
        overall_confidence=forecast.overall_confidence,
        version_id=forecast.version.version_id,
    )


def _largest(flows: tuple[CashFlow, ...], direction: Direction) -> CashFlow | None:
    candidates = [f for f in flows if f.direction is direction]
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f.amount.minor_units, -f.seq))


def _outflow_drivers(
    flows: tuple[CashFlow, ...], total_out: Money, ccy: str, top: int = 3
) -> tuple[Driver, ...]:
    by_cat: dict[Category, list[CashFlow]] = {}
    for f in flows:
        if f.direction is Direction.OUTFLOW:
            by_cat.setdefault(f.category, []).append(f)

    rows: list[Driver] = []
    for cat, fs in by_cat.items():
        total = Money(sum(f.amount.minor_units for f in fs), ccy)
        share = (
            round(total.minor_units * 10000 / total_out.minor_units)
            if total_out.minor_units > 0
            else 0
        )
        rows.append(
            Driver(
                category=cat,
                label=_category_label(cat),
                direction=Direction.OUTFLOW,
                amount=total,
                share_bps=share,
                evidence=Evidence("flows", flow_seqs=tuple(sorted(f.seq for f in fs))),
            )
        )
    rows.sort(key=lambda d: d.amount.minor_units, reverse=True)
    return tuple(rows[:top])


def _category_label(cat: Category) -> str:
    return cat.value.replace("_", " ").title()
