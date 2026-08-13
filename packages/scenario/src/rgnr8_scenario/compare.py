"""Diff a scenario forecast against its baseline.

Every field is a signed delta (scenario minus base) so the sign reads the way an
owner expects: a negative ``trough_delta`` or ``cushion_delta`` means the change
made things worse. ``cushion`` is headroom above the floor (``trough - floor``),
so it correctly reflects a scenario that also moved the minimum-cash floor.
"""

from __future__ import annotations

from dataclasses import dataclass

from rgnr8_forecast import ForecastResult, Money, Projection


@dataclass(frozen=True, slots=True)
class ScenarioDiff:
    trough_delta: Money
    breach_week_before: int | None
    breach_week_after: int | None
    cushion_delta: Money
    weekly_closing_deltas: tuple[Money, ...]


def _cushion(projection: Projection) -> Money:
    """Headroom of the trough above the effective floor."""
    return projection.trough.balance - projection.effective_floor


def compare(base: ForecastResult, scenario: ForecastResult) -> ScenarioDiff:
    """Signed deltas of ``scenario`` relative to ``base``.

    Assumes both were run over the same horizon (same number of weekly buckets);
    weekly deltas are paired index-for-index up to the shorter of the two.
    """
    base_proj = base.projection
    scen_proj = scenario.projection

    trough_delta = scen_proj.trough.balance - base_proj.trough.balance
    cushion_delta = _cushion(scen_proj) - _cushion(base_proj)

    weekly: tuple[Money, ...] = tuple(
        s.closing - b.closing
        for b, s in zip(base_proj.weeks, scen_proj.weeks)
    )

    return ScenarioDiff(
        trough_delta=trough_delta,
        breach_week_before=base_proj.breach.weeks_until,
        breach_week_after=scen_proj.breach.weeks_until,
        cushion_delta=cushion_delta,
        weekly_closing_deltas=weekly,
    )


__all__ = ["ScenarioDiff", "compare"]
