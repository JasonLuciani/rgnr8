from __future__ import annotations

from datetime import date

from rgnr8_forecast import (
    CashPosition,
    Category,
    CustomerHistory,
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
from rgnr8_web import WebApp


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def _inputs(available: str) -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 3), available=usd(available)),
        invoices=(Invoice("INV-1", "acme", date(2026, 8, 1), date(2026, 8, 20), usd("15000.00")),),
        customer_histories=(CustomerHistory("acme", override_days_late=6),),
        payroll=(
            PayrollSchedule("Payroll", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 8, 7)),
                            usd("16000.00"), usd("4200.00")),
        ),
        recurring=(
            RecurringItem("Rent", Category.RENT, Direction.OUTFLOW, usd("4000.00"),
                          Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 1))),
        ),
    )


def app_with_two_tenants() -> WebApp:
    app = WebApp()
    app.add_tenant("bright", "Bright Agency", _inputs("80000.00"), ForecastConfig(minimum_cash=usd("10000.00")), token="tok-bright")
    app.add_tenant("acme", "Acme Co", _inputs("20000.00"), ForecastConfig(minimum_cash=usd("10000.00")), token="tok-acme")
    return app
