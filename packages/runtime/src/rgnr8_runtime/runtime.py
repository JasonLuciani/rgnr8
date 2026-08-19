"""The delivery runtime: turn the scheduler into a runnable, durable process.

`DeliveryRuntime.tick(now)` is the whole job in one deterministic step:
1. load every subscription (with its persisted `last_sent`) from the store,
2. for each *due* subscription, resolve the tenant, run its forecast, build the
   briefing, run the unsupported-number validator, and — only if it passes —
   build the delivery envelope,
3. hand the due set to the scheduler's `run_due`, which delivers and advances
   each fired subscription's `last_sent`,
4. persist every fired subscription back to the store.

`now` is injected, so a tick is fully testable and reproducible; `serve()` is the
thin real-clock loop around it. A tenant whose numbers don't validate is skipped
(its `last_sent` is not advanced), so it retries next tick rather than shipping an
unbacked briefing — the same invariant the web surface enforces.
"""

from __future__ import annotations

from datetime import datetime

from rgnr8_briefing import (
    Deliverer,
    DeliveryEnvelope,
    DeliveryOutcome,
    Subscription,
    build_briefing,
    build_envelope,
    run_due,
    validate_briefing,
)
from rgnr8_forecast import run_forecast

from .subscriptions import SubscriptionStore
from .tenants import TenantSource


class DeliveryRuntime:
    def __init__(
        self,
        subscriptions: SubscriptionStore,
        tenants: TenantSource,
        deliverer: Deliverer,
        *,
        base_url: str = "https://app.rgnr8.com",
    ) -> None:
        self._subs = subscriptions
        self._tenants = tenants
        self._deliverer = deliverer
        self._base_url = base_url

    def _envelope_for(self, sub: Subscription) -> DeliveryEnvelope | None:
        tenant = self._tenants.resolve(sub.tenant_id)
        if tenant is None:
            return None  # unknown tenant → skip (don't advance the cursor)
        forecast = run_forecast(tenant.inputs, tenant.config)
        briefing = build_briefing(forecast)
        # unsupported-number validator: never ship a briefing whose numbers
        # don't recompute from the forecast.
        if validate_briefing(briefing, forecast):
            return None
        return build_envelope(
            briefing,
            sub.recipient,
            tenant_id=sub.tenant_id,
            base_url=self._base_url,
        )

    def tick(self, now: datetime, at: str | None = None) -> list[DeliveryOutcome]:
        subs = self._subs.list()
        outcomes = run_due(subs, now, self._envelope_for, self._deliverer, at)
        # run_due mutates each fired subscription's last_sent in place and returns
        # one outcome per subscription, in order — persist the ones that fired.
        for sub, outcome in zip(subs, outcomes):
            if outcome.fired:
                self._subs.save(sub)
        return outcomes
