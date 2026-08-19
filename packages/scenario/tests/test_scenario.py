"""Scenario engine tests.

Fully deterministic: fixed anchor date, integer money, no wall clock or random.
Fixtures are hand-built so the trough, breach, and per-week deltas are known in
advance and every assertion is exact.
"""

from __future__ import annotations

from datetime import date
from fractions import Fraction

from rgnr8_forecast import (
    CashPosition,
    Category,
    Confidence,
    Direction,
    ForecastConfig,
    ForecastInputs,
    ForecastResult,
    Frequency,
    Invoice,
    Money,
    OneTimeItem,
    PayrollSchedule,
    Recurrence,
    RecurringItem,
    run_forecast,
)
from rgnr8_scenario import (
    ScalePayroll,
    ScaleRecurring,
    Scenario,
    ScenarioDiff,
    SetMinimumCash,
    apply,
    compare,
    config_override,
    customer_pays_late,
    hire_employee,
    one_time_expense,
    run_scenario,
    take_loan,
)

AS_OF = date(2026, 1, 5)  # Monday; week 1 begins here


def usd(decimal: str) -> Money:
    return Money.from_decimal(decimal, "USD")


def _outflow(label: str, amount: Money, on: date) -> OneTimeItem:
    return OneTimeItem(
        label=label,
        category=Category.OTHER_OUTFLOW,
        direction=Direction.OUTFLOW,
        amount=amount,
        on_date=on,
        confidence=Confidence.PLANNED,
    )


def base_inputs() -> ForecastInputs:
    """A hand-tuned bundle with a known trough of $2,000 on 2026-01-12.

    Timeline (baseline, C1 has no history so pays the config default of 5d late):
      Jan 5  open           10,000
      Jan 12 rent  -8,000     2,000  <- trough
      Jan 13 receipt +7,000   9,000  (invoice due Jan 8, paid +5d)
      Jan 26 bill  -6,000     3,000
    """
    return ForecastInputs(
        opening=CashPosition(as_of=AS_OF, available=usd("10000.00")),
        currency="USD",
        invoices=(
            Invoice(
                id="INV-1",
                customer_id="C1",
                issue_date=date(2026, 1, 1),
                due_date=date(2026, 1, 8),
                open_amount=usd("7000.00"),
            ),
        ),
        one_time=(
            _outflow("rent", usd("8000.00"), date(2026, 1, 12)),
            _outflow("bill2", usd("6000.00"), date(2026, 1, 26)),
        ),
    )


def base_config() -> ForecastConfig:
    return ForecastConfig(currency="USD")


# --- acceptance: late payment moves the trough later and lower ---------------
def test_customer_pays_late_moves_trough_later_and_lower() -> None:
    inputs = base_inputs()
    config = base_config()
    scenario = customer_pays_late("C1", 30)

    base = run_forecast(inputs, config)
    scen_result, diff = run_scenario(inputs, config, scenario)

    # Baseline trough is $2,000 on Jan 12 (before the receipt lands).
    assert base.projection.trough.balance == usd("2000.00")
    assert base.projection.trough.on_date == date(2026, 1, 12)

    # Delaying the receipt to Feb 7 exposes a deeper, later trough on Jan 26.
    assert scen_result.projection.trough.on_date > base.projection.trough.on_date
    assert scen_result.projection.trough.balance < base.projection.trough.balance
    assert scen_result.projection.trough.balance == usd("-4000.00")
    assert scen_result.projection.trough.on_date == date(2026, 1, 26)


# --- acceptance: the diff reconciles to a re-run -----------------------------
def test_diff_reconciles_to_rerun() -> None:
    inputs = base_inputs()
    config = base_config()
    scenario = customer_pays_late("C1", 30)

    base = run_forecast(inputs, config)
    scen_result, diff = run_scenario(inputs, config, scenario)

    assert diff.trough_delta == (
        scen_result.projection.trough.balance - base.projection.trough.balance
    )
    # And it equals compare() run directly on the two results.
    assert compare(base, scen_result) == diff


# --- acceptance: one-time expense lowers cushion by exactly its amount --------
def test_one_time_expense_lowers_cushion_by_its_amount() -> None:
    inputs = base_inputs()
    config = base_config()
    expense = usd("1000.00")
    # Dated on/before the trough (Jan 12) so the trough absorbs the outflow.
    scenario = one_time_expense("laptop", expense, date(2026, 1, 5))

    _, diff = run_scenario(inputs, config, scenario)

    assert diff.cushion_delta == -expense
    assert diff.trough_delta == -expense


