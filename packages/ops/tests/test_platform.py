"""The platform admin surface: provisioning, entitlement-gated onboarding,
support impersonation (audited + role-gated)."""

from datetime import date

from rgnr8_forecast import CashPosition, ForecastInputs, Money
from rgnr8_billing import BillingService, EntitlementError, FakeBillingProvider, InMemoryAccountStore, Tier
from rgnr8_web import InMemoryAuditLog, InMemoryUserDirectory, Role, verify_jwt
from rgnr8_ops import Fleet, PlatformAdmin, PlatformError

SECRET = "platform-secret"
NOW = 1_760_000_000


def _dto() -> dict[str, object]:
    return {
        "contract": "forecast-inputs/1", "currency": "USD",
        "opening": {"as_of": "2026-08-31",
                    "available": {"minor": 9000000, "currency": "USD"},
                    "restricted": {"minor": 0, "currency": "USD"}, "verified": True},
    }


def _admin() -> tuple[PlatformAdmin, InMemoryUserDirectory, InMemoryAuditLog, BillingService]:
    billing = BillingService(InMemoryAccountStore(), FakeBillingProvider(), clock=lambda: NOW)
    fleet = Fleet(jwt_secret=SECRET, clock=lambda: NOW)
    directory = InMemoryUserDirectory()
    audit = InMemoryAuditLog()
    admin = PlatformAdmin(billing, fleet, directory, audit, clock=lambda: NOW)
    return admin, directory, audit, billing


def test_provision_then_onboard_seats_owner_and_audits() -> None:
    admin, directory, audit, billing = _admin()
    admin.provision_account("acct_1", "Northwind LLC", "billing@northwind.com",
                            Tier.CO_DELIVERY, operator="ops@rgnr8.co")
    bt = admin.onboard_business("acct_1", "northwind", "Northwind LLC", "owner@northwind.com",
                                _dto(), Money.from_decimal("25000.00"), "owner@northwind.com",
                                operator="ops@rgnr8.co")
    assert bt.tenant_id == "northwind"
    # owner seated in the client-side directory
    assert directory.membership("owner@northwind.com", "northwind").role is Role.OWNER
    # billing knows the tenant belongs to the account
    assert billing.account_for_tenant("northwind").id == "acct_1"
    actions = [e.action for e in audit.events(account_id="acct_1")]
    assert "account.provisioned" in actions and "tenant.onboarded" in actions


def test_onboarding_blocked_by_plan_entitlement() -> None:
    admin, _dir, _audit, _billing = _admin()
    admin.provision_account("acct_2", "Solo Co", "o@solo.com", Tier.SELF_SERVE,
                            operator="ops@rgnr8.co")  # 1 business only
    admin.onboard_business("acct_2", "solo-1", "Solo Co", "o@solo.com", _dto(),
                           Money.from_decimal("10000.00"), "o@solo.com", operator="ops@rgnr8.co")
    try:
        admin.onboard_business("acct_2", "solo-2", "Solo Co 2", "o@solo.com", _dto(),
                               Money.from_decimal("10000.00"), "o@solo.com", operator="ops@rgnr8.co")
        assert False, "expected EntitlementError"
    except EntitlementError:
        pass


def test_impersonation_requires_platform_role_and_is_audited() -> None:
    admin, directory, audit, _billing = _admin()
    admin.provision_account("acct_1", "N", "b@n.com", Tier.CO_DELIVERY, operator="ops@rgnr8.co")
    admin.onboard_business("acct_1", "northwind", "N", "o@n.com", _dto(),
                           Money.from_decimal("25000.00"), "o@n.com", operator="ops@rgnr8.co")

    # a non-staff user cannot impersonate
    try:
        admin.impersonate("random@user.com", "northwind")
        assert False, "expected PlatformError"
    except PlatformError:
        pass

    # grant support, then impersonate → a valid, tenant-scoped token
    admin.grant_platform_role("sam@rgnr8.co", Role.SUPPORT, operator="ops@rgnr8.co")
    token = admin.impersonate("sam@rgnr8.co", "northwind", ttl_seconds=900)
    claims = verify_jwt(token, SECRET, now=NOW)
    assert claims["tenant"] == "northwind"
    imp = [e for e in audit.events(tenant_id="northwind") if e.action == "support.impersonate"]
    assert imp and imp[0].actor == "sam@rgnr8.co"


def test_grant_rejects_non_platform_role() -> None:
    admin, _dir, _audit, _billing = _admin()
    try:
        admin.grant_platform_role("x@rgnr8.co", Role.OWNER, operator="ops@rgnr8.co")
        assert False, "expected PlatformError"
    except PlatformError:
        pass


def test_view_as_role_carries_the_role_claim_and_audits() -> None:
    admin, directory, audit, _billing = _admin()
    admin.provision_account("acct_1", "N", "b@n.com", Tier.CO_DELIVERY, operator="ops@rgnr8.co")
    admin.onboard_business("acct_1", "northwind", "N", "o@n.com", _dto(),
                           Money.from_decimal("25000.00"), "o@n.com", operator="ops@rgnr8.co")
    admin.grant_platform_role("sam@rgnr8.co", Role.SUPPORT, operator="ops@rgnr8.co")

    token = admin.view_as("sam@rgnr8.co", "northwind", Role.BOOKKEEPER, ttl_seconds=600)
    claims = verify_jwt(token, SECRET, now=NOW)
    assert claims["tenant"] == "northwind"
    assert claims["sub"] == "sam@rgnr8.co"          # staff identity, not the client
    assert claims["view_as"] == "bookkeeper"
    ev = [e for e in audit.events(tenant_id="northwind") if e.action == "support.view_as"]
    assert ev and "as=bookkeeper" in ev[0].detail


def test_view_as_rejects_platform_role_as_target() -> None:
    admin, _dir, _audit, _billing = _admin()
    admin.provision_account("acct_1", "N", "b@n.com", Tier.CO_DELIVERY, operator="ops@rgnr8.co")
    admin.onboard_business("acct_1", "northwind", "N", "o@n.com", _dto(),
                           Money.from_decimal("25000.00"), "o@n.com", operator="ops@rgnr8.co")
    admin.grant_platform_role("sam@rgnr8.co", Role.SUPPORT, operator="ops@rgnr8.co")
    try:
        admin.view_as("sam@rgnr8.co", "northwind", Role.OPERATOR)
        assert False, "expected PlatformError (platform role is not a client role)"
    except PlatformError:
        pass


def test_view_as_can_be_globally_disabled() -> None:
    admin, _dir, audit, _billing = _admin()
    admin.provision_account("acct_1", "N", "b@n.com", Tier.CO_DELIVERY, operator="ops@rgnr8.co")
    admin.onboard_business("acct_1", "northwind", "N", "o@n.com", _dto(),
                           Money.from_decimal("25000.00"), "o@n.com", operator="ops@rgnr8.co")
    admin.grant_platform_role("sam@rgnr8.co", Role.SUPPORT, operator="ops@rgnr8.co")

    admin.set_view_as_enabled(False, operator="ops@rgnr8.co")
    assert admin.view_as_enabled is False
    try:
        admin.view_as("sam@rgnr8.co", "northwind", Role.OWNER)
        assert False, "expected PlatformError when view-as disabled"
    except PlatformError:
        pass
    # a plain support session (no role) still works even when view-as is off
    admin.impersonate("sam@rgnr8.co", "northwind")
    assert any(e.action == "platform.view_as_toggled" for e in audit.events())
