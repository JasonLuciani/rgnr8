"""AR / collections tests — deterministic against a fixed anchor date.

Every case pins ``AS_OF`` explicitly; nothing reads the wall clock. Money is exact
integer minor units via the forecast engine's ``Money``.
"""

from __future__ import annotations

from datetime import date, timedelta

from rgnr8_forecast import (
    CustomerHistory,
    Invoice,
    Money,
    PaymentObservation,
)

from rgnr8_ar import (
    AgingBucket,
    ARReport,
    NudgeTone,
    ar_report,
    bucket_for,
    chase_list,
    draft_nudge,
    summarize_aging,
    typical_days_late,
)

AS_OF = date(2026, 8, 13)


def _inv(
    id: str,
    customer_id: str,
    due: date,
    minor: int,
    *,
    issue: date | None = None,
) -> Invoice:
    return Invoice(
        id=id,
        customer_id=customer_id,
        issue_date=issue or date(2026, 1, 1),
        due_date=due,
        open_amount=Money(minor),
    )


# --- aging boundaries --------------------------------------------------------
def test_bucket_boundaries_around_as_of() -> None:
    # Not yet due and due-today are both CURRENT (days overdue <= 0).
    assert bucket_for(date(2026, 8, 20), AS_OF) is AgingBucket.CURRENT
    assert bucket_for(AS_OF, AS_OF) is AgingBucket.CURRENT
    # 1 day overdue -> first overdue bucket.
    assert bucket_for(date(2026, 8, 12), AS_OF) is AgingBucket.D1_30
    # 30 days overdue is the top of D1_30; 31 tips into D31_60.
    assert bucket_for(AS_OF - _days(30), AS_OF) is AgingBucket.D1_30
    assert bucket_for(AS_OF - _days(31), AS_OF) is AgingBucket.D31_60
    # 60 days is the top of D31_60; 61 tips into D60_PLUS.
    assert bucket_for(AS_OF - _days(60), AS_OF) is AgingBucket.D31_60
    assert bucket_for(AS_OF - _days(61), AS_OF) is AgingBucket.D60_PLUS


def _days(n: int) -> timedelta:
    return timedelta(days=n)


# --- buckets sum to total AR -------------------------------------------------
def test_buckets_sum_to_total_ar() -> None:
    invoices = [
        _inv("A", "c1", date(2026, 9, 1), 10_000),  # current
        _inv("B", "c1", AS_OF - _days(10), 20_000),  # D1_30
        _inv("C", "c2", AS_OF - _days(45), 30_000),  # D31_60
        _inv("D", "c3", AS_OF - _days(120), 40_000),  # D60_PLUS
    ]
    summary = summarize_aging(invoices, AS_OF)
    assert summary.current == Money(10_000)
    assert summary.d1_30 == Money(20_000)
    assert summary.d31_60 == Money(30_000)
    assert summary.d60_plus == Money(40_000)
    # Grand total reconciles to the sum of the four buckets.
    parts = summary.as_mapping().values()
    total = Money.zero()
    for p in parts:
        total = total + p
    assert total == summary.grand_total == Money(100_000)
    assert summary.overdue_total == Money(90_000)


# --- chase ranking -----------------------------------------------------------
def test_chase_ranks_big_old_over_small_recent_and_excludes_not_due() -> None:
    big_old = _inv("BIG", "c1", AS_OF - _days(90), 500_000)
    small_recent = _inv("SMALL", "c2", AS_OF - _days(2), 5_000)
    not_yet_due = _inv("FUTURE", "c3", AS_OF + _days(10), 999_000)

    items = chase_list([small_recent, not_yet_due, big_old], AS_OF)

    ids = [it.invoice.id for it in items]
    assert "FUTURE" not in ids  # not-yet-due excluded
    assert ids == ["BIG", "SMALL"]  # big + old ranks above small + recent
    assert items[0].priority > items[1].priority


