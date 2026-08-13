"""RGNR8 scenario / what-if planning — the layer that lets an owner *change* the
forecast instead of just reading it.

A passive 13-week forecast shows the future; this package models a decision
against it — a hire, a late-paying customer, a loan, a one-off expense, a higher
cash floor — and shows the delta versus baseline. It sits directly on
``rgnr8-forecast`` and mirrors the house conventions: frozen dataclasses, a
Protocol seam onto the engine (``ForecastRunner``), integer ``Money`` throughout,
and strict determinism — no wall clock, no randomness, so a scenario re-runs
identically every time.

Shape:
- ``adjustments`` — an ``Adjustment`` sum type (delay a customer's payment, add a
  one-time flow, scale/add a recurring item or payroll, set the min-cash floor)
  and ``Scenario`` (a named tuple of them). ``apply(inputs, scenario)`` returns a
  new ``ForecastInputs``; ``config_override`` folds any floor change onto config.
- ``compare`` — ``ScenarioDiff`` (trough Δ, breach-week before/after, cushion Δ,
  per-week closing Δ) and ``compare(base, scenario)``.
- ``library`` — template builders: ``hire_employee``, ``customer_pays_late``,
  ``take_loan``, ``one_time_expense``.
- ``runner`` — ``run_scenario`` applies, re-runs baseline + scenario, and diffs.

Non-goals (seam only): persistence and any UI live in the surfaces that consume
this engine.
"""

from __future__ import annotations

from .adjustments import (
    AddRecurring,
    Adjustment,
    DelayCustomerPayment,
    OneTimeFlow,
    ScalePayroll,
    ScaleRecurring,
    Scenario,
    SetMinimumCash,
    apply,
    config_override,
)
from .compare import ScenarioDiff, compare
from .library import (
    customer_pays_late,
    hire_employee,
    one_time_expense,
    take_loan,
)
from .runner import ForecastRunner, run_scenario

__version__ = "0.1.0"

__all__ = [
    # adjustments
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
    # compare
    "ScenarioDiff",
    "compare",
    # library
    "hire_employee",
    "customer_pays_late",
    "take_loan",
    "one_time_expense",
    # runner
    "ForecastRunner",
    "run_scenario",
]
