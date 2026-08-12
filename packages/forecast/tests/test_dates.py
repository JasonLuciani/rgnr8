from datetime import date, timedelta

from rgnr8_forecast import Frequency, Recurrence, add_months, week_buckets
from rgnr8_forecast.dates import bucket_index


def test_add_months_clamps_month_end() -> None:
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 1, 31), 13) == date(2027, 2, 28)
    assert add_months(date(2026, 3, 15), -3) == date(2025, 12, 15)


def test_monthly_anchor_does_not_drift_from_month_end() -> None:
    occ = Recurrence(Frequency.MONTHLY, anchor=date(2026, 1, 31)).occurrences(
        date(2026, 2, 1), date(2026, 7, 1)
    )
    assert occ == [
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
        date(2026, 5, 31),
        date(2026, 6, 30),
    ]


def test_weekly_and_biweekly() -> None:
    start, end = date(2026, 8, 3), date(2026, 8, 3) + timedelta(days=90)
    wk = Recurrence(Frequency.WEEKLY, anchor=date(2026, 8, 7)).occurrences(start, end)
    bw = Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 8, 7)).occurrences(start, end)
    assert wk[0] == date(2026, 8, 7)
    assert all((wk[i + 1] - wk[i]).days == 7 for i in range(len(wk) - 1))
    assert all((bw[i + 1] - bw[i]).days == 14 for i in range(len(bw) - 1))


def test_count_limits_from_anchor() -> None:
    occ = Recurrence(Frequency.WEEKLY, anchor=date(2026, 8, 3), count=3).occurrences(
        date(2026, 1, 1), date(2027, 1, 1)
    )
    assert occ == [date(2026, 8, 3), date(2026, 8, 10), date(2026, 8, 17)]


def test_semimonthly() -> None:
    occ = Recurrence(
        Frequency.SEMIMONTHLY, anchor=date(2026, 8, 15), second_day=31
    ).occurrences(date(2026, 8, 1), date(2026, 10, 3))
    assert occ == [
        date(2026, 8, 15),
        date(2026, 8, 31),
        date(2026, 9, 15),
        date(2026, 9, 30),
    ]


def test_end_date_caps_occurrences() -> None:
    occ = Recurrence(
        Frequency.WEEKLY, anchor=date(2026, 8, 3), end=date(2026, 8, 20)
    ).occurrences(date(2026, 8, 1), date(2026, 12, 1))
    assert occ[-1] <= date(2026, 8, 20)


def test_week_buckets_and_index() -> None:
    buckets = week_buckets(date(2026, 8, 3), 13)
    assert len(buckets) == 13
    assert buckets[0].start == date(2026, 8, 3)
    assert buckets[0].end == date(2026, 8, 9)
    assert buckets[-1].end == date(2026, 11, 1)
    assert bucket_index(date(2026, 8, 3), date(2026, 8, 3) + timedelta(days=20), 13) == 3
    assert bucket_index(date(2026, 8, 3), date(2025, 1, 1), 13) is None
    assert bucket_index(date(2026, 8, 3), date(2026, 8, 3) + timedelta(days=200), 13) is None
