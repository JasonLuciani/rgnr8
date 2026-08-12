"""Self-serve signup → subscription → provisioning, on top of BillingService.

`SelfServeSignup` is the public-track orchestration: a prospect picks a tier,
we open a TRIALING account and a hosted checkout session for it, and — when Stripe
tells us the session completed — we stamp the real customer/subscription ids on
the account and flip it ACTIVE. The flow is idempotent: replaying the same
completed event is a no-op (no double-activate, the provisioning hook fires once).

Billing must not depend on web/ops, so the tenant/fleet provisioning is an
injected `on_provisioned` callback seam — the fleet layer hooks in there without
this package importing it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .accounts import Account, AccountStatus
from .checkout import CheckoutProvider, CheckoutSession
from .plans import Tier
from .service import BillingService


class SelfServeSignup:
    def __init__(
        self,
        service: BillingService,
        checkout: CheckoutProvider,
        *,
        success_url: str,
        cancel_url: str,
        trial_days: int = 14,
        account_id_factory: Callable[[], str] | None = None,
        on_provisioned: Callable[[str, str], None] | None = None,
    ) -> None:
        self._service = service
        self._checkout = checkout
        self._success_url = success_url
        self._cancel_url = cancel_url
        self._trial_days = trial_days
        self._new_id = account_id_factory if account_id_factory is not None else self._default_ids()
        self._on_provisioned = on_provisioned
        # subscription ids already provisioned — makes complete_checkout idempotent
        self._provisioned: set[str] = set()

    @staticmethod
    def _default_ids() -> Callable[[], str]:
        """Deterministic default id factory: acct_1, acct_2, … per instance."""
        counter = {"n": 0}

        def _next() -> str:
            counter["n"] += 1
            return f"acct_{counter['n']}"

        return _next

    # --- start: trialing account + hosted checkout session -------------------
    def start_checkout(self, email: str, tier: Tier) -> tuple[Account, CheckoutSession]:
        """Open a TRIALING account for the prospect and a checkout session that
        will subscribe them to ``tier`` on completion."""
        account_id = self._new_id()
        account = self._service.create_account(
            account_id, email, email, tier, trial_days=self._trial_days)
        session = self._checkout.create_checkout_session(
            account_id=account_id,
            tier=tier,
            success_url=self._success_url,
            cancel_url=self._cancel_url,
            customer_email=email,
        )
        return account, session

    # --- complete: provision from the completed-checkout webhook -------------
    def complete_checkout(self, payload: Mapping[str, object]) -> Account | None:
        """Parse a completed-checkout webhook and provision the account: stamp the
        Stripe customer/subscription ids and flip it ACTIVE. Idempotent and safe
        on foreign/bad payloads — returns the account when it acted (or was already
        provisioned), None when the payload was ignored."""
        completed = self._checkout.parse_completed_event(payload)
        if completed is None:
            return None  # not a completed-checkout event, or unparseable — ignore
        acct = self._service.get_account(completed.account_id)
        if acct is None:
            return None  # foreign / unknown account — ignore
        if completed.subscription_id in self._provisioned:
            return acct  # already provisioned this subscription — no-op replay
        self._service.record_billing_refs(
            completed.account_id,
            customer_id=completed.customer_id or None,
            subscription_id=completed.subscription_id,
        )
        acct = self._service.set_status(completed.account_id, AccountStatus.ACTIVE)
        self._provisioned.add(completed.subscription_id)
        if self._on_provisioned is not None:
            # tenant hint = the signup email; the fleet layer resolves it to a
            # concrete tenant without billing knowing how.
            self._on_provisioned(acct.id, acct.billing_email)
        return acct
