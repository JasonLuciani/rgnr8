"""Render the owner Today dashboard to today.html.  python examples/today_demo.py"""

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
    run_forecast,
)
from rgnr8_briefing import render_today_html


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def main() -> None:
    AS_OF = date(2026, 8, 3)
    inputs = ForecastInputs(
        opening=CashPosition(as_of=AS_OF, available=usd("68000.00"), restricted=usd("8000.00")),
        invoices=(
            Invoice("INV-201", "northwind", date(2026, 7, 5), date(2026, 8, 20), usd("22000.00")),
            Invoice("INV-202", "contoso", date(2026, 7, 20), date(2026, 8, 31), usd("16500.00")),
        ),
        customer_histories=(CustomerHistory("northwind", override_days_late=8),),
        payroll=(
            PayrollSchedule("Payroll", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 8, 7)),
                            usd("21000.00"), usd("5600.00")),
        ),
        recurring=(
            RecurringItem("Office rent", Category.RENT, Direction.OUTFLOW, usd("7200.00"),
                          Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 1))),
        ),
    )
    forecast = run_forecast(inputs, ForecastConfig(minimum_cash=usd("15000.00")))
    with open("today.html", "w", encoding="utf-8") as fh:
        fh.write(render_today_html(forecast, "Bright Agency"))
    print("wrote today.html — open it in a browser (click the questions, hover the chart)")


if __name__ == "__main__":
    main()
