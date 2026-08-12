"""The unsupported-number validator.

Recomputes every numeric claim in a briefing (or a Q&A answer) from the
forecast. A projection-field claim must equal the resolved field; a flow-backed
claim must equal the summed magnitudes of the referenced cash flows. Anything
that does not reconcile is a Violation — the briefing must not be published with
any violation outstanding.
"""

from __future__ import annotations

from rgnr8_forecast import ForecastResult, Money
from rgnr8_forecast.projection import Projection

from .models import Driver, Evidence, Fact, Violation

_ZERO = Money(0)


def resolve_field(projection: Projection, field: str) -> Money:
    """Resolve a named projection value (including a few computed pseudo-fields)."""
    p = projection
    match field:
        case "opening_available":
            return p.opening_available
        case "restricted":
            return p.restricted
        case "effective_floor":
            return p.effective_floor
        case "trough_balance":
            return p.trough.balance
        case "ending_balance":
            return p.ending_balance
        case "total_inflows":
            return p.total_inflows
        case "total_outflows":
            return p.total_outflows
        case "worst_shortfall":
            return p.breach.worst_shortfall
        case "cushion_at_trough":
            return p.trough.balance - p.effective_floor
        case _:
            raise KeyError(f"Unknown projection field: {field}")


def _expected_for(evidence: Evidence, forecast: ForecastResult) -> Money:
    if evidence.kind == "field":
        assert evidence.field is not None
        return resolve_field(forecast.projection, evidence.field)
    # flows: sum the magnitudes of the referenced flows
    by_seq = {f.seq: f for f in forecast.flows}
    total = _ZERO
    for seq in evidence.flow_seqs:
        flow = by_seq.get(seq)
        if flow is None:
            raise KeyError(f"evidence references unknown flow seq {seq}")
        total = total + flow.amount
    return total


def check_fact(fact: Fact, forecast: ForecastResult) -> Violation | None:
    if fact.amount is None:
        return None  # purely textual fact, nothing to reconcile
    try:
        expected = _expected_for(fact.evidence, forecast)
    except KeyError as exc:
        return Violation(where=f"fact:{fact.key}", message=str(exc), stated=fact.amount)
    if expected != fact.amount:
        return Violation(
            where=f"fact:{fact.key}",
            message="stated amount does not match its evidence",
            stated=fact.amount,
            expected=expected,
        )
    return None


def check_driver(driver: Driver, forecast: ForecastResult) -> Violation | None:
    try:
        expected = _expected_for(driver.evidence, forecast)
    except KeyError as exc:
        return Violation(where=f"driver:{driver.category.value}", message=str(exc), stated=driver.amount)
    if expected != driver.amount:
        return Violation(
            where=f"driver:{driver.category.value}",
            message="driver total does not match referenced flows",
            stated=driver.amount,
            expected=expected,
        )
    return None


def validate_facts(facts: tuple[Fact, ...], forecast: ForecastResult) -> list[Violation]:
    out: list[Violation] = []
    for f in facts:
        v = check_fact(f, forecast)
        if v is not None:
            out.append(v)
    return out


def validate_briefing(briefing: "object", forecast: ForecastResult) -> list[Violation]:
    """Return all violations; empty list means every number is backed."""
    from .models import WeeklyBriefing

    assert isinstance(briefing, WeeklyBriefing)
    violations = validate_facts(briefing.facts, forecast)
    for d in briefing.drivers:
        v = check_driver(d, forecast)
        if v is not None:
            violations.append(v)
    return violations
