"""Shared test factories."""

from __future__ import annotations

from datetime import date, timedelta

from rgnr8_forecast import (
    Bill,
    CashPosition,
    CustomerHistory,
    DebtInstrument,
    Direction,
    ForecastConfig,
    ForecastInputs,
    Frequency,
    Invoice,
    Money,
    OneTimeItem,
    PaymentObservation,
    PayrollSchedule,
    Recurrence,
    RecurringItem,
    Category,
)

AS_OF = date(2026, 8, 3)


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def history(customer: str, days_late: list[int], base: date = date(2026, 1, 1)) -> CustomerHistory:
    obs = tuple(PaymentObservation(base, base + timedelta(days=d)) for d in days_late)
    return CustomerHistory(customer_id=customer, observations=obs)


def simple_inputs(
    *,
    available: str = "50000.00",
    restricted: str = "0.00",
    minimum_cash: str = "10000.00",
    verified: bool = True,
) -> tuple[ForecastInputs, ForecastConfig]:
    opening = CashPosition(
        as_of=AS_OF,
        available=usd(available),
        restricted=usd(restricted),
        verified=verified,
    )
    inputs = ForecastInputs(opening=opening)
    config = ForecastConfig(minimum_cash=usd(minimum_cash))
    return inputs, config
