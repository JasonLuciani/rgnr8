"""Accounts-receivable aging.

Classifies each open invoice into an aging bucket by how many days past its due
date it sits *as of an injected anchor date* (never the wall clock), and rolls the
open amounts up into a per-bucket summary. Deterministic and stdlib-only.

Bucket boundaries (days overdue = ``as_of - due_date``):
  * ``CURRENT``   — not yet due / due today (days overdue <= 0)
  * ``D1_30``     — 1..30 days overdue
  * ``D31_60``    — 31..60 days overdue
  * ``D60_PLUS``  — more than 60 days overdue
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Iterable

from rgnr8_forecast import Invoice, Money


class AgingBucket(str, Enum):
    CURRENT = "CURRENT"
    D1_30 = "D1_30"
    D31_60 = "D31_60"
    D60_PLUS = "D60_PLUS"


# Buckets in ascending age order — the canonical iteration order everywhere.
BUCKET_ORDER: tuple[AgingBucket, ...] = (
    AgingBucket.CURRENT,
    AgingBucket.D1_30,
    AgingBucket.D31_60,
    AgingBucket.D60_PLUS,
)


def days_overdue(due_date: date, as_of: date) -> int:
    """Signed days past due: positive when overdue, <= 0 when current/not-yet-due."""
    return (as_of - due_date).days


def bucket_for(due_date: date, as_of: date) -> AgingBucket:
    """Classify an invoice's due date into an aging bucket relative to ``as_of``."""
    overdue = days_overdue(due_date, as_of)
    if overdue <= 0:
        return AgingBucket.CURRENT
    if overdue <= 30:
        return AgingBucket.D1_30
    if overdue <= 60:
        return AgingBucket.D31_60
    return AgingBucket.D60_PLUS


@dataclass(frozen=True, slots=True)
class AgingSummary:
    """Open AR totalled per bucket, plus a grand total that reconciles to the sum.

    ``grand_total`` is exactly the sum of the four bucket totals, so an AR report
    can assert buckets sum to total AR without recomputing.
    """

    as_of: date
    current: Money
    d1_30: Money
    d31_60: Money
    d60_plus: Money
    grand_total: Money

    def amount(self, bucket: AgingBucket) -> Money:
        """Total open amount in one bucket."""
        if bucket is AgingBucket.CURRENT:
            return self.current
        if bucket is AgingBucket.D1_30:
            return self.d1_30
        if bucket is AgingBucket.D31_60:
            return self.d31_60
        return self.d60_plus

    def as_mapping(self) -> dict[AgingBucket, Money]:
        """The per-bucket totals as a fresh dict in ascending-age order."""
        return {bucket: self.amount(bucket) for bucket in BUCKET_ORDER}

    @property
    def overdue_total(self) -> Money:
        """Everything past due — grand total minus the current bucket."""
        return self.grand_total - self.current


def summarize_aging(
    invoices: Iterable[Invoice],
    as_of: date,
    *,
    currency: str = "USD",
) -> AgingSummary:
    """Roll a set of open invoices into an :class:`AgingSummary` as of ``as_of``."""
    totals: dict[AgingBucket, Money] = {b: Money.zero(currency) for b in BUCKET_ORDER}
    for inv in invoices:
        bucket = bucket_for(inv.due_date, as_of)
        totals[bucket] = totals[bucket] + inv.open_amount
    grand = Money.zero(currency)
    for bucket in BUCKET_ORDER:
        grand = grand + totals[bucket]
    return AgingSummary(
        as_of=as_of,
        current=totals[AgingBucket.CURRENT],
        d1_30=totals[AgingBucket.D1_30],
        d31_60=totals[AgingBucket.D31_60],
        d60_plus=totals[AgingBucket.D60_PLUS],
        grand_total=grand,
    )
