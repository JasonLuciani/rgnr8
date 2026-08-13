"""The alert engine: evaluate rules, then dispatch with dedupe + re-arm.

Two responsibilities, kept separate:

* ``evaluate(forecast, rules)`` runs every rule against one `ForecastResult`,
  stamps each resulting alert's ``at`` from an injected clock, and returns them
  **worst-first** (severity descending, then key for a deterministic tiebreak).

* `AlertDispatcher` owns delivery. Given the alerts for a tick it delivers only
  the keys that are *newly* active — a condition that was already firing stays
  quiet (dedupe) — and it re-arms cleared conditions: any key that was active but
  is absent this tick is dropped from the store, so if it recurs later it fires
  again. State lives behind the `AlertStateStore` seam (in-memory here; a durable
  store implements the same Protocol); delivery lives behind the `AlertSink` seam
  (in-memory / any callable here; email/push/webhook later).

The scheduler wires these together per tenant per tick:
``dispatcher.dispatch(evaluate(forecast, rules, clock=clock))``.
"""

from __future__ import annotations

import dataclasses
from typing import Callable, Protocol, Sequence, runtime_checkable

from rgnr8_forecast import ForecastResult

from .alert import Alert
from .rules import AlertRule

Clock = Callable[[], float]


def _zero_clock() -> float:
    return 0.0


def evaluate(
    forecast: ForecastResult,
    rules: Sequence[AlertRule],
    *,
    clock: Clock = _zero_clock,
) -> list[Alert]:
    """Evaluate ``rules`` against ``forecast``; return fired alerts worst-first.

    Each fired alert is stamped with ``at = int(clock())``. Ordering is
    ``severity`` descending then ``key`` ascending — total and deterministic.
    """
    at = int(clock())
    fired: list[Alert] = []
    for rule in rules:
        alert = rule.evaluate(forecast)
        if alert is not None:
            fired.append(dataclasses.replace(alert, at=at))
    fired.sort(key=lambda a: (-int(a.severity), a.key))
    return fired


# --- delivery seam ----------------------------------------------------------- #


@runtime_checkable
class AlertSink(Protocol):
    """Where a delivered alert goes. Real sinks: email, push, webhook."""

    def send(self, alert: Alert) -> None: ...


class InMemoryAlertSink:
    """Records delivered alerts for assertions/local runs, in delivery order."""

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.sent.append(alert)


class CallableAlertSink:
    """Adapts any ``Callable[[Alert], None]`` into an `AlertSink`.

    Lets a caller wire delivery to an email/push/webhook function without writing
    a class — ``CallableAlertSink(notifier.send_push)``.
    """

    def __init__(self, fn: Callable[[Alert], None]) -> None:
        self._fn = fn

    def send(self, alert: Alert) -> None:
        self._fn(alert)


# --- dedupe state seam ------------------------------------------------------- #


@runtime_checkable
class AlertStateStore(Protocol):
    """Remembers which alert keys are currently active (already fired, not cleared)."""

    def active_keys(self) -> frozenset[str]: ...
    def is_active(self, key: str) -> bool: ...
    def mark(self, key: str) -> None: ...
    def unmark(self, key: str) -> None: ...


class InMemoryAlertStateStore:
    """In-process active-key set. A durable store implements the same Protocol."""

    def __init__(self) -> None:
        self._active: set[str] = set()

    def active_keys(self) -> frozenset[str]:
        return frozenset(self._active)

    def is_active(self, key: str) -> bool:
        return key in self._active

    def mark(self, key: str) -> None:
        self._active.add(key)

    def unmark(self, key: str) -> None:
        self._active.discard(key)


class AlertDispatcher:
    """Delivers newly-active alerts once; re-arms conditions that clear.

    ``dispatch(alerts)`` is the per-tick entrypoint. An alert whose key is already
    active is suppressed (dedupe). A key that was active last tick but is absent
    this tick is unmarked so the same condition can fire again if it recurs.
    """

    def __init__(
        self,
        store: AlertStateStore,
        sink: AlertSink,
        *,
        clock: Clock,
    ) -> None:
        self._store = store
        self._sink = sink
        self._clock = clock
        self.last_dispatch_at: int = 0

    def dispatch(self, alerts: Sequence[Alert]) -> list[Alert]:
        """Deliver the newly-active alerts; return them in input order."""
        self.last_dispatch_at = int(self._clock())
        present = {alert.key for alert in alerts}

        # Re-arm: any previously-active key absent this tick has cleared.
        for key in self._store.active_keys():
            if key not in present:
                self._store.unmark(key)

        delivered: list[Alert] = []
        seen: set[str] = set()
        for alert in alerts:
            if alert.key in seen:
                continue
            seen.add(alert.key)
            if not self._store.is_active(alert.key):
                self._store.mark(alert.key)
                self._sink.send(alert)
                delivered.append(alert)
        return delivered
