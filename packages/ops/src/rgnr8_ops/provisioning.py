"""The composed growth seam: signup → checkout → provisioning → metering.

`Provisioning` ties together the three seams that make a fresh deployment genuinely
login-ready end-to-end, without any of them importing the others directly:

* an `EventTracker` (analytics) that records the acquisition→activation funnel;
* billing's `SelfServeSignup`, whose injected ``on_provisioned`` hook flips an
  account ACTIVE, emits the funnel events, and hands off to the fleet-onboard seam;
* a ``usage_recorder`` callable for web's metering hook that meters tenant usage
  back into billing.

Everything is optional and injected — the dev path constructs in-memory defaults
(an in-memory billing service, a fake checkout provider, an in-memory event sink)
so `create_application()` with no args still builds. `on_provisioned` only receives
``(account_id, billing_email)``; the concrete tenant onboarding (which needs a
captured ``forecast-inputs/1`` DTO) still flows through the operator console /
`PlatformAdmin`, so here we record the account↔tenant association that later lets
metering resolve a tenant to its billing account.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from rgnr8_analytics import EventTracker, InMemoryEventSink
from rgnr8_billing import BillingService, FakeCheckoutProvider, SelfServeSignup, UsageKind

from .fleet import Fleet

_USAGE_KINDS: dict[str, UsageKind] = {k.value: k for k in UsageKind}


class Provisioning:
    """Assembled signup/checkout/provisioning/metering seam for the served app."""

    def __init__(
        self,
        *,
        tracker: EventTracker,
        billing: BillingService | None = None,
        checkout: FakeCheckoutProvider | None = None,
        success_url: str = "https://app.rgnr8.co/welcome",
        cancel_url: str = "https://app.rgnr8.co/pricing",
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.tracker = tracker
        self.billing = billing if billing is not None else BillingService(clock=lambda: int(clock()))
        self._checkout = checkout if checkout is not None else FakeCheckoutProvider()
        self._clock = clock
        self._fleet: Fleet | None = None
        # tenant_id -> account_id, so a metered tenant event maps to its billing account.
        self.account_of: dict[str, str] = {}
        self.signup = SelfServeSignup(
            self.billing,
            self._checkout,
            success_url=success_url,
            cancel_url=cancel_url,
            on_provisioned=self._on_provisioned,
        )

    def bind_fleet(self, fleet: Fleet) -> None:
        """Give provisioning the live fleet so `on_provisioned` can reach it."""
        self._fleet = fleet

    def _on_provisioned(self, account_id: str, billing_email: str) -> None:
        """SelfServeSignup fires this once per completed checkout (idempotent
        upstream). Emit the funnel events and record the account↔tenant link the
        metering path resolves against."""
        self.tracker.account_activated(distinct_id=billing_email, account_id=account_id)
        self.tracker.tenant_onboarded(distinct_id=billing_email, account_id=account_id)
        fleet = self._fleet
        if fleet is not None:
            for tid, bt in fleet.tenants.items():
                if bt.recipient == billing_email:
                    self.account_of.setdefault(tid, account_id)

    def usage_recorder(self, tenant_id: str, kind: str, quantity: int) -> None:
        """web's metering hook ``(tenant, kind, quantity)`` → billing usage.

        Best-effort and safe: an unmapped tenant or an unknown usage kind is a
        no-op, and a billing error never propagates into the request path."""
        account_id = self.account_of.get(tenant_id)
        usage_kind = _USAGE_KINDS.get(kind)
        if account_id is None or usage_kind is None:
            return
        period = time.strftime("%Y-%m", time.gmtime(self._clock()))
        try:
            self.billing.record_usage(account_id, usage_kind, quantity, period, tenant_id=tenant_id)
        except Exception:
            pass


def build_provisioning(
    *,
    tracker: EventTracker | None = None,
    clock: Callable[[], float] = time.time,
) -> Provisioning:
    """Assemble a `Provisioning` with in-memory dev defaults. Pass a ``tracker``
    bound to a real `EventSink` in production; omit for the in-memory dev path."""
    tr = tracker if tracker is not None else EventTracker(clock=clock, sink=InMemoryEventSink())
    return Provisioning(tracker=tr, clock=clock)
