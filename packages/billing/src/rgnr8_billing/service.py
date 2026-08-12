"""BillingService — the one place customer lifecycle + metering + invoicing meet.

Ties the account store, the plan/entitlement model, the usage meter, and the
provider seam together behind a small API the rest of the platform calls:
create an account, attach a business (entitlement-checked), start billing, record
usage, and close a period into an invoice. The clock is injected — deterministic,
no wall-clock reads — so runs are reproducible and testable.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .accounts import Account, AccountStatus
from .entitlements import Entitlements
from .invoice import Invoice, build_invoice
from .plans import Feature, Tier, plan_for
from .provider import BillingProvider, FakeBillingProvider
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
    ) -> None:
        self._store: AccountStore = store if store is not None else InMemoryAccountStore()
        self._provider: BillingProvider = provider if provider is not None else FakeBillingProvider()
        self._clock = clock if clock is not None else (lambda: int(time.time()))
        # periods already finalized (account, period) — makes close_period idempotent
        self._closed: set[tuple[str, str]] = set()

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

    def change_tier(self, account_id: str, tier: Tier) -> Account:
        acct = self._require(account_id)
        import dataclasses
        acct = dataclasses.replace(acct, tier=tier)
        # keep the processor subscription in step with the new plan
        self._provider.ensure_subscription(acct, plan_for(tier))
        self._store.save_account(acct)
        return acct

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
