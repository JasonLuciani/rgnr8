"""Proactive cash alerts on the production scheduler.

The weekly briefing is the habit; a *breach forming Tuesday* is an event that must
reach the owner now. `AlertsJob` is the scheduler seam that turns the alert engine
into a recurring fleet job: on every dispatcher tick it runs each tenant's forecast,
evaluates a default rule set (a floor breach forming within N weeks, and the
projected trough dipping below the owner's floor), and dispatches the newly-active
alerts through an injected `AlertSink` — deduped and re-armed by a shared
`AlertDispatcher`.

Everything is deterministic: the injected `clock` (epoch seconds) stamps each
alert's ``at`` and drives the dispatcher, so a tick is fully reproducible in tests.
Per-tenant alert keys are namespaced with the tenant id so two tenants breaching
the same rule stay distinct conditions, and the whole fleet's active set is passed
to `dispatch` in one call each tick so a cleared condition on any tenant re-arms
correctly.

    dispatcher = build_alert_dispatcher(sink, clock=clock)
    job = AlertsJob("alerts", fleet, dispatcher, clock)   # register alongside BriefingDispatchJob

or, in one call::

    job = build_alerts_job(fleet, sink, clock=clock)

`sink` is any `AlertSink`; wrap an existing delivery function with
`CallableAlertSink(deliver)` to reuse the briefing transport seam.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime

from rgnr8_forecast import Money, run_forecast
from rgnr8_alerts import (
    Alert,
    AlertDispatcher,
    AlertRule,
    AlertSink,
    AlertStateStore,
    BalanceBelow,
    Clock,
    FloorBreachWithinWeeks,
    InMemoryAlertStateStore,
    evaluate,
)

from .fleet import Fleet

DEFAULT_BREACH_WEEKS = 4


def default_alert_rules(floor: Money, *, weeks: int = DEFAULT_BREACH_WEEKS) -> list[AlertRule]:
    """The default per-tenant rule set the scheduler evaluates each tick.

    Two rules, matching the launch alert vocabulary: `FloorBreachWithinWeeks`
    (AT_RISK — the minimum-cash floor is projected to break within ``weeks``) and
    `BalanceBelow` the owner's ``floor`` (WATCH — the projected trough dips under
    the floor even if no hard breach window is inside the horizon)."""
    return [FloorBreachWithinWeeks(weeks), BalanceBelow(floor)]


def build_alert_dispatcher(
    sink: AlertSink,
    *,
    clock: Clock,
    store: AlertStateStore | None = None,
) -> AlertDispatcher:
    """An `AlertDispatcher` wired to ``sink`` with dedupe state (in-memory by
    default) and the injected ``clock`` for the deterministic dispatch stamp."""
    return AlertDispatcher(
        store if store is not None else InMemoryAlertStateStore(), sink, clock=clock
    )


@dataclass(slots=True)
class AlertsJob:
    """A scheduler `Job` that fires proactive cash alerts for the whole fleet.

    Each `run(now)` evaluates `default_alert_rules` against every tenant's fresh
    forecast, namespaces the resulting alert keys per tenant, and dispatches the
    combined fleet set through the shared `AlertDispatcher` (which delivers only
    newly-active conditions and re-arms cleared ones). It is due every tick — a
    forming breach must reach the owner promptly; the dispatcher's dedupe, not a
    coarse interval, is what keeps it quiet once fired."""

    name: str
    fleet: Fleet
    dispatcher: AlertDispatcher
    clock: Clock
    weeks: int = DEFAULT_BREACH_WEEKS

    def due(self, now: datetime, last_run: datetime | None) -> bool:
        return True  # timely by design; the dispatcher dedupes so it stays quiet

    def run(self, now: datetime) -> str:
        collected: list[Alert] = []
        for bt in self.fleet.tenants.values():
            fc = run_forecast(bt.inputs, bt.config)
            rules = default_alert_rules(bt.config.minimum_cash, weeks=self.weeks)
            for alert in evaluate(fc, rules, clock=self.clock):
                collected.append(
                    dataclasses.replace(
                        alert,
                        key=f"{bt.tenant_id}:{alert.key}",
                        evidence={
                            **alert.evidence,
                            "tenant_id": bt.tenant_id,
                            "tenant_name": bt.name,
                            "recipient": bt.recipient,
                        },
                    )
                )
        delivered = self.dispatcher.dispatch(collected)
        return f"{len(delivered)} alert(s) dispatched"


def build_alerts_job(
    fleet: Fleet,
    sink: AlertSink,
    *,
    clock: Clock,
    name: str = "alerts",
    weeks: int = DEFAULT_BREACH_WEEKS,
    store: AlertStateStore | None = None,
) -> AlertsJob:
    """Job factory: build the dispatcher + `AlertsJob` in one call so the worker
    can register it alongside `BriefingDispatchJob`. ``sink`` is any `AlertSink`
    (wrap a delivery callable with `CallableAlertSink` to reuse the briefing
    transport)."""
    dispatcher = build_alert_dispatcher(sink, clock=clock, store=store)
    return AlertsJob(name=name, fleet=fleet, dispatcher=dispatcher, clock=clock, weeks=weeks)
