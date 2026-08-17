"""The authenticated OPERATOR HTTP surface — RGNR8 staff only.

Proves the platform-role gate (non-staff rejected, support read-only, operator
can onboard), that a successful onboard registers the tenant everywhere (fleet +
billing + seated owner + audit), and that plan/entitlement and bad-JSON failures
come back as clean 4xx rather than a 500.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from rgnr8_forecast import Money
from rgnr8_billing import (
    BillingService,
    FakeBillingProvider,
    InMemoryAccountStore,
    Tier,
)
from rgnr8_web import InMemoryAuditLog, InMemoryUserDirectory, Request, Role
from rgnr8_ops import Fleet, OperatorApp, PlatformAdmin

SECRET = "operator-secret"
NOW = datetime(2025, 10, 9, 12, 0, tzinfo=timezone.utc)
NOW_EPOCH = int(NOW.timestamp())

# A forecast-inputs/1 DTO exactly like the overlay/migration engines emit.
DTO: dict[str, object] = {
    "contract": "forecast-inputs/1",
    "currency": "USD",
    "opening": {
        "as_of": "2026-08-31",
        "available": {"minor": 9000000, "currency": "USD"},
        "restricted": {"minor": 0, "currency": "USD"},
        "verified": True,
    },
}


def _app() -> tuple[OperatorApp, Fleet, PlatformAdmin, BillingService,
                     InMemoryUserDirectory, InMemoryAuditLog]:
    billing = BillingService(InMemoryAccountStore(), FakeBillingProvider(),
                             clock=lambda: NOW_EPOCH)
    fleet = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    directory = InMemoryUserDirectory()
    audit = InMemoryAuditLog()
    admin = PlatformAdmin(billing, fleet, directory, audit, clock=lambda: NOW_EPOCH)
    app = OperatorApp(fleet, billing, directory, audit, SECRET, clock=lambda: NOW)
    return app, fleet, admin, billing, directory, audit


def _staff_token(admin: PlatformAdmin, fleet: Fleet, email: str, role: Role) -> str:
    """Grant a platform role, then mint a session JWT whose `sub` is the staffer.

    A platform staffer isn't a tenant, so we mint against any registered tenant
    (the token's tenant claim is irrelevant to the operator surface — only `sub`
    and the platform role matter) with an explicit `subject=`."""
    admin.grant_platform_role(email, role, operator="root@rgnr8.co")
    any_tenant = next(iter(fleet.tenants))
    return fleet.mint_token(any_tenant, subject=email)


def _bootstrap_tenant(admin: PlatformAdmin) -> None:
    """Seed one account+tenant so there's a tenant to mint staff tokens against."""
    admin.provision_account("acct_seed", "Seed Co", "b@seed.com", Tier.CO_DELIVERY,
                            operator="root@rgnr8.co")
    admin.onboard_business("acct_seed", "seed", "Seed Co", "owner@seed.com", DTO,
                           Money.from_decimal("10000.00"), "owner@seed.com",
                           operator="root@rgnr8.co")


def test_health_needs_no_auth() -> None:
    app, *_ = _app()
    r = app.handle(Request("GET", "/health"))
    assert r.status == 200
    assert json.loads(r.body)["status"] == "ok"


def test_non_staff_token_is_rejected() -> None:
    app, fleet, admin, _billing, _dir, _audit = _app()
    _bootstrap_tenant(admin)
    # a validly-signed token whose subject holds NO platform role
    token = fleet.mint_token("seed", subject="owner@seed.com")
    r = app.handle(Request("GET", "/operator",
                           {"authorization": f"Bearer {token}"}))
    assert r.status == 403

    # and a missing/garbage token is a 401
    assert app.handle(Request("GET", "/operator")).status == 401
    assert app.handle(Request("GET", "/operator",
                              {"authorization": "Bearer not.a.jwt"})).status == 401


def test_support_can_view_but_cannot_onboard() -> None:
    app, fleet, admin, _billing, _dir, _audit = _app()
    _bootstrap_tenant(admin)
    token = _staff_token(admin, fleet, "sam@rgnr8.co", Role.SUPPORT)
    hdr = {"authorization": f"Bearer {token}"}

    # support may read the fleet console
    r = app.handle(Request("GET", "/operator", hdr))
    assert r.status == 200
    assert "Operator console" in r.body
    assert r.content_type.startswith("text/html")

    # but may NOT onboard — read-only
    body = json.dumps({
        "account_id": "acct_new", "tenant_id": "nw", "name": "Northwind",
        "recipient": "owner@nw.com", "dto": DTO, "minimum_cash": "25000.00",
        "owner_email": "owner@nw.com", "tier": "co_delivery",
    })
    r = app.handle(Request("POST", "/operator/onboard", hdr, body))
    assert r.status == 403
    assert "nw" not in fleet.tenants


