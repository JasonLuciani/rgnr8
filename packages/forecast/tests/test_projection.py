from datetime import date, timedelta

from rgnr8_forecast import (
    CashFlow,
    Category,
    Confidence,
    Direction,
    ForecastConfig,
    Money,
    compute_projection,
)

AS_OF = date(2026, 8, 3)


def flow(seq: int, offset: int, direction: Direction, amount: str,
         conf: Confidence = Confidence.RECORDED) -> CashFlow:
    return CashFlow(
        seq=seq,
        on_date=AS_OF + timedelta(days=offset),
        direction=direction,
        amount=Money.from_decimal(amount),
        category=Category.OTHER_OUTFLOW if direction is Direction.OUTFLOW else Category.OTHER_INFLOW,
        confidence=conf,
        basis="test",
    )


def cfg(min_cash: str = "0.00") -> ForecastConfig:
    return ForecastConfig(minimum_cash=Money.from_decimal(min_cash))


def test_trough_is_the_lowest_daily_balance() -> None:
    flows = [
        flow(1, 10, Direction.OUTFLOW, "8000.00"),
        flow(2, 40, Direction.INFLOW, "5000.00"),
    ]
    p = compute_projection(Money.from_decimal("10000.00"), Money(0), flows, cfg(), AS_OF)
    assert p.trough.balance == Money.from_decimal("2000.00")
    assert p.trough.on_date == AS_OF + timedelta(days=10)
    assert p.ending_balance == Money.from_decimal("7000.00")
    assert not p.breach.breached


def test_totals_and_weekly_chaining() -> None:
    flows = [
        flow(1, 2, Direction.INFLOW, "1000.00"),
        flow(2, 9, Direction.OUTFLOW, "400.00"),
    ]
    p = compute_projection(Money.from_decimal("5000.00"), Money(0), flows, cfg(), AS_OF)
    assert p.total_inflows == Money.from_decimal("1000.00")
    assert p.total_outflows == Money.from_decimal("400.00")
    # week 1 opening is the starting balance; each week opens where the last closed
    assert p.weeks[0].opening == Money.from_decimal("5000.00")
    assert p.weeks[0].closing == Money.from_decimal("6000.00")
    assert p.weeks[1].opening == Money.from_decimal("6000.00")
    assert p.weeks[1].closing == Money.from_decimal("5600.00")
    assert p.ending_balance == Money.from_decimal("5600.00")


def test_minimum_cash_breach_and_recovery() -> None:
    flows = [
        flow(1, 5, Direction.OUTFLOW, "12000.00"),
        flow(2, 20, Direction.INFLOW, "20000.00"),
    ]
    p = compute_projection(Money.from_decimal("10000.00"), Money(0), flows, cfg("0.00"), AS_OF)
    assert p.breach.breached
    assert p.breach.first_date == AS_OF + timedelta(days=5)
    assert p.breach.weeks_until == 1
    assert p.breach.worst_shortfall == Money.from_decimal("2000.00")
    assert p.breach.recovery_date == AS_OF + timedelta(days=20)


def test_restricted_cash_raises_the_effective_floor() -> None:
    flows = [flow(1, 3, Direction.OUTFLOW, "6000.00")]
    # available 20000, restricted 5000, min_cash 10000 -> effective floor 15000
    p = compute_projection(
        Money.from_decimal("20000.00"), Money.from_decimal("5000.00"), flows, cfg("10000.00"), AS_OF
    )
    assert p.effective_floor == Money.from_decimal("15000.00")
    assert p.breach.breached  # 20000 - 6000 = 14000 < 15000
    assert p.breach.first_date == AS_OF + timedelta(days=3)


def test_no_flows_holds_opening_and_full_confidence() -> None:
    p = compute_projection(Money.from_decimal("8000.00"), Money(0), [], cfg(), AS_OF)
    assert p.ending_balance == Money.from_decimal("8000.00")
    assert p.trough.balance == Money.from_decimal("8000.00")
    assert all(w.confidence == 100 for w in p.weeks)


def test_weekly_confidence_is_amount_weighted() -> None:
    # one recorded (100) and one scenario (30) flow of equal size in week 1 -> ~65
    flows = [
        flow(1, 1, Direction.OUTFLOW, "1000.00", Confidence.RECORDED),
        flow(2, 2, Direction.INFLOW, "1000.00", Confidence.SCENARIO),
    ]
    p = compute_projection(Money.from_decimal("10000.00"), Money(0), flows, cfg(), AS_OF)
    assert p.weeks[0].confidence == 65
