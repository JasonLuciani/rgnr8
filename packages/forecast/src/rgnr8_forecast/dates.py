"""Deterministic calendar helpers: week bucketing and recurrence expansion.

No wall clock is read anywhere — every date is an explicit input, so the same
inputs always produce the same schedule. Named ``dates`` to avoid shadowing the
standard-library ``calendar`` module.
"""

from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum


class Frequency(str, Enum):
    WEEKLY = "WEEKLY"
    BIWEEKLY = "BIWEEKLY"
    SEMIMONTHLY = "SEMIMONTHLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    ANNUAL = "ANNUAL"


def add_months(d: date, n: int) -> date:
    """Add ``n`` months, clamping to the last valid day (Jan 31 + 1mo -> Feb 28/29)."""
    m0 = d.month - 1 + n
    year = d.year + m0 // 12
    month = m0 % 12 + 1
    last = _cal.monthrange(year, month)[1]
    return date(year, month, min(d.day, last))


@dataclass(frozen=True, slots=True)
class Recurrence:
    """A recurring schedule.

    - WEEKLY/BIWEEKLY: stepped in weeks from ``anchor`` (BIWEEKLY == interval 2).
    - MONTHLY/QUARTERLY/ANNUAL: stepped in months from ``anchor`` (day clamped).
    - SEMIMONTHLY: two days each month, ``anchor.day`` and ``second_day``
      (default 15 and end-of-month if not given).
    """

    frequency: Frequency
    anchor: date
    interval: int = 1
    second_day: int | None = None  # for SEMIMONTHLY
    end: date | None = None
    count: int | None = None

    def occurrences(self, start: date, end: date) -> list[date]:
        """All occurrence dates within the inclusive window [start, end].

        Occurrence ``k`` is computed directly from the anchor (``anchor`` stepped
        k times), so a month-end anchor never drifts: MONTHLY from Jan 31 yields
        Feb 28, Mar 31, Apr 30, ... rather than collapsing to the 28th. ``count``
        caps the number of occurrences from the anchor forward (k in [0, count)).
        """
        hard_end = min(end, self.end) if self.end else end
        if hard_end < start:
            return []

        if self.frequency is Frequency.SEMIMONTHLY:
            return self._semimonthly(start, hard_end)

        occ = self._occ
        k = 0
        if self.count is None:
            # backfill to include occurrences before the anchor, down to start
            while occ(k - 1) >= start:
                k -= 1
        while occ(k) < start:
            k += 1

        out: list[date] = []
        while occ(k) <= hard_end:
            if self.count is not None and k >= self.count:
                break
            if self.count is None or k >= 0:
                out.append(occ(k))
            k += 1
        return out

    def _occ(self, k: int) -> date:
        """The k-th occurrence measured from the anchor (k may be negative)."""
        if self.frequency in (Frequency.WEEKLY, Frequency.BIWEEKLY):
            step = 2 if self.frequency is Frequency.BIWEEKLY else self.interval
            return self.anchor + timedelta(weeks=step * k)
        months = {
            Frequency.MONTHLY: 1,
            Frequency.QUARTERLY: 3,
            Frequency.ANNUAL: 12,
        }[self.frequency] * self.interval
        return add_months(self.anchor, months * k)

    def _semimonthly(self, start: date, hard_end: date) -> list[date]:
        d1 = self.anchor.day
        d2 = self.second_day if self.second_day is not None else 31
        out: list[date] = []
        cursor = date(start.year, start.month, 1)
        while cursor <= hard_end:
            last = _cal.monthrange(cursor.year, cursor.month)[1]
            for day in sorted({min(d1, last), min(d2, last)}):
                occ = date(cursor.year, cursor.month, day)
                if start <= occ <= hard_end:
                    out.append(occ)
            cursor = add_months(cursor, 1)
        return sorted(out)


@dataclass(frozen=True, slots=True)
class WeekBucket:
    index: int  # 1-based
    start: date
    end: date  # inclusive

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


def week_buckets(as_of: date, weeks: int) -> list[WeekBucket]:
    """``weeks`` consecutive 7-day buckets, week 1 beginning on ``as_of``."""
    buckets: list[WeekBucket] = []
    for i in range(weeks):
        s = as_of + timedelta(days=7 * i)
        buckets.append(WeekBucket(index=i + 1, start=s, end=s + timedelta(days=6)))
    return buckets


def bucket_index(as_of: date, d: date, weeks: int) -> int | None:
    """1-based week index for a date, or None if outside the horizon."""
    if d < as_of:
        return None
    delta = (d - as_of).days
    idx = delta // 7
    return idx + 1 if idx < weeks else None
