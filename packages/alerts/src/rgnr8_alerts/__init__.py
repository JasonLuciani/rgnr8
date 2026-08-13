"""RGNR8 proactive-alert engine.

The weekly briefing is the habit; a *breach forming Tuesday* is an event that
must reach the owner now. This package turns a `ForecastResult` into deduped,
severity-ranked alerts and delivers them through an injected sink.

Two moving parts, both deterministic and seam-based (Protocol + in-memory impls,
injected clock, frozen dataclasses, stdlib only):

* Rules (`rules`) — small frozen predicates over the forecast projection:
  `FloorBreachWithinWeeks`, `BalanceBelow`, `LargeOutflow`, `TroughWorsened`.
  Each ``evaluate(forecast)`` returns an `Alert` (with a stable dedupe `key`) or
  ``None``.
* Engine (`engine`) — ``evaluate(forecast, rules)`` runs the rules and returns
  alerts worst-first; `AlertDispatcher` delivers only newly-active keys (dedupe)
  and re-arms conditions that clear, over the `AlertStateStore` and `AlertSink`
  seams.

The scheduler calls, per tenant per tick::

    dispatcher.dispatch(evaluate(forecast, rules, clock=clock))

so an unchanged risk stays quiet, a new one fires once, and a resolved-then-
recurring one fires again. Delivery transport (email/push/webhook) binds a real
`AlertSink` later; the state store binds a durable implementation later.
"""

from __future__ import annotations

from .alert import Alert
from .rules import (
    AlertRule,
    BalanceBelow,
    FloorBreachWithinWeeks,
    LargeOutflow,
    Severity,
    TroughWorsened,
)
from .engine import (
    AlertDispatcher,
    AlertSink,
    AlertStateStore,
    CallableAlertSink,
    Clock,
    InMemoryAlertSink,
    InMemoryAlertStateStore,
    evaluate,
)

__version__ = "0.1.0"

__all__ = [
    # alert value object
    "Alert",
    "Severity",
    # rules
    "AlertRule",
    "FloorBreachWithinWeeks",
    "BalanceBelow",
    "LargeOutflow",
    "TroughWorsened",
    # engine
    "evaluate",
    "Clock",
    "AlertSink",
    "InMemoryAlertSink",
    "CallableAlertSink",
    "AlertStateStore",
    "InMemoryAlertStateStore",
    "AlertDispatcher",
]
