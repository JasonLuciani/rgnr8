"""Runnable demo:  python examples/demo.py   (from the package root)

Builds a realistic agency's 13-week position and prints the base forecast, the
scenario envelope, a drill-down of one week, and the publication lifecycle.
"""

from __future__ import annotations

from datetime import date

from rgnr8_forecast import (
    Bill,
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
    PipelineOpportunity,
    Recurrence,
    RecurringItem,
    Scenario,
    run_all_scenarios,
    run_forecast,
)


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def obs(customer: str, days: list[int]) -> CustomerHistory:
    base = date(2026, 1, 1)
    from datetime import timedelta

    return CustomerHistory(
        customer_id=customer,
        observations=tuple(PaymentObservation(base, base + timedelta(days=d)) for d in days),
    )


AS_OF = date(2026, 8, 3)

inputs = ForecastInputs(
    currency="USD",
    opening=CashPosition(as_of=AS_OF, available=usd("68000.00"), restricted=usd("8000.00")),
    invoices=(
        Invoice("INV-201", "northwind", date(2026, 7, 5), date(2026, 8, 4), usd("22000.00")),
        Invoice("INV-202", "contoso", date(2026, 7, 20), date(2026, 8, 19), usd("16500.00")),
        Invoice("INV-203", "northwind", date(2026, 8, 12), date(2026, 9, 11), usd("18000.00")),
        Invoice("INV-140", "fabrikam", date(2026, 3, 1), date(2026, 3, 31), usd("9000.00")),  # very aged
    ),
    customer_histories=(
        obs("northwind", [6, 9, 7, 8, 5, 10]),
        obs("contoso", [24, 31, 28, 35]),
        CustomerHistory("fabrikam", override_days_late=20),
    ),
    payroll=(
        PayrollSchedule("Payroll", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 8, 7)),
                        net_pay=usd("21000.00"), payroll_taxes=usd("5600.00"), tax_remit_lag_days=3),
    ),
    recurring=(
        RecurringItem("Office rent", Category.RENT, Direction.OUTFLOW, usd("7200.00"),
                      Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 1))),
        RecurringItem("SaaS + tooling", Category.OTHER_OUTFLOW, Direction.OUTFLOW, usd("2400.00"),
                      Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 5))),
        RecurringItem("Retainer — Globex", Category.CUSTOMER_RECEIPT, Direction.INFLOW, usd("9000.00"),
                      Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 15))),
    ),
    debt=(
        DebtInstrument("Equipment loan", Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 18)),
                       payment=usd("3100.00"), principal_portion=usd("2500.00"),
                       interest_portion=usd("600.00")),
    ),
    bills=(
        Bill("BILL-88", "adobe", date(2026, 8, 25), usd("1800.00")),
    ),
    one_time=(
        OneTimeItem("Quarterly sales tax", Category.TAX_REMITTANCE, Direction.OUTFLOW,
                    usd("12500.00"), date(2026, 9, 20)),
        OneTimeItem("New laptop batch", Category.CAPITAL_EXPENDITURE, Direction.OUTFLOW,
                    usd("9000.00"), date(2026, 9, 2)),
    ),
    pipeline=(
        PipelineOpportunity("Initech redesign", "initech", date(2026, 9, 25), usd("45000.00"), 4000),
    ),
)

config = ForecastConfig(minimum_cash=usd("15000.00"))


def money(m: Money) -> str:
    return f"{m.to_decimal_string():>12}"


def main() -> None:
    base = run_forecast(inputs, config, Scenario.BASE)
    p = base.projection

    print("=" * 78)
    print("RGNR8 — 13-week cash forecast — Bright Agency — as of", AS_OF.isoformat())
    print("=" * 78)
    print(base.headline)
    if base.recommended_action:
        print("\nRecommended action:")
        print(" ", base.recommended_action)
    print(f"\nOverall confidence: {base.overall_confidence}/100")
    print(f"Version: {base.version.version_id}   status: {base.status.value}")
    if base.data_quality:
        print("Data quality:")
        for n in base.data_quality:
            print("  -", n)

    print("\n Wk  Start        Opening     Inflows    Outflows         Net      Closing  Conf")
    print(" " + "-" * 74)
    for w in p.weeks:
        print(
            f" {w.index:>2}  {w.start.isoformat()} "
            f"{money(w.opening)} {money(w.inflows)} {money(w.outflows)} "
            f"{money(w.net)} {money(w.closing)}  {w.confidence:>3}"
        )
    print(" " + "-" * 74)
    print(f" Cash trough: {p.trough.balance.to_decimal_string()} on {p.trough.on_date.isoformat()}"
          f"   (floor {p.effective_floor.to_decimal_string()})")

    print("\nScenario envelope:")
    for line in run_all_scenarios(inputs, config).summary_lines():
        print("  ", line)

    # Drill-down: the flows composing the trough week.
    trough_week = min(p.weeks, key=lambda w: w.closing.minor_units)
    print(f"\nDrill-down — week {trough_week.index} ({trough_week.start.isoformat()}):")
    for seq in trough_week.flow_seqs:
        f = base.flow(seq)
        assert f is not None
        sign = "+" if f.direction.value == "INFLOW" else "-"
        print(f"   {f.on_date.isoformat()}  {sign}{f.amount.to_decimal_string():>10}  "
              f"{f.category.value:<18} [{f.confidence.value}]  {f.basis}")

    # Publication lifecycle.
    published = base.verified("controller@rgnr8").published("controller@rgnr8", "2026-08-06T22:00:00Z")
    print(f"\nPublished version {published.version.version_id} "
          f"status={published.status.value} reviewer={published.version.reviewer}")
    print("Base fingerprint unchanged by publication:",
          published.version.input_fingerprint == base.version.input_fingerprint)


if __name__ == "__main__":
    main()
