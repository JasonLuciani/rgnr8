"""Prioritized collections chase list.

Ranks overdue invoices by how much money is at stake, how long it has been late,
and how risky the customer is (from their historical payment timing), so an owner
works the highest-impact accounts first. Deterministic and stdlib-only.

Score = open amount (minor units) x days overdue x risk weight.

The risk weight is derived from the customer's typical days-late: a chronically
late payer is weighted above an otherwise-identical on-time payer, bumping them up
the list. Only invoices whose due date is strictly before ``as_of`` appear.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping

from rgnr8_forecast import CustomerHistory, Invoice

from .aging import AgingBucket, bucket_for, days_overdue

# Neutral weight (1.00x) for a customer who pays on time or has no history.
NEUTRAL_RISK_WEIGHT = 100
# Cap the late-days contribution so one pathological account cannot dominate.
_MAX_RISK_BONUS = 100


def typical_days_late(history: CustomerHistory | None) -> int:
    """A customer's representative days-late, mirroring the forecast engine's rule.

    An explicit ``override_days_late`` wins; otherwise the median of observed
    (due, paid) lateness; with no signal at all, 0 (assume on-time).
    """
    if history is None:
        return 0
    if history.override_days_late is not None:
        return history.override_days_late
    samples = [o.days_late for o in history.observations]
    if not samples:
        return 0
    return int(round(statistics.median(samples)))


def risk_weight(history: CustomerHistory | None) -> int:
    """Map payment history to an integer weight >= ``NEUTRAL_RISK_WEIGHT``.

    On-time or early payers sit at the neutral weight; each day a customer is
    typically late adds one point (capped), so a 40-day-late payer is weighted
    1.40x an on-time payer of the same invoice.
    """
    late = typical_days_late(history)
    bonus = max(0, min(late, _MAX_RISK_BONUS))
    return NEUTRAL_RISK_WEIGHT + bonus


@dataclass(frozen=True, slots=True)
class ChaseItem:
    """One overdue invoice, scored for collection priority."""

    invoice: Invoice
    days_overdue: int
    bucket: AgingBucket
    risk_score: int  # the customer's risk weight (>= NEUTRAL_RISK_WEIGHT)
    priority: int  # amount(minor) x days_overdue x risk_score; higher = chase first


def chase_list(
    invoices: Iterable[Invoice],
    as_of: date,
    *,
    histories: Mapping[str, CustomerHistory] | None = None,
) -> list[ChaseItem]:
    """Rank overdue invoices most-urgent-first as of ``as_of``.

    Excludes any invoice not yet due (``due_date >= as_of``). Ordering is
    deterministic: by priority descending, then days overdue, then open amount,
    then invoice id — so ties never depend on input order.
    """
    hist = histories or {}
    items: list[ChaseItem] = []
    for inv in invoices:
        overdue = days_overdue(inv.due_date, as_of)
        if overdue <= 0:
            continue
        weight = risk_weight(hist.get(inv.customer_id))
        priority = inv.open_amount.minor_units * overdue * weight
        items.append(
            ChaseItem(
                invoice=inv,
                days_overdue=overdue,
                bucket=bucket_for(inv.due_date, as_of),
                risk_score=weight,
                priority=priority,
            )
        )
    items.sort(
        key=lambda it: (
            it.priority,
            it.days_overdue,
            it.invoice.open_amount.minor_units,
            it.invoice.id,
        ),
        reverse=True,
    )
    return items