def test_risk_weighting_bumps_chronically_late_customer() -> None:
    # Two identical invoices (same amount, same days overdue), different customers.
    inv_late = _inv("LATE", "chronic", AS_OF - _days(20), 100_000)
    inv_ok = _inv("OK", "prompt", AS_OF - _days(20), 100_000)

    histories = {
        "chronic": CustomerHistory(
            customer_id="chronic",
            observations=(
                PaymentObservation(date(2026, 1, 1), date(2026, 2, 15)),  # 45d late
                PaymentObservation(date(2026, 2, 1), date(2026, 3, 20)),  # 47d late
                PaymentObservation(date(2026, 3, 1), date(2026, 4, 16)),  # 46d late
            ),
        ),
        "prompt": CustomerHistory(
            customer_id="prompt",
            observations=(
                PaymentObservation(date(2026, 1, 1), date(2026, 1, 1)),  # on time
                PaymentObservation(date(2026, 2, 1), date(2026, 2, 1)),
                PaymentObservation(date(2026, 3, 1), date(2026, 3, 1)),
            ),
        ),
    }

    items = chase_list([inv_ok, inv_late], AS_OF, histories=histories)
    assert [it.invoice.id for it in items] == ["LATE", "OK"]
    late_item = next(it for it in items if it.invoice.id == "LATE")
    ok_item = next(it for it in items if it.invoice.id == "OK")
    assert late_item.risk_score > ok_item.risk_score
    assert late_item.priority > ok_item.priority
    # The chronic payer's typical lateness is the median of ~46 days.
    assert typical_days_late(histories["chronic"]) == 46


def test_override_days_late_drives_risk() -> None:
    inv = _inv("X", "cust", AS_OF - _days(5), 1_000)
    hist = {"cust": CustomerHistory(customer_id="cust", override_days_late=30)}
    items = chase_list([inv], AS_OF, histories=hist)
    assert items[0].risk_score == 130  # neutral 100 + 30 late days


# --- nudge tone escalation ---------------------------------------------------
def test_nudge_tone_escalates_current_to_60_plus() -> None:
    current = draft_nudge(_inv("A", "c", AS_OF + _days(5), 1_000), AS_OF)
    d1_30 = draft_nudge(_inv("B", "c", AS_OF - _days(10), 1_000), AS_OF)
    d31_60 = draft_nudge(_inv("C", "c", AS_OF - _days(45), 1_000), AS_OF)
    d60 = draft_nudge(_inv("D", "c", AS_OF - _days(90), 1_000), AS_OF)

    assert current.tone is NudgeTone.FRIENDLY
    assert d1_30.tone is NudgeTone.FRIENDLY
    assert d31_60.tone is NudgeTone.FIRM
    assert d60.tone is NudgeTone.FINAL
    # Final notice reads as a final notice and references the invoice.
    assert "FINAL NOTICE" in d60.subject
    assert "D" in d60.body and "immediate payment" in d60.body


def test_nudge_is_deterministic() -> None:
    inv = _inv("Z", "c", AS_OF - _days(90), 1_000)
    assert draft_nudge(inv, AS_OF) == draft_nudge(inv, AS_OF)


# --- AR report + DSO ---------------------------------------------------------
def test_ar_report_totals_and_dso() -> None:
    invoices = [
        _inv("A", "c1", date(2026, 9, 1), 10_000),  # current
        _inv("B", "c1", AS_OF - _days(10), 20_000),  # overdue
        _inv("C", "c2", AS_OF - _days(45), 30_000),  # overdue
    ]
    # $600/day average sales -> 60_000 minor units per day.
    report = ar_report(invoices, AS_OF, avg_daily_sales=Money(60_000))
    assert isinstance(report, ARReport)
    assert report.total_ar == Money(60_000)
    assert report.overdue_total == Money(50_000)
    # DSO = total AR (60_000) / avg daily sales (60_000) = 1.0 day.
    assert report.dso == 1.0


def test_ar_report_dso_none_without_avg_daily_sales() -> None:
    invoices = [_inv("A", "c1", AS_OF - _days(10), 20_000)]
    report = ar_report(invoices, AS_OF)
    assert report.dso is None
    assert report.total_ar == Money(20_000)


def test_ar_report_dso_none_when_avg_daily_sales_zero() -> None:
    invoices = [_inv("A", "c1", AS_OF - _days(10), 20_000)]
    report = ar_report(invoices, AS_OF, avg_daily_sales=Money(0))
    assert report.dso is None
