from __future__ import annotations

from datetime import date

from rgnr8_forecast import (
    CashPosition,
    Category,
    Direction,
    ForecastConfig,
    ForecastInputs,
    Frequency,
    Invoice,
    Money,
    PayrollSchedule,
    Recurrence,
    RecurringItem,
)
from rgnr8_ops import BetaTenant


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def _steady_inputs() -> ForecastInputs:
    # comfortable cash, modest outflows → STABLE
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=usd("120000.00")),
        invoices=(Invoice("INV-1", "acme", date(2026, 8, 20), date(2026, 9, 15), usd("18000.00")),),
        recurring=(
            RecurringItem("Rent", Category.RENT, Direction.OUTFLOW, usd("4000.00"),
                          Recurrence(Frequency.MONTHLY, anchor=date(2026, 9, 1))),
        ),
    )


def _at_risk_inputs() -> ForecastInputs:
    # thin cash, heavy biweekly payroll → AT_RISK
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=usd("40000.00")),
        payroll=(
            PayrollSchedule("Payroll", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 9, 3)),
                            usd("22000.00"), usd("5600.00")),
        ),
        recurring=(
            RecurringItem("Rent", Category.RENT, Direction.OUTFLOW, usd("6000.00"),
                          Recurrence(Frequency.MONTHLY, anchor=date(2026, 9, 1))),
        ),
    )


def steady_tenant() -> BetaTenant:
    return BetaTenant("acme", "Acme Co", "owner@acme.com", _steady_inputs(),
                      ForecastConfig(minimum_cash=usd("10000.00")))


def at_risk_tenant() -> BetaTenant:
    return BetaTenant("bright", "Bright Agency", "owner@bright.com", _at_risk_inputs(),
                      ForecastConfig(minimum_cash=usd("15000.00")))
