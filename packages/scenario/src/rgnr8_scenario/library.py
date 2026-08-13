"""Template builders for the common what-if questions owners actually ask.

Each returns a ready-to-apply :class:`Scenario`; the caller runs it through
:func:`rgnr8_scenario.runner.run_scenario`. Templates only assemble adjustments —
they read no clock and hold no state, so a given call always yields the same
scenario.
"""

from __future__ import annotations

from datetime import date

from rgnr8_forecast import (
    Category,
    Direction,
    Frequency,
    Money,
    Recurrence,
    RecurringItem,
)

from .adjustments import (
    AddRecurring,
    DelayCustomerPayment,
    OneTimeFlow,
    Scenario,
)


def hire_employee(monthly_cost: Money, start: date) -> Scenario:
    """Model a recurring monthly payroll outflow beginning on ``start``."""
    item = RecurringItem(
        label=f"New hire ({monthly_cost.to_decimal_string()}/mo)",
        category=Category.PAYROLL_NET,
        direction=Direction.OUTFLOW,
        amount=monthly_cost,
        recurrence=Recurrence(frequency=Frequency.MONTHLY, anchor=start),
    )
    return Scenario(name="Hire an employee", adjustments=(AddRecurring(item),))


def customer_pays_late(customer_id: str, days: int) -> Scenario:
    """Model a customer paying ``days`` later than their baseline timing."""
    return Scenario(
        name=f"Customer {customer_id} pays {days}d late",
        adjustments=(DelayCustomerPayment(customer_id=customer_id, days=days),),
    )


def take_loan(
    amount: Money,
    on: date,
    monthly_repayment: Money,
    first_repayment: date,
) -> Scenario:
    """Model a loan: a lump inflow on ``on`` and monthly repayment outflows."""
    disbursement = OneTimeFlow(
        label=f"Loan draw ({amount.to_decimal_string()})",
        amount=amount,
        on=on,
        inflow=True,
    )
    repayment = AddRecurring(
        RecurringItem(
            label=f"Loan repayment ({monthly_repayment.to_decimal_string()}/mo)",
            category=Category.DEBT_SERVICE,
            direction=Direction.OUTFLOW,
            amount=monthly_repayment,
            recurrence=Recurrence(frequency=Frequency.MONTHLY, anchor=first_repayment),
        )
    )
    return Scenario(name="Take a loan", adjustments=(disbursement, repayment))


def one_time_expense(label: str, amount: Money, on: date) -> Scenario:
    """Model a single dated cash outflow (a purchase, a tax payment)."""
    return Scenario(
        name=f"One-time expense: {label}",
        adjustments=(OneTimeFlow(label=label, amount=amount, on=on, inflow=False),),
    )


__all__ = [
    "hire_employee",
    "customer_pays_late",
    "take_loan",
    "one_time_expense",
]