# --- acceptance: a loan raises near-term cash, then repayments lower it -------
def test_take_loan_raises_then_lowers() -> None:
    inputs = base_inputs()
    config = base_config()
    scenario = take_loan(
        amount=usd("20000.00"),
        on=date(2026, 1, 6),
        monthly_repayment=usd("2000.00"),
        first_repayment=date(2026, 2, 6),
    )

    _, diff = run_scenario(inputs, config, scenario)

    # Near-term cash is raised (the $20k draw dominates the first repayment).
    assert diff.weekly_closing_deltas[0].is_positive
    # As monthly repayments accumulate, the gain steadily erodes.
    assert diff.weekly_closing_deltas[-1] < diff.weekly_closing_deltas[0]
    # Each repayment lowers the net gain by exactly $2,000; the draw still nets
    # positive across the whole horizon, so the trough is lifted.
    assert diff.weekly_closing_deltas[0] == usd("18000.00")
    assert diff.weekly_closing_deltas[-1] == usd("14000.00")
    assert diff.trough_delta.is_positive


# --- set-minimum-cash flows through config_override, not the flows ------------
def test_set_minimum_cash_raises_floor_and_creates_breach() -> None:
    inputs = base_inputs()
    config = base_config()
    scenario = Scenario(
        name="raise floor", adjustments=(SetMinimumCash(usd("5000.00")),)
    )

    # apply() must not touch the flows for a floor change.
    assert apply(inputs, scenario) == inputs
    assert config_override(scenario, config).minimum_cash == usd("5000.00")

    base = run_forecast(inputs, config)
    scen_result, diff = run_scenario(inputs, config, scenario)

    assert not base.projection.breach.breached
    assert scen_result.projection.breach.breached
    assert diff.breach_week_before is None
    assert diff.breach_week_after is not None
    # Trough ($2,000) unchanged in balance; only the floor moved.
    assert diff.trough_delta == usd("0.00")
    assert diff.cushion_delta == -usd("5000.00")


# --- unit: scaling multiplies amounts via Money.scale_by ---------------------
def test_scale_payroll_and_recurring() -> None:
    inputs = ForecastInputs(
        opening=CashPosition(as_of=AS_OF, available=usd("50000.00")),
        currency="USD",
        payroll=(
            PayrollSchedule(
                label="staff",
                recurrence=Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 1, 9)),
                net_pay=usd("8000.00"),
                payroll_taxes=usd("2000.00"),
            ),
        ),
        recurring=(
            RecurringItem(
                label="Office rent",
                category=Category.RENT,
                direction=Direction.OUTFLOW,
                amount=usd("3000.00"),
                recurrence=Recurrence(Frequency.MONTHLY, anchor=date(2026, 1, 15)),
            ),
        ),
    )
    scenario = Scenario(
        name="cut costs",
        adjustments=(
            ScalePayroll(Fraction(1, 2)),
            ScaleRecurring("nonexistent label", Fraction(1, 9)),  # matches nothing
            ScaleRecurring("Office", Fraction(1, 3)),  # substring of "Office rent"
        ),
    )
    out = apply(inputs, scenario)

    assert out.payroll[0].net_pay == usd("4000.00")
    assert out.payroll[0].payroll_taxes == usd("1000.00")
    # Only the substring "Office" matched; rent scaled by 1/3 (banker's rounding).
    assert out.recurring[0].amount == usd("1000.00")


# --- acceptance: templates produce inputs run_forecast accepts ---------------
def test_templates_produce_runnable_inputs() -> None:
    inputs = base_inputs()
    config = base_config()
    scenarios = [
        hire_employee(usd("8000.00"), date(2026, 1, 15)),
        customer_pays_late("C1", 20),
        take_loan(
            amount=usd("15000.00"),
            on=date(2026, 1, 10),
            monthly_repayment=usd("1500.00"),
            first_repayment=date(2026, 2, 10),
        ),
        one_time_expense("tax", usd("4000.00"), date(2026, 1, 20)),
    ]
    for scenario in scenarios:
        applied = apply(inputs, scenario)
        result = run_forecast(applied, config_override(scenario, config))
        assert isinstance(result, ForecastResult)
        assert len(result.projection.weeks) == config.horizon_weeks

        scen_result, diff = run_scenario(inputs, config, scenario)
        assert isinstance(scen_result, ForecastResult)
        assert isinstance(diff, ScenarioDiff)
        assert len(diff.weekly_closing_deltas) == config.horizon_weeks


# --- hire adds a recurring outflow that reaches the flows ---------------------
def test_hire_employee_adds_recurring_outflow() -> None:
    inputs = base_inputs()
    config = base_config()
    scenario = hire_employee(usd("8000.00"), date(2026, 1, 15))

    base = run_forecast(inputs, config)
    scen_result, diff = run_scenario(inputs, config, scenario)

    # A new monthly $8k outflow can only lower closing balances vs baseline.
    assert scen_result.projection.trough.balance < base.projection.trough.balance
    assert all(d.minor_units <= 0 for d in diff.weekly_closing_deltas)
