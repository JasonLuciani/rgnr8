"""Shared factories: build forecasts in known states."""

from __future__ import annotations

from datetime import date

from rgnr8_forecast import (
    CashPosition,
    Category,
    CustomerHistory,
    Direction,
    ForecastConfig,
    ForecastInputs,
    ForecastResult,
    Frequency,
    Invoice,
    Money,
    PayrollSchedule,
    Recurrence,
    RecurringItem,
    run_forecast,
)

AS_OF = date(2026, 8, 3)


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def healthy_forecast() -> ForecastResult:
    inputs = ForecastInputs(
        opening=CashPosition(as_of=AS_OF, available=usd("200000.00")),
        invoices=(Invoice("INV-1", "acme", date(2026, 8, 1), date(2026, 8, 20), usd("15000.00")),),
        customer_histories=(CustomerHistory("acme", override_days_late=6),),
        recurring=(
            RecurringItem("Rent", Category.RENT, Direction.OUTFLOW, usd("4000.00"),
                          Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 1))),
        ),
    )
    return run_forecast(inputs, ForecastConfig(minimum_cash=usd("10000.00")))


def breach_forecast() -> ForecastResult:
    inputs = ForecastInputs(
        opening=CashPosition(as_of=AS_OF, available=usd("20000.00")),
        payroll=(
            PayrollSchedule("Team", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 8, 7)),
                            usd("16000.00"), usd("4200.00")),
        ),
        invoices=(Invoice("INV-1", "acme", date(2026, 8, 1), date(2026, 8, 20), usd("15000.00")),),
        customer_histories=(CustomerHistory("acme", override_days_late=6),),
    )
    return run_forecast(inputs, ForecastConfig(minimum_cash=usd("10000.00")))
