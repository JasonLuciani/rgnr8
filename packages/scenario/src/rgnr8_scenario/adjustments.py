"""What-if adjustments: a small sum type of edits to ``ForecastInputs``.

Each adjustment is a frozen dataclass describing one owner-facing change ("this
customer pays 30 days late", "hire at $8k/mo", "take a $50k loan"). A ``Scenario``
is a named, ordered tuple of them. ``apply`` folds a scenario over the inputs and
returns a *new* ``ForecastInputs`` — it is pure (no mutation, no wall clock, no
randomness), so re-running the forecast on the result is fully deterministic.

Min-cash is special: it changes the *floor* (a ``ForecastConfig`` concern), not
the flows, so ``apply`` leaves the inputs' cash flows untouched for it and
``config_override`` folds any ``SetMinimumCash`` onto the config instead.
"""

from __future__ import annotations

import dataclasses
import statistics
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from rgnr8_forecast import (
    Category,
    Confidence,
    CustomerHistory,
    Direction,
    ForecastConfig,
    ForecastInputs,
    Money,
    OneTimeItem,
    PayrollSchedule,
    RecurringItem,
)


# --- adjustment variants -----------------------------------------------------
@dataclass(frozen=True, slots=True)
class DelayCustomerPayment:
    """Shift a customer's expected payment timing ``days`` later than baseline.

    Implemented as a ``CustomerHistory`` override: the customer's current
    effective days-late (their existing override, else the median of their
    observed history, else 0) plus ``days`` becomes an explicit override, which
    the timing predictor honors over learned behavior.
    """

    customer_id: str
    days: int


@dataclass(frozen=True, slots=True)
class OneTimeFlow:
    """A single dated cash movement (a purchase, a distribution, a capital inflow)."""

    label: str
    amount: Money
    on: date
    inflow: bool


@dataclass(frozen=True, slots=True)
class ScaleRecurring:
    """Multiply the amount of every recurring item whose label contains
    ``label_match`` by an exact rational ``factor`` (0 removes it economically)."""

    label_match: str
    factor: Fraction


@dataclass(frozen=True, slots=True)
class AddRecurring:
    """Introduce a new recurring inflow/outflow (a hire, a new retainer, rent)."""

    item: RecurringItem


@dataclass(frozen=True, slots=True)
class ScalePayroll:
    """Multiply every payroll schedule's net pay and taxes by ``factor``."""

    factor: Fraction


@dataclass(frozen=True, slots=True)
class SetMinimumCash:
    """Change the minimum-cash floor. Affects the config, not the flows."""

    amount: Money


Adjustment = (
    DelayCustomerPayment
    | OneTimeFlow
    | ScaleRecurring
    | AddRecurring
    | ScalePayroll
    | SetMinimumCash
)


@dataclass(frozen=True, slots=True)
class Scenario:
    """A named, ordered set of adjustments applied together."""

    name: str
    adjustments: tuple[Adjustment, ...] = ()


# --- application -------------------------------------------------------------
def _scale_money(amount: Money, factor: Fraction) -> Money:
    return amount.scale_by(factor.numerator, factor.denominator)


def _effective_days_late(history: CustomerHistory | None) -> int:
    """The customer's current baseline days-late, ignoring config defaults.

    An explicit override wins; else the median of observed lateness; else 0.
    """
    if history is None:
        return 0
    if history.override_days_late is not None:
        return history.override_days_late
    if history.observations:
        return int(round(statistics.median(o.days_late for o in history.observations)))
    return 0


def _delay_customer(
    histories: list[CustomerHistory], adj: DelayCustomerPayment
) -> list[CustomerHistory]:
    existing: CustomerHistory | None = None
    for h in histories:
        if h.customer_id == adj.customer_id:
            existing = h
            break
    new_override = _effective_days_late(existing) + adj.days
    updated = CustomerHistory(
        customer_id=adj.customer_id,
        observations=existing.observations if existing is not None else (),
        override_days_late=new_override,
    )
    out = [h for h in histories if h.customer_id != adj.customer_id]
    out.append(updated)
    return out


def apply(inputs: ForecastInputs, scenario: Scenario) -> ForecastInputs:
    """Return a new ``ForecastInputs`` with ``scenario``'s adjustments applied.

    Pure: the input object is never mutated. ``SetMinimumCash`` is a floor change
    handled by :func:`config_override`, so it does not touch the flows here.
    """
    histories = list(inputs.customer_histories)
    recurring = list(inputs.recurring)
    payroll = list(inputs.payroll)
    one_time = list(inputs.one_time)

    for adj in scenario.adjustments:
        if isinstance(adj, DelayCustomerPayment):
            histories = _delay_customer(histories, adj)
        elif isinstance(adj, OneTimeFlow):
            direction = Direction.INFLOW if adj.inflow else Direction.OUTFLOW
            category = (
                Category.OTHER_INFLOW if adj.inflow else Category.OTHER_OUTFLOW
            )
            one_time.append(
                OneTimeItem(
                    label=adj.label,
                    category=category,
                    direction=direction,
                    amount=adj.amount,
                    on_date=adj.on,
                    confidence=Confidence.PLANNED,
                )
            )
        elif isinstance(adj, ScaleRecurring):
            recurring = [
                dataclasses.replace(item, amount=_scale_money(item.amount, adj.factor))
                if adj.label_match in item.label
                else item
                for item in recurring
            ]
        elif isinstance(adj, AddRecurring):
            recurring.append(adj.item)
        elif isinstance(adj, ScalePayroll):
            payroll = [
                dataclasses.replace(
                    pr,
                    net_pay=_scale_money(pr.net_pay, adj.factor),
                    payroll_taxes=_scale_money(pr.payroll_taxes, adj.factor),
                )
                for pr in payroll
            ]
        else:  # SetMinimumCash — a config concern, not a flow
            _assert_min_cash(adj)

    return dataclasses.replace(
        inputs,
        customer_histories=tuple(histories),
        recurring=tuple(recurring),
        payroll=tuple(payroll),
        one_time=tuple(one_time),
    )


def _assert_min_cash(adj: SetMinimumCash) -> None:
    """Type-narrowing no-op so the ``apply`` dispatch is exhaustive for mypy."""
    _ = adj.amount


def config_override(scenario: Scenario, config: ForecastConfig) -> ForecastConfig:
    """Fold any ``SetMinimumCash`` adjustments onto ``config`` (last one wins).

    Returns ``config`` unchanged when the scenario does not touch the floor.
    """
    result = config
    for adj in scenario.adjustments:
        if isinstance(adj, SetMinimumCash):
            result = dataclasses.replace(result, minimum_cash=adj.amount)
    return result


__all__ = [
    "DelayCustomerPayment",
    "OneTimeFlow",
    "ScaleRecurring",
    "AddRecurring",
    "ScalePayroll",
    "SetMinimumCash",
    "Adjustment",
    "Scenario",
    "apply",
    "config_override",
]
