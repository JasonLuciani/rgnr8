"""The platform (RGNR8-staff) admin surface — our side of user + account management.

Where `rgnr8_web.rbac` is *client-side* authorization (a business's own members and
roles) and `rgnr8_billing` owns the account, `PlatformAdmin` is the operator's
control plane that ties them together: provision a billing account, onboard a
business under it (entitlement-checked against the plan), set a plan, grant our
own staff their platform roles, and — audited every time — mint a scoped
impersonation token so support can act as a client to debug. Every mutating action
writes to the audit log; impersonation requires a platform role and is time-boxed.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from rgnr8_forecast import Money
from rgnr8_billing import Account, BillingService, Tier
from rgnr8_web import AuditSink, Role, User, UserDirectory

from .fleet import BetaTenant, Fleet


class PlatformError(Exception):
    """A platform action that isn't permitted (missing platform role, etc.)."""


class PlatformAdmin:
    def __init__(
        self,
        billing: BillingService,
        fleet: Fleet,
        directory: UserDirectory,
        audit: AuditSink | None = None,
        *,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._billing = billing
        self._fleet = fleet
        self._dir = directory
        self._audit = audit
        self._clock = clock if clock is not None else (lambda: int(time.time()))

    # --- provisioning --------------------------------------------------------
    def provision_account(
        self, account_id: str, name: str, billing_email: str, tier: Tier,
        *, operator: str, trial_days: int = 14,
    ) -> Account:
        acct = self._billing.create_account(account_id, name, billing_email, tier,
                                            trial_days=trial_days)
        self._record(operator, "account.provisioned", account_id=account_id, target=name,
                     detail=tier.value)
        return acct

    def onboard_business(
        self, account_id: str, tenant_id: str, name: str, recipient: str,
        dto: dict[str, object] | str, minimum_cash: Money, owner_email: str,
        *, operator: str,
    ) -> BetaTenant:
        """Attach a business to the account (entitlement-checked in billing), onboard
        it into the fleet from a ``forecast-inputs/1`` DTO, and seat its owner."""
        self._billing.attach_tenant(account_id, tenant_id)  # raises EntitlementError if the plan forbids
        bt = self._fleet.onboard_from_dto(tenant_id, name, recipient, dto, minimum_cash)
        user = self._dir.find_by_email(owner_email) or User(id=owner_email, email=owner_email)
        self._dir.upsert_user(user)
        self._dir.set_membership(user.id, tenant_id, Role.OWNER)
        self._record(operator, "tenant.onboarded", account_id=account_id, tenant_id=tenant_id,
                     target=owner_email)
        return bt

    def set_plan(self, account_id: str, tier: Tier, *, operator: str) -> Account:
        acct = self._billing.change_tier(account_id, tier)
        self._record(operator, "account.plan_changed", account_id=account_id, detail=tier.value)
        return acct

    # --- our own staff -------------------------------------------------------
    def grant_platform_role(self, user_id: str, role: Role | None, *, operator: str) -> None:
        if role is not None and not role.is_platform:
            raise PlatformError(f"{role.value} is not a platform role")
        self._dir.set_platform_role(user_id, role)
        self._record(operator, "platform_role.granted", target=user_id,
                     detail=role.value if role is not None else "revoked")

    # --- support impersonation (audited, time-boxed) -------------------------
    def impersonate(self, support_user: str, tenant_id: str, *, ttl_seconds: int = 900) -> str:
        """Mint a short-lived token to act as a client for support/debugging. Only
        RGNR8 staff (a platform role) may do this, and every use is logged."""
        role = self._dir.platform_role(support_user)
        if role is None or not role.is_platform:
            raise PlatformError("impersonation requires a platform role")
        if tenant_id not in self._fleet.tenants:
            raise PlatformError(f"unknown tenant {tenant_id}")
        # the impersonation token carries the SUPPORT user as its subject, so every
        # action taken while acting-as is attributable to them (not the tenant).
        token = self._fleet.mint_token(tenant_id, subject=support_user, ttl_seconds=ttl_seconds)
        self._record(support_user, "support.impersonate", tenant_id=tenant_id,
                     detail=f"ttl={ttl_seconds}s")
        return token

    def _record(self, actor: str, action: str, *, tenant_id: str = "", account_id: str = "",
                target: str = "", detail: str = "") -> None:
        if self._audit is not None:
            self._audit.record(actor, action, self._clock(), tenant_id=tenant_id,
                               account_id=account_id, target=target, detail=detail)