def test_operator_can_onboard_business_everywhere() -> None:
    app, fleet, admin, billing, directory, audit = _app()
    _bootstrap_tenant(admin)
    token = _staff_token(admin, fleet, "ops@rgnr8.co", Role.OPERATOR)
    hdr = {"authorization": f"Bearer {token}"}

    body = json.dumps({
        "account_id": "acct_nw", "tenant_id": "northwind", "name": "Northwind LLC",
        "recipient": "owner@northwind.com", "dto": DTO, "minimum_cash": "25000.00",
        "owner_email": "owner@northwind.com", "tier": "co_delivery",
    })
    r = app.handle(Request("POST", "/operator/onboard", hdr, body))
    assert r.status == 201, r.body
    summary = json.loads(r.body)
    assert summary["tenant_id"] == "northwind"
    assert summary["cash_today"] == "90000.00"

    # registered in the fleet
    assert "northwind" in fleet.tenants
    assert fleet.tenant_source.resolve("northwind") is not None
    # attached in billing under the account
    acct = billing.account_for_tenant("northwind")
    assert acct is not None and acct.id == "acct_nw"
    # owner seated in the client-side directory
    seat = directory.membership("owner@northwind.com", "northwind")
    assert seat is not None and seat.role is Role.OWNER
    # audited
    actions = [e.action for e in audit.events(account_id="acct_nw")]
    assert "account.provisioned" in actions and "tenant.onboarded" in actions

    # and the audit route surfaces it
    ar = app.handle(Request("GET", "/operator/audit?account=acct_nw", hdr))
    assert ar.status == 200
    assert any(e["action"] == "tenant.onboarded"
               for e in json.loads(ar.body)["events"])


def test_onboarding_beyond_entitlement_is_4xx_not_500() -> None:
    app, fleet, admin, _billing, _dir, _audit = _app()
    _bootstrap_tenant(admin)
    token = _staff_token(admin, fleet, "ops@rgnr8.co", Role.OPERATOR)
    hdr = {"authorization": f"Bearer {token}"}

    # a self-serve account bundles exactly one business
    first = json.dumps({
        "account_id": "acct_solo", "tenant_id": "solo-1", "name": "Solo Co",
        "recipient": "o@solo.com", "dto": DTO, "minimum_cash": "5000.00",
        "owner_email": "o@solo.com", "tier": "self_serve",
    })
    assert app.handle(Request("POST", "/operator/onboard", hdr, first)).status == 201

    # a second business on the same account exceeds the plan → clean 4xx
    second = json.dumps({
        "account_id": "acct_solo", "tenant_id": "solo-2", "name": "Solo Co 2",
        "recipient": "o@solo.com", "dto": DTO, "minimum_cash": "5000.00",
        "owner_email": "o@solo.com",
    })
    r = app.handle(Request("POST", "/operator/onboard", hdr, second))
    assert 400 <= r.status < 500
    assert r.status != 500
    assert "solo-2" not in fleet.tenants


def test_bad_json_body_is_400() -> None:
    app, fleet, admin, _billing, _dir, _audit = _app()
    _bootstrap_tenant(admin)
    token = _staff_token(admin, fleet, "ops@rgnr8.co", Role.OPERATOR)
    hdr = {"authorization": f"Bearer {token}"}
    r = app.handle(Request("POST", "/operator/onboard", hdr, "{not valid json"))
    assert r.status == 400
    assert "JSON" in r.body


def test_operator_can_launch_view_as_and_it_is_audited() -> None:
    from rgnr8_web import verify_jwt
    app, fleet, admin, _billing, directory, audit = _app()
    _bootstrap_tenant(admin)
    token = _staff_token(admin, fleet, "dev@rgnr8.co", Role.OPERATOR)
    hdr = {"authorization": f"Bearer {token}"}

    r = app.handle(Request("POST", "/operator/view-as", hdr,
                           json.dumps({"tenant_id": "seed", "role": "viewer", "ttl_seconds": 600})))
    assert r.status == 200
    body = json.loads(r.body)
    assert body["view_as"] == "viewer" and body["tenant_id"] == "seed"
    claims = verify_jwt(body["token"], SECRET, now=NOW_EPOCH)
    assert claims["sub"] == "dev@rgnr8.co" and claims["view_as"] == "viewer"
    assert any(e.action == "support.view_as" for e in audit.events(tenant_id="seed"))

    # an unknown role is a clean 400
    bad = app.handle(Request("POST", "/operator/view-as", hdr,
                             json.dumps({"tenant_id": "seed", "role": "wizard"})))
    assert bad.status == 400


def test_view_as_toggle_is_operator_only_and_disables_role_view() -> None:
    app, fleet, admin, _billing, _dir, _audit = _app()
    _bootstrap_tenant(admin)
    op = {"authorization": f"Bearer {_staff_token(admin, fleet, 'dev@rgnr8.co', Role.OPERATOR)}"}
    sup = {"authorization": f"Bearer {_staff_token(admin, fleet, 'sam@rgnr8.co', Role.SUPPORT)}"}

    # support cannot flip the global switch
    assert app.handle(Request("POST", "/operator/view-as/toggle", sup,
                              json.dumps({"enabled": False}))).status == 403
    # operator disables view-as
    off = app.handle(Request("POST", "/operator/view-as/toggle", op,
                             json.dumps({"enabled": False})))
    assert off.status == 200 and json.loads(off.body)["view_as_enabled"] is False

    # now a role-scoped view-as is refused...
    denied = app.handle(Request("POST", "/operator/view-as", op,
                                json.dumps({"tenant_id": "seed", "role": "owner"})))
    assert denied.status == 403
    # ...but a plain support session (no role) still works
    plain = app.handle(Request("POST", "/operator/view-as", op,
                               json.dumps({"tenant_id": "seed"})))
    assert plain.status == 200 and json.loads(plain.body)["view_as"] is None
