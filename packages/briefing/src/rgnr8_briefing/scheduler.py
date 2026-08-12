"""A deterministic scheduler for the recurring weekly briefing.

`Schedule` (in delivery.py) is data only — weekday/hour/minute/timezone. This
module is the consumer: given an injected wall-clock ``now`` (timezone-aware,
never read from the system clock here — determinism is a core invariant) and the
last time a tenant was sent to, it decides whether a briefing is *due* and drives
the `Deliverer`. It uses catch-up semantics: due when the most recent scheduled
fire time is strictly newer than the last delivery, so a missed tick still sends
once (and only once) on the next run rather than being silently dropped.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .delivery import Deliverer, DeliveryEnvelope, DeliveryReceipt, Schedule


def _in_zone(when: datetime, schedule: Schedule) -> datetime:
    if when.tzinfo is None:
        raise ValueError("now must be timezone-aware (determinism: inject the clock)")
    return when.astimezone(ZoneInfo(schedule.timezone))


def most_recent_fire(schedule: Schedule, now: datetime) -> datetime:
    """The latest scheduled datetime at or before ``now`` (in the schedule's tz)."""
    local = _in_zone(now, schedule)
    days_back = (local.weekday() - schedule.weekday) % 7
    candidate = local.replace(
        hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0
    ) - timedelta(days=days_back)
    if candidate > local:
        candidate -= timedelta(days=7)
    return candidate


def next_fire(schedule: Schedule, after: datetime) -> datetime:
    """The earliest scheduled datetime strictly after ``after`` (in the schedule's tz)."""
    local = _in_zone(after, schedule)
    days_ahead = (schedule.weekday - local.weekday()) % 7
    candidate = local.replace(
        hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0
    ) + timedelta(days=days_ahead)
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate


def is_due(schedule: Schedule, now: datetime, last_sent: datetime | None) -> bool:
    """Due when the most recent scheduled fire is newer than the last delivery."""
    fire = most_recent_fire(schedule, now)
    if last_sent is None:
        return True
    return last_sent < fire


@dataclass(slots=True)
class Subscription:
    """A tenant's standing subscription to the weekly briefing."""

    tenant_id: str
    recipient: str
    schedule: Schedule
    last_sent: datetime | None = None
    active: bool = True  # paused subscriptions stay stored but never fire


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    tenant_id: str
    fired: bool
    fire_time: datetime | None
    receipt: DeliveryReceipt | None
    skipped_reason: str | None = None


def run_due(
    subscriptions: list[Subscription],
    now: datetime,
    envelope_for: Callable[[Subscription], DeliveryEnvelope | None],
    deliverer: Deliverer,
    at: str | None = None,
) -> list[DeliveryOutcome]:
    """Send to every subscription that is due, advancing each fired subscription's
    ``last_sent`` to the fire time it satisfied (not ``now``) so the cadence stays
    stable. ``envelope_for`` yields the tenant's envelope (None → skip, e.g. its
    numbers didn't reconcile). Mutates ``subscriptions`` in place."""
    stamp = at if at is not None else now.isoformat()
    outcomes: list[DeliveryOutcome] = []
    for sub in subscriptions:
        if not sub.active:
            outcomes.append(DeliveryOutcome(sub.tenant_id, False, None, None, "paused"))
            continue
        if not is_due(sub.schedule, now, sub.last_sent):
            outcomes.append(DeliveryOutcome(sub.tenant_id, False, None, None, "not_due"))
            continue
        fire = most_recent_fire(sub.schedule, now)
        envelope = envelope_for(sub)
        if envelope is None:
            outcomes.append(DeliveryOutcome(sub.tenant_id, False, fire, None, "no_envelope"))
            continue
        receipt = deliverer.send(envelope, stamp)
        sub.last_sent = fire
        outcomes.append(DeliveryOutcome(sub.tenant_id, True, fire, receipt))
    return outcomes
