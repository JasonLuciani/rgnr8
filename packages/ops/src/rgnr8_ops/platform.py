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

from rgnr8_billing import Account, BillingService, Tier
from rgnr8_forecast import Money
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
        view_as_enabled: bool = True,
    ) -> None:
        self._billing = billing
        self._fleet = fleet
        self._dir = directory
        self._audit = audit
        self._clock = clock if clock is not None else (lambda: int(time.time()))
        # Master switch for the developer "view-as-role" capability. Left on for
        # build/support today; flip it off (globally) once the product is in
        # customers' hands and staff shouldn't be able to see into live books.
        self._view_as_enabled = view_as_enabled

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

    # --- a client's users + roles (operator management of any tenant) --------
    def list_members(self, tenant_id: str) -> list[dict[str, object]]:
        """Every member of a client business with their role — for the operator
        console's per-tenant user management."""
        rows: list[dict[str, object]] = []
        for m in self._dir.members(tenant_id):
            u = self._dir.get_user(m.user_id)
            rows.append({
                "user_id": m.user_id,
                "email": u.email if u is not None else None,
                "name": u.name if u is not None else None,
                "role": m.role.value,
            })
        return rows

    def set_member_role(
        self, tenant_id: str, email: str, role: Role | None, *, operator: str,
        name: str = "",
    ) -> None:
        """Add or change a client member's role, or remove them (``role=None``).
        Only *client* roles may be assigned here (a platform role is granted with
        ``grant_platform_role``, not seated in a tenant). Audited."""
        if role is not None and role.is_platform:
            raise PlatformError(f"{role.value} is a platform role — use grant_platform_role")
        user = self._dir.find_by_email(email)
        if role is None:
            if user is not None:
                self._dir.remove_membership(user.id, tenant_id)
            self._record(operator, "membership.removed", tenant_id=tenant_id, target=email)
            return
        if user is None:
            user = User(id=email, email=email, name=name)
            self._dir.upsert_user(user)
        elif name and user.name != name:
            self._dir.upsert_user(User(id=user.id, email=user.email, name=name))
        self._dir.set_membership(user.id, tenant_id, role)
        self._record(operator, "membership.set", tenant_id=tenant_id, target=email, detail=role.value)

    # --- our own staff -------------------------------------------------------
    def grant_platform_role(self, user_id: str, role: Role | None, *, operator: str) -> None:
        if role is not None and not role.is_platform:
            raise PlatformError(f"{role.value} is not a platform role")
        self._dir.set_platform_role(user_id, role)
        self._record(operator, "platform_role.granted", target=user_id,
                     detail=role.value if role is not None else "revoked")

    # --- support impersonation / view-as (audited, time-boxed) ---------------
    def impersonate(self, support_user: str, tenant_id: str, *, ttl_seconds: int = 900) -> str:
        """Mint a short-lived token to act as a client for support/debugging. Only
        RGNR8 staff (a platform role) may do this, and every use is logged."""
        return self.view_as(support_user, tenant_id, view_as=None, ttl_seconds=ttl_seconds)

    def view_as(
        self, support_user: str, tenant_id: str, view_as: Role | None,
        *, ttl_seconds: int = 900,
    ) -> str:
        """Mint a short-lived token that lets RGNR8 staff view a client's account
        **exactly as one of its roles sees it** (owner / bookkeeper / accountant /
        viewer). `view_as=None` is a plain support session (the operator's own
        cross-tenant sight). Only a platform role may do this, it is time-boxed,
        every use is audited, and the whole capability can be turned off with
        `set_view_as_enabled(False)` once we're live.

        The token's subject is the SUPPORT user, so every action taken while
        viewing-as is attributable to staff — you cannot launder a change through
        a client's identity."""
        role = self._dir.platform_role(support_user)
        if role is None or not role.is_platform:
            raise PlatformError("view-as requires a platform role")
        if view_as is not None:
            if not self._view_as_enabled:
                raise PlatformError("developer view-as is disabled")
            if view_as.is_platform:
                raise PlatformError(f"{view_as.value} is a platform role, not a client role to view as")
        if tenant_id not in self._fleet.tenants:
            raise PlatformError(f"unknown tenant {tenant_id}")
        token = self._fleet.mint_token(
            tenant_id, subject=support_user, ttl_seconds=ttl_seconds,
            view_as=view_as.value if view_as is not None else None,
        )
        action = "support.view_as" if view_as is not None else "support.impersonate"
        detail = f"ttl={ttl_seconds}s" + (f" as={view_as.value}" if view_as is not None else "")
        self._record(support_user, action, tenant_id=tenant_id, detail=detail)
        return token

    def set_view_as_enabled(self, enabled: bool, *, operator: str) -> None:
        """Globally enable/disable the developer view-as capability (the
        "turn it off later" switch). Audited."""
        self._view_as_enabled = enabled
        self._record(operator, "platform.view_as_toggled", detail="enabled" if enabled else "disabled")

    @property
    def view_as_enabled(self) -> bool:
        return self._view_as_enabled

    def _record(self, actor: str, action: str, *, tenant_id: str = "", account_id: str = "",
                target: str = "", detail: str = "") -> None:
        if self._audit is not None:
            self._audit.record(actor, action, self._clock(), tenant_id=tenant_id,
                               account_id=account_id, target=target, detail=detail)
