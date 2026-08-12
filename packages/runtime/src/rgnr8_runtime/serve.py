"""The thin real-clock loop around ``DeliveryRuntime.tick``.

The runtime logic is a pure, injected-clock ``tick``; this is the only place a
real wall clock and sleeping live, kept minimal and out of the tested surface.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone

from .runtime import DeliveryRuntime


def serve(
    runtime: DeliveryRuntime,
    *,
    interval_seconds: float = 60.0,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_ticks: int | None = None,
) -> None:
    """Tick the runtime forever (or ``max_ticks`` times). ``now`` and ``sleep``
    are injectable so a harness can drive it without real time; by default it
    reads the system clock in UTC and sleeps for real."""
    clock = now if now is not None else (lambda: datetime.now(timezone.utc))
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        stamp = clock()
        runtime.tick(stamp, at=stamp.isoformat())
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        sleep(interval_seconds)
