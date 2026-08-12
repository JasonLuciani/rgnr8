"""Entitlements — what an account's plan lets it do, in one place.

Feature gates and limits are derived from the account's plan so callers ask one
object instead of scattering tier checks. Limits are *soft* where the plan
carries an overage rate (adding a business beyond the bundle is allowed and
billed) and *hard* where there's no overage price (a feature the tier lacks).
"""

from __future__ import annotations

from dataclasses import dataclass

from .accounts import Account, AccountStatus
from .plans import Feature, Plan, plan_for


@dataclass(frozen=True, slots=True)
class Entitlements:
    account: Account
    plan: Plan

    @staticmethod
    def of(account: Account) -> "Entitlements":
        return Entitlements(account, plan_for(account.tier))

    def feature_enabled(self, feature: Feature) -> bool:
        # a suspended/canceled account keeps its plan shape but loses live access
        if self.account.status in (AccountStatus.PAUSED, AccountStatus.CANCELED):
            return False
        return self.plan.has(feature)

    @property
    def within_included_tenants(self) -> bool:
        return self.account.tenant_count <= self.plan.included_tenants

    def can_add_tenant(self) -> bool:
        """True if another business may be added — always, when the plan supports
        multiple businesses (billed as overage past the bundle); otherwise only up
        to the single included business."""
        if self.plan.has(Feature.MULTI_TENANT):
            return True
        return self.account.tenant_count < self.plan.included_tenants

    def can_add_seat(self) -> bool:
        return self.account.seats_used < self.plan.included_seats

    @property
    def included_analyst_minutes(self) -> int:
        return self.plan.included_analyst_minutes
