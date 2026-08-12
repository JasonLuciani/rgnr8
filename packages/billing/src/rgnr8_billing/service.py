"""BillingService — the one place customer lifecycle + metering + invoicing meet.

Ties the account store, the plan/entitlement model, the usage meter, and the
provider seam together behind a small API the rest of the platform calls:
create an account, attach a business (entitlement-checked), start billing, record
usage, and close a period into an invoice. The clock is injected — deterministic,
no wall-clock reads — so runs are reproducible and testable.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable

from .accounts import Account, AccountStatus
from .entitlements import Entitlements
from .invoice import Invoice, build_invoice
from .plan_changes import PlanChange, PlanChangeKind, classify_change
from .plans import Feature, Tier, plan_for
from .provider import BillingPortalProvider, BillingProvider, FakeBillingProvider
from .store import AccountStore, InMemoryAccountStore
from .usage import UsageEvent, UsageKind, UsageSummary


class BillingError(Exception):
    """A billing operation that isn't allowed (unknown account, entitlement)."""


class EntitlementError(BillingError):
    """The account's plan doesn't permit this action."""


_DAY = 86_400


class BillingService:
    def __init__(
        self,
        store: AccountStore | None = None,
        provider: BillingProvider | None = None,
        *,
        clock: Callable[[], int] | None = None,
        portal: BillingPortalProvider | None = None,
        on_plan_change: Callable[[PlanChange], None] | None = None,
        reactivate_grace_days: int = 30,
    ) -> None:
        self._store: AccountStore = store if store is not None else InMemoryAccountStore()
        self._provider: BillingProvider = provider if provider is not None else FakeBillingProvider()
        self._clock = clock if clock is not None else (lambda: int(time.time()))
        self._portal = portal
        self._on_plan_change = on_plan_change
        self._grace_days = reactivate_grace_days
        # periods already finalized (account, period) — makes close_period idempotent
        self._closed: set[tuple[str, str]] = set()
        # append-only audit of tier moves (also fanned out to on_plan_change)
        self._plan_changes: list[PlanChange] = []
        # when an account was canceled/paused — gates the reactivate grace window
        self._grace_start: dict[str, int] = {}

    # --- lifecycle -----------------------------------------------------------
    def create_account(
        self, account_id: str, name: str, billing_email: str, tier: Tier,
        *, trial_days: int = 14,
    ) -> Account:
        now = self._clock()
        acct = Account(
            id=account_id, name=name, billing_email=billing_email, tier=tier,
            status=AccountStatus.TRIALING,
            trial_end=now + trial_days * _DAY if trial_days > 0 else None,
            created_at=now,
        )
        customer_id = self._provider.ensure_customer(acct)
        acct = acct.with_billing_refs(customer_id=customer_id)
        self._store.save_account(acct)
        return acct

    def activate(self, account_id: str) -> Account:
        """Start the paid subscription (trial → active)."""
        acct = self._require(account_id)
        plan = plan_for(acct.tier)
        sub_id = self._provider.ensure_subscription(acct, plan)
        acct = acct.with_billing_refs(subscription_id=sub_id).with_status(AccountStatus.ACTIVE)
        self._store.save_account(acct)
        return acct

    def set_status(self, account_id: str, status: AccountStatus) -> Account:
        acct = self._require(account_id).with_status(status)
        self._store.save_account(acct)
        return acct

    def record_billing_refs(
        self, account_id: str, *,
        customer_id: str | None = None, subscription_id: str | None = None,
    ) -> Account:
        """Stamp processor ids the platform learned out-of-band (e.g. from a
        checkout webhook) onto the account, without re-creating them at the
        provider. Only non-None refs are updated."""
        acct = self._require(account_id).with_billing_refs(
            customer_id=customer_id, subscription_id=subscription_id)
        self._store.save_account(acct)
        return acct

    def change_tier(self, account_id: str, tier: Tier) -> Account:
        acct = self._require(account_id)
        acct = dataclasses.replace(acct, tier=tier)
        # keep the processor subscription in step with the new plan
        self._provider.ensure_subscription(acct, plan_for(tier))
        self._store.save_account(acct)
        return acct

    # --- self-serve plan lifecycle ------------------------------------------
    def change_plan(self, account_id: str, new_tier: Tier) -> Account:
        """Move an account between tiers with downgrade guardrails.

        Upgrades (higher base price) are always allowed. A downgrade is refused
        when it would strand resources the target plan can't hold: more businesses
        than the new plan's ``included_tenants`` (only when the new plan lacks
        MULTI_TENANT — a multi-tenant plan bills extras as overage), or more seats
        than the new plan's ``included_seats``. The processor subscription is kept
        in step and the move is recorded (history + ``on_plan_change`` seam).
        """
        acct = self._require_live(self._require(account_id))
        old_tier = acct.tier
        old_plan = plan_for(old_tier)
        new_plan = plan_for(new_tier)
        kind = classify_change(old_plan, new_plan)
        if kind is PlanChangeKind.DOWNGRADE:
            if not new_plan.has(Feature.MULTI_TENANT) and acct.tenant_count > new_plan.included_tenants:
                raise EntitlementError(
                    f"cannot downgrade {account_id} to {new_tier.value}: "
                    f"{acct.tenant_count} business(es) exceed the plan's "
                    f"{new_plan.included_tenants} included; remove businesses first")
            if acct.seats_used > new_plan.included_seats:
                raise EntitlementError(
                    f"cannot downgrade {account_id} to {new_tier.value}: "
                    f"{acct.seats_used} seat(s) exceed the plan's "
                    f"{new_plan.included_seats} included; remove seats first")
        acct = dataclasses.replace(acct, tier=new_tier)
        # keep the processor subscription in step with the new plan
        self._provider.ensure_subscription(acct, new_plan)
        self._store.save_account(acct)
        change = PlanChange(account_id, old_tier, new_tier, kind, at=self._clock())
        self._plan_changes.append(change)
        if self._on_plan_change is not None:
            self._on_plan_change(change)
        return acct

    def cancel(self, account_id: str, *, at_period_end: bool = True) -> Account:
        """Churn an account: mark it CANCELED and fire the provider cancel hook.
        ``at_period_end`` is passed through to the processor (cancel at period end
        vs immediately). Entitlements fail closed as soon as the account is
        CANCELED. Records the moment so a later ``reactivate`` can honor a grace
        window."""
        acct = self._require(account_id)
        self._provider.cancel_subscription(acct, at_period_end=at_period_end)
        acct = acct.with_status(AccountStatus.CANCELED)
        self._store.save_account(acct)
        self._grace_start[account_id] = self._clock()
        return acct

    def pause(self, account_id: str) -> Account:
        """Temporarily suspend an account (kept, not billed). Entitlements fail
        closed while PAUSED; ``resume``/``reactivate`` restore it."""
        acct = self._require(account_id).with_status(AccountStatus.PAUSED)
        self._store.save_account(acct)
        self._grace_start[account_id] = self._clock()
        return acct

    def resume(self, account_id: str) -> Account:
        """Lift a pause: PAUSED → ACTIVE. Refuses if the account isn't paused."""
        acct = self._require(account_id)
        if acct.status is not AccountStatus.PAUSED:
            raise BillingError(f"account {account_id} is not paused ({acct.status.value})")
        return self._revive(acct)

    def reactivate(self, account_id: str) -> Account:
        """Revive a canceled or paused account back to ACTIVE, but only within the
        reactivation grace window. Refuses if the account is in another state or
        the grace window has lapsed."""
        acct = self._require(account_id)
        if acct.status not in (AccountStatus.CANCELED, AccountStatus.PAUSED):
            raise BillingError(
                f"account {account_id} is {acct.status.value}; "
                "only canceled or paused accounts can be reactivated")
        start = self._grace_start.get(account_id)
        if start is not None and self._clock() - start > self._grace_days * _DAY:
            raise BillingError(
                f"reactivation window for {account_id} has expired "
                f"({self._grace_days}-day grace)")
        return self._revive(acct)

    def _revive(self, acct: Account) -> Account:
        """Flip an account back to ACTIVE and re-assert its subscription."""
        self._provider.ensure_subscription(acct, plan_for(acct.tier))
        acct = acct.with_status(AccountStatus.ACTIVE)
        self._store.save_account(acct)
        self._grace_start.pop(acct.id, None)
        return acct

    def plan_changes(self, account_id: str) -> list[PlanChange]:
        """The recorded tier moves for an account, oldest first."""
        return [c for c in self._plan_changes if c.account_id == account_id]

    # --- billing portal (customer self-serve) --------------------------------
    def create_portal_session(self, account_id: str, return_url: str) -> str:
        """Open a hosted billing-portal session for the account's customer so they
        can self-serve manage their subscription. Requires a portal provider."""
        if self._portal is None:
            raise BillingError("no billing portal provider configured")
        acct = self._require(account_id)
        return self._portal.create_portal_session(acct, return_url)

    # --- tenancy (entitlement-checked) --------------------------------------
    def _require_live(self, acct: Account) -> Account:
        """Reject actions on suspended/churned accounts — a canceled or paused
        account keeps its plan shape but loses the ability to add businesses or
        accrue usage."""
        if acct.status in (AccountStatus.CANCELED, AccountStatus.PAUSED):
            raise BillingError(f"account {acct.id} is {acct.status.value}")
        return acct

    def attach_tenant(self, account_id: str, tenant_id: str) -> Account:
        acct = self._require_live(self._require(account_id))
        if tenant_id not in acct.tenant_ids and not Entitlements.of(acct).can_add_tenant():
            raise EntitlementError(
                f"{acct.tier.value} plan allows {plan_for(acct.tier).included_tenants} "
                "business(es); upgrade to add more")
        acct = acct.with_tenant(tenant_id)
        self._store.save_account(acct)
        return acct

    def detach_tenant(self, account_id: str, tenant_id: str) -> Account:
        acct = self._require(account_id).without_tenant(tenant_id)
        self._store.save_account(acct)
        return acct

    # --- metering ------------------------------------------------------------
    def record_usage(
        self, account_id: str, kind: UsageKind, quantity: int, period: str,
        *, tenant_id: str = "", note: str = "",
    ) -> UsageEvent:
        acct = self._require_live(self._require(account_id))
        event = UsageEvent(acct.id, kind, quantity, period, tenant_id=tenant_id,
                           at=self._clock(), note=note)
        self._store.append_usage(event)
        self._provider.report_usage(acct, kind, quantity, at=event.at)
        return event

    def record_analyst_minutes(
        self, account_id: str, minutes: int, period: str, *, tenant_id: str = "", note: str = "",
    ) -> UsageEvent:
        return self.record_usage(account_id, UsageKind.ANALYST_MINUTES, minutes, period,
                                 tenant_id=tenant_id, note=note)

    def usage_summary(self, account_id: str, period: str) -> UsageSummary:
        events = self._store.usage_for(account_id, period)
        return UsageSummary.summarize(account_id, period, events)

    # --- invoicing -----------------------------------------------------------
    def close_period(self, account_id: str, period: str) -> Invoice:
        """Fold the period's usage into an invoice and finalize it at the provider.
        The amounts are authoritative here (never re-derived by the processor)."""
        acct = self._require(account_id)
        plan = plan_for(acct.tier)
        summary = self.usage_summary(account_id, period)
        invoice = build_invoice(acct, plan, summary)
        # Idempotent: finalize at the provider only once per (account, period). A
        # retry / scheduler re-run returns the same invoice without double-billing.
        if (account_id, period) not in self._closed:
            self._provider.finalize_invoice(invoice)
            self._closed.add((account_id, period))
        return invoice

    # --- reads ---------------------------------------------------------------
    def get_account(self, account_id: str) -> Account | None:
        return self._store.get_account(account_id)

    def account_for_tenant(self, tenant_id: str) -> Account | None:
        return self._store.find_by_tenant(tenant_id)

    def entitlements(self, account_id: str) -> Entitlements:
        return Entitlements.of(self._require(account_id))

    def feature_enabled(self, account_id: str, feature: Feature) -> bool:
        acct = self._store.get_account(account_id)
        return acct is not None and Entitlements.of(acct).feature_enabled(feature)

    def _require(self, account_id: str) -> Account:
        acct = self._store.get_account(account_id)
        if acct is None:
            raise BillingError(f"unknown account {account_id}")
        return acct
