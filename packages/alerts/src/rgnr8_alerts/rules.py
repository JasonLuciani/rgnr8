"""Alert rules — pure, frozen predicates over a `ForecastResult`.

Each rule is a small frozen dataclass carrying its own threshold and one method,
``evaluate(forecast) -> Alert | None``: it inspects the forecast's projection and
either returns a fully-formed `Alert` (with a *stable* dedupe key) or ``None`` when
its condition does not hold. Rules are deterministic and side-effect free — the
engine stamps the alert's ``at`` from an injected clock, so nothing here reads a
wall clock or a global.

The four rule kinds map to the spec's alert vocabulary:

* `FloorBreachWithinWeeks` — the minimum-cash floor is projected to break within N
  weeks (AT_RISK; the headline "a breach forming Tuesday must reach the owner now").
* `BalanceBelow` — the projected trough dips under a caller-set balance threshold
  (WATCH; "warn me if cash ever drops below X").
* `LargeOutflow` — a single week's net drawdown exceeds X (WATCH; a big planned
  outflow worth surfacing).
* `TroughWorsened` — the projected trough is lower (worse) than a baseline the
  caller passes in, typically the prior published forecast's trough (INFO).

Severity orders the fleet worst-first; it is an ``IntEnum`` so ``-int(severity)``
is a valid, deterministic sort key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import IntEnum
from typing import Protocol, runtime_checkable

from rgnr8_forecast import ForecastResult, Money

from .alert import Alert


class Severity(IntEnum):
    """How urgent an alert is; higher sorts first (worst-first)."""

    INFO = 0
    WATCH = 1
    AT_RISK = 2


@runtime_checkable
class AlertRule(Protocol):
    """Structural seam every rule variant satisfies."""

    def evaluate(self, forecast: ForecastResult) -> Alert | None: ...


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


@dataclass(frozen=True, slots=True)
class FloorBreachWithinWeeks:
    """Fire when the minimum-cash floor is projected to break within ``weeks``."""

    weeks: int

    def evaluate(self, forecast: ForecastResult) -> Alert | None:
        proj = forecast.projection
        breach = proj.breach
        if not breach.breached or breach.weeks_until is None:
            return None
        if breach.weeks_until > self.weeks:
            return None
        ccy = proj.currency
        where = f" (around {breach.first_date.isoformat()})" if breach.first_date else ""
        message = (
            f"Cash is projected to fall below your {ccy} "
            f"{proj.effective_floor.to_decimal_string()} floor in week "
            f"{breach.weeks_until}{where}, short by up to {ccy} "
            f"{breach.worst_shortfall.to_decimal_string()}."
        )
        return Alert(
            key=f"floor_breach_within_{self.weeks}w",
            severity=Severity.AT_RISK,
            title="Cash floor breach forming",
            message=message,
            evidence={
                "kind": "floor_breach_within_weeks",
                "threshold_weeks": self.weeks,
                "weeks_until": breach.weeks_until,
                "first_date": _iso(breach.first_date),
                "worst_shortfall_minor": breach.worst_shortfall.minor_units,
                "floor_minor": proj.effective_floor.minor_units,
                "currency": ccy,
            },
        )


@dataclass(frozen=True, slots=True)
class BalanceBelow:
    """Fire when the projected cash trough dips below ``amount``."""

    amount: Money

    def evaluate(self, forecast: ForecastResult) -> Alert | None:
        proj = forecast.projection
        trough = proj.trough
        if trough.balance.currency != self.amount.currency:
            return None
        if trough.balance.minor_units >= self.amount.minor_units:
            return None
        ccy = proj.currency
        message = (
            f"Projected cash reaches a low of {ccy} "
            f"{trough.balance.to_decimal_string()} around {trough.on_date.isoformat()}, "
            f"below your {self.amount.currency} {self.amount.to_decimal_string()} threshold."
        )
        return Alert(
            key=f"balance_below_{self.amount.currency}_{self.amount.minor_units}",
            severity=Severity.WATCH,
            title="Projected balance below threshold",
            message=message,
            evidence={
                "kind": "balance_below",
                "threshold_minor": self.amount.minor_units,
                "trough_minor": trough.balance.minor_units,
                "trough_on": trough.on_date.isoformat(),
                "currency": ccy,
            },
        )


@dataclass(frozen=True, slots=True)
class LargeOutflow:
    """Fire when a single week's net drawdown (opening - closing) exceeds ``amount``.

    Reports the *worst* qualifying week; the dedupe key pins that week's start so a
    later large outflow in a different week is a distinct alert.
    """

    amount: Money

    def evaluate(self, forecast: ForecastResult) -> Alert | None:
        proj = forecast.projection
        worst_week = None
        worst_drop = 0
        for week in proj.weeks:
            if week.closing.currency != self.amount.currency:
                continue
            drop = week.opening.minor_units - week.closing.minor_units
            if drop > self.amount.minor_units and drop > worst_drop:
                worst_week = week
                worst_drop = drop
        if worst_week is None:
            return None
        ccy = proj.currency
        drop_money = Money(worst_drop, ccy)
        message = (
            f"A large net outflow of {ccy} {drop_money.to_decimal_string()} is "
            f"projected in week {worst_week.index} (starting "
            f"{worst_week.start.isoformat()}), above your {self.amount.currency} "
            f"{self.amount.to_decimal_string()} threshold."
        )
        return Alert(
            key=(
                f"large_outflow_over_{self.amount.currency}_"
                f"{self.amount.minor_units}:{worst_week.start.isoformat()}"
            ),
            severity=Severity.WATCH,
            title="Large single-week outflow",
            message=message,
            evidence={
                "kind": "large_outflow",
                "threshold_minor": self.amount.minor_units,
                "week_index": worst_week.index,
                "week_start": worst_week.start.isoformat(),
                "drop_minor": worst_drop,
                "currency": ccy,
            },
        )


@dataclass(frozen=True, slots=True)
class TroughWorsened:
    """Fire when the projected trough is lower (worse) than baseline ``vs_amount``.

    ``vs_amount`` is a prior baseline the caller supplies — typically the last
    published forecast's trough balance — so this catches material worsening
    between forecasts even when no absolute floor is breached.
    """

    vs_amount: Money

    def evaluate(self, forecast: ForecastResult) -> Alert | None:
        proj = forecast.projection
        trough = proj.trough
        if trough.balance.currency != self.vs_amount.currency:
            return None
        if trough.balance.minor_units >= self.vs_amount.minor_units:
            return None
        ccy = proj.currency
        worse_by = Money(self.vs_amount.minor_units - trough.balance.minor_units, ccy)
        message = (
            f"The projected cash trough of {ccy} {trough.balance.to_decimal_string()} "
            f"(around {trough.on_date.isoformat()}) is {ccy} "
            f"{worse_by.to_decimal_string()} worse than the prior baseline of "
            f"{self.vs_amount.currency} {self.vs_amount.to_decimal_string()}."
        )
        return Alert(
            key=f"trough_worsened_vs_{self.vs_amount.currency}_{self.vs_amount.minor_units}",
            severity=Severity.INFO,
            title="Cash trough worsened vs. prior forecast",
            message=message,
            evidence={
                "kind": "trough_worsened",
                "baseline_minor": self.vs_amount.minor_units,
                "trough_minor": trough.balance.minor_units,
                "worse_by_minor": worse_by.minor_units,
                "trough_on": trough.on_date.isoformat(),
                "currency": ccy,
            },
        )
