"""Runnable demo:  python examples/demo.py

Builds a realistic forecast, assembles the weekly briefing, validates every
number, prints the owner text, writes an HTML briefing, and runs the Q&A.
"""

from __future__ import annotations

from datetime import date

from rgnr8_forecast import (
    CashPosition,
    Category,
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
    run_forecast,
)
from rgnr8_briefing import (
    ask,
    build_briefing,
    render_html,
    render_text,
    suggested_questions,
    validate_briefing,
)


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def obs(customer: str, days: list[int]) -> CustomerHistory:
    from datetime import timedelta

    base = date(2026, 1, 1)
    return CustomerHistory(
        customer_id=customer,
        observations=tuple(PaymentObservation(base, base + timedelta(days=d)) for d in days),
    )


AS_OF = date(2026, 8, 3)

inputs = ForecastInputs(
    opening=CashPosition(as_of=AS_OF, available=usd("58000.00"), restricted=usd("8000.00")),
    invoices=(
        Invoice("INV-201", "northwind", date(2026, 7, 5), date(2026, 8, 4), usd("22000.00")),
        Invoice("INV-202", "contoso", date(2026, 7, 20), date(2026, 8, 19), usd("16500.00")),
        Invoice("INV-203", "northwind", date(2026, 8, 12), date(2026, 9, 11), usd("18000.00")),
    ),
    customer_histories=(obs("northwind", [6, 9, 7, 8, 5, 10]), obs("contoso", [24, 31, 28, 35])),
    payroll=(
        PayrollSchedule("Payroll", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 8, 7)),
                        usd("21000.00"), usd("5600.00")),
    ),
    recurring=(
        RecurringItem("Office rent", Category.RENT, Direction.OUTFLOW, usd("7200.00"),
                      Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 1))),
        RecurringItem("Retainer — Globex", Category.CUSTOMER_RECEIPT, Direction.INFLOW, usd("9000.00"),
                      Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 15))),
    ),
    debt=(
        DebtInstrument("Equipment loan", Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 18)),
                       usd("3100.00"), usd("2500.00"), usd("600.00")),
    ),
    one_time=(
        OneTimeItem("Quarterly sales tax", Category.TAX_REMITTANCE, Direction.OUTFLOW,
                    usd("12500.00"), date(2026, 9, 20)),
    ),
)


def main() -> None:
    forecast = run_forecast(inputs, ForecastConfig(minimum_cash=usd("15000.00")))
    briefing = build_briefing(forecast)

    violations = validate_briefing(briefing, forecast)
    print(f"Unsupported-number validator: {len(violations)} violation(s)\n")
    assert not violations, "briefing has unbacked numbers!"

    print(render_text(briefing))

    out = "briefing.html"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(render_html(briefing))
    print(f"\nWrote {out}")

    print("\n--- Ask your CFO ---")
    for q in suggested_questions()[:4]:
        a = ask(forecast, q)
        print(f"Q: {q}\nA: {a.answer_text}\n")


if __name__ == "__main__":
    main()
