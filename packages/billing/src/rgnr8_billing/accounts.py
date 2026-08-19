"""The Account — the billing customer, and the root of the tenancy tree.

Tenancy is three levels: an **Account** (who we bill) owns one or more
**Tenants** (businesses); a Tenant has **Users** (members with roles — see
`rgnr8_web.rbac`). A direct SMB is an account with a single tenant; an accounting
firm on the co-delivery tier is one account owning many client tenants. Billing,
plan, and subscription state live on the account and roll up across its tenants;
authorization stays per-tenant in RBAC.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum

from .plans import Tier


class AccountStatus(str, Enum):
    TRIALING = "trialing"   # in trial, not yet paying
    ACTIVE = "active"       # subscription in good standing
    PAST_DUE = "past_due"   # payment failed — dunning
    PAUSED = "paused"       # temporarily suspended (kept, not billed)
    CANCELED = "canceled"   # churned


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    name: str
    billing_email: str
    tier: Tier
    status: AccountStatus = AccountStatus.TRIALING
    tenant_ids: tuple[str, ...] = ()
    seats_used: int = 0
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    trial_end: int | None = None   # epoch seconds
    created_at: int = 0

    def with_status(self, status: AccountStatus) -> "Account":
        return dataclasses.replace(self, status=status)

    def with_tenant(self, tenant_id: str) -> "Account":
        if tenant_id in self.tenant_ids:
            return self
        return dataclasses.replace(self, tenant_ids=(*self.tenant_ids, tenant_id))

    def without_tenant(self, tenant_id: str) -> "Account":
        return dataclasses.replace(
            self, tenant_ids=tuple(t for t in self.tenant_ids if t != tenant_id))

    def with_billing_refs(self, *, customer_id: str | None = None,
                          subscription_id: str | None = None) -> "Account":
        return dataclasses.replace(
            self,
            stripe_customer_id=customer_id if customer_id is not None else self.stripe_customer_id,
            stripe_subscription_id=subscription_id if subscription_id is not None else self.stripe_subscription_id,
        )

    @property
    def tenant_count(self) -> int:
        return len(self.tenant_ids)

    @property
    def is_billable(self) -> bool:
        """Active or past-due accounts get billed; trialing/paused/canceled don't."""
        return self.status in (AccountStatus.ACTIVE, AccountStatus.PAST_DUE)
