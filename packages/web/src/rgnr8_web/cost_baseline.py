"""A cost baseline from what the business actually spent.

With no costs in the outlook, the forecast is today's balance drawn as a flat
line: it can't say how long the cash lasts, and its confidence is capped at
20/100. Owners rarely type their costs in, but QuickBooks already knows them.
This reads the last three full months of spending from the Profit and Loss
report, averages it to a month, and offers it as one recurring cost the owner
adds in a single step. It is the owner's choice, kept in their saved state, and
labelled as an average of real spending, not a promise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from rgnr8_forecast import (
    Category,
    Direction,
    Frequency,
    Money,
    Recurrence,
    RecurringItem,
)

MONTHS = 3
# Shown on the page: enough to recognise the business, not a whole P&L.
TOP_LINES = 8


@dataclass(frozen=True, slots=True)
class CostBaseline:
    """Average monthly spending over [start, end], with the biggest accounts."""

    monthly: Money
    start: date
    end: date
    lines: tuple[tuple[str, Money], ...]  # (account, monthly average), largest first

    @property
    def period_label(self) -> str:
        return f"{self.start:%b} – {self.end:%b %Y}"

    def to_dict(self) -> dict[str, object]:
        return {
            "monthly_minor": self.monthly.minor_units,
            "currency": self.monthly.currency,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "lines": [[name, m.minor_units] for name, m in self.lines],
        }

    @staticmethod
    def from_dict(d: "dict[str, Any] | None") -> "CostBaseline | None":
        if not isinstance(d, dict):
            return None
        try:
            currency = str(d["currency"])
            return CostBaseline(
                monthly=Money(int(d["monthly_minor"]), currency),
                start=date.fromisoformat(str(d["start"])),
                end=date.fromisoformat(str(d["end"])),
                lines=tuple((str(n), Money(int(m), currency))
                            for n, m in d.get("lines") or []),
            )
        except (KeyError, TypeError, ValueError):
            return None  # an unreadable saved baseline is no baseline, not a crash


def window(today: date) -> tuple[date, date]:
    """The last MONTHS full calendar months before `today`'s month."""
    end = today.replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    for _ in range(MONTHS - 1):
        start = (start - timedelta(days=1)).replace(day=1)
    return start, end


def from_quickbooks(client: Any, today: date, currency: str) -> "CostBaseline | None":
    """The baseline from the connected QuickBooks company, or None when it spent
    nothing in the window (a brand-new company has no history to average)."""
    start, end = window(today)
    totals: dict[str, int] = {}
    for name, minor in client.expense_lines(start=start.isoformat(), end=end.isoformat()):
        totals[name] = totals.get(name, 0) + minor
    total = sum(totals.values())
    if total <= 0:
        return None
    ranked = sorted(((n, m) for n, m in totals.items() if m > 0), key=lambda x: -x[1])
    return CostBaseline(
        monthly=Money(round(total / MONTHS), currency),
        start=start,
        end=end,
        lines=tuple((n, Money(round(m / MONTHS), currency)) for n, m in ranked[:TOP_LINES]),
    )


def as_recurring(b: CostBaseline) -> RecurringItem:
    """The baseline as the forecast sees it: one monthly outflow."""
    return RecurringItem(
        label=f"Running costs (average of {b.period_label} in QuickBooks)",
        category=Category.OTHER_OUTFLOW,
        direction=Direction.OUTFLOW,
        amount=b.monthly,
        # Due on the 1st of each month from the month after the window, i.e.
        # this month: spending is spread across a month, and the 1st is the
        # cautious end of it.
        recurrence=Recurrence(Frequency.MONTHLY, anchor=b.end + timedelta(days=1)),
    )
