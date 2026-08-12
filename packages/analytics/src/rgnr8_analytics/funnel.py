"""Funnel report — step-by-step conversion from a set of recorded events.

Given a flat stream of `ProductEvent`s and an ordered list of funnel `steps`,
`funnel_report` answers the one question the acquisition→activation funnel exists
to answer: of everyone who entered, how many reached each subsequent step, and
what fraction is that?

The counting rule is *reached-this-step*, grouped by `distinct_id`: a user counts
toward a step if they have at least one event for that step's name (order across
users doesn't matter, and duplicate events for the same user collapse to one). The
conversion percentage is relative to the *first* step — the top of the funnel —
so the numbers read as "X% of everyone who started got this far". The first step
is 100% by construction (or 0% when nobody entered).

Pure and deterministic: no clock, no I/O, no mutation of the inputs. Counts are
monotonic only if the underlying data is; the report reflects the data as given,
which is what makes it a faithful measurement rather than a smoothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .events import EventName, ProductEvent


def funnel_report(
    events: Iterable[ProductEvent],
    steps: Sequence[EventName],
) -> list[tuple[EventName, int, float]]:
    """Compute (step, distinct-users-reached, conversion-pct-of-first-step).

    `events` is any iterable of recorded events; `steps` is the ordered funnel.
    Conversion percent is each step's reached-count as a percentage of the first
    step's count, rounded to two decimals. An empty `steps` yields an empty
    report; a first step nobody reached yields 0.0% for every step.
    """
    # distinct_ids that reached each step name (dedupes repeat events per user).
    reached: dict[EventName, set[str]] = {step: set() for step in steps}
    wanted = set(steps)
    for event in events:
        if event.name in wanted:
            reached[event.name].add(event.distinct_id)

    report: list[tuple[EventName, int, float]] = []
    if not steps:
        return report

    base = len(reached[steps[0]])
    for step in steps:
        count = len(reached[step])
        pct = round(100.0 * count / base, 2) if base else 0.0
        report.append((step, count, pct))
    return report
