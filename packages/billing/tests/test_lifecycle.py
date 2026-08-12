"""Self-serve plan lifecycle: upgrade/downgrade guardrails, cancel/reactivate,
pause/resume, and the customer billing-portal seam."""

import dataclasses
import json

from rgnr8_billing import (
    AccountStatus,
    BillingService,
    EntitlementError,
    FakeBillingPortalProvider,
    FakeBillingProvider,
    Feature,
    HttpResponse,
    InMemoryAccountStore,
    PlanChange,
    PlanChangeKind,
    StripeBillingPortalProvider,
    Tier,
)

NOW = 1_760_000_000
_DAY = 86_400


class _Clock:
    """A movable deterministic clock (no wall-clock reads)."""

    def __init__(self, t: int = NOW) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def _svc(
    provider: FakeBillingProvider | None = None,
    *,
    clock: _Clock | None = None,
    portal: FakeBillingPortalProvider | None = None,
    on_plan_change: object = None,
    grace_days: int = 30,
) -> BillingService:
    return BillingService(
        InMemoryAccountStore(),
        provider or FakeBillingProvider(),
        clock=clock or (lambda: NOW),
        portal=portal,
        on_plan_change=on_plan_change,  # type: ignore[arg-type]
        reactivate_grace_days=grace_days,
    )


# --- upgrade / downgrade guardrails -----------------------------------------

def test_upgrade_is_allowed_and_recorded() -> None:
    changes: list[PlanChange] = []
    p = FakeBillingProvider()
    svc = _svc(p, on_plan_change=changes.append)
    svc.create_account("a", "Acme", "o@a.com", Tier.SELF_SERVE)
    svc.attach_tenant("a", "biz-1")

    a = svc.change_plan("a", Tier.ASSISTED)  # 199 -> 499
    assert a.tier == Tier.ASSISTED
    # processor subscription kept in step
    assert p.subscriptions["a"].startswith("sub_fake_a")
    # recorded to history + fanned out to the seam, classified as an upgrade
    hist = svc.plan_changes("a")
    assert len(hist) == 1 and hist[0].kind is PlanChangeKind.UPGRADE
    assert changes == hist
    assert hist[0].from_tier == Tier.SELF_SERVE and hist[0].to_tier == Tier.ASSISTED


def test_downgrade_blocked_when_too_many_businesses() -> None:
    svc = _svc()
    svc.create_account("firm", "F", "o@f.com", Tier.CO_DELIVERY)  # multi-tenant
    for i in range(3):
        svc.attach_tenant("firm", f"client-{i}")  # 3 businesses
    # self-serve lacks MULTI_TENANT and includes only 1 business → would strand 2
    try:
        svc.change_plan("firm", Tier.SELF_SERVE)
        assert False, "expected EntitlementError"
    except EntitlementError as e:
        assert "business" in str(e)
    # unchanged
    a = svc.get_account("firm")
    assert a is not None and a.tier == Tier.CO_DELIVERY


def test_downgrade_blocked_when_too_many_seats() -> None:
    store = InMemoryAccountStore()
    svc = BillingService(store, FakeBillingProvider(), clock=lambda: NOW)
    svc.create_account("a", "Acme", "o@a.com", Tier.CO_DELIVERY)  # 10 seats
    a = svc.get_account("a")
    assert a is not None
    store.save_account(dataclasses.replace(a, seats_used=7))  # 7 seats in use
    # assisted includes only 5 seats → would strand 2
    try:
        svc.change_plan("a", Tier.ASSISTED)
        assert False, "expected EntitlementError"
    except EntitlementError as e:
        assert "seat" in str(e)


def test_downgrade_allowed_within_limits() -> None:
    changes: list[PlanChange] = []
    svc = _svc(on_plan_change=changes.append)
    svc.create_account("a", "Acme", "o@a.com", Tier.ASSISTED)  # 499
    svc.attach_tenant("a", "biz-1")  # 1 business — fits self-serve's 1
    a = svc.change_plan("a", Tier.SELF_SERVE)  # 499 -> 199
    assert a.tier == Tier.SELF_SERVE
    assert changes and changes[0].kind is PlanChangeKind.DOWNGRADE


# --- cancel / reactivate / pause / resume -----------------------------------

def test_cancel_sets_status_and_fails_entitlements_closed() -> None:
    p = FakeBillingProvider()
    svc = _svc(p)
    svc.create_account("a", "Acme", "o@a.com", Tier.CO_DELIVERY)
    svc.activate("a")
    svc.attach_tenant("a", "biz-1")

    a = svc.cancel("a", at_period_end=False)
    assert a.status == AccountStatus.CANCELED
    # provider cancel hook fired with the flag
    assert p.canceled == [("a", False)]
    # entitlements now fail closed
    assert svc.feature_enabled("a", Feature.MULTI_TENANT) is False
    try:
        svc.attach_tenant("a", "biz-2")
        assert False, "canceled account must be blocked"
    except Exception as e:
        assert "canceled" in str(e)


def test_reactivate_restores_within_grace() -> None:
    p = FakeBillingProvider()
    svc = _svc(p)
    svc.create_account("a", "Acme", "o@a.com", Tier.ASSISTED)
    svc.activate("a")
    svc.cancel("a")
    a = svc.reactivate("a")
    assert a.status == AccountStatus.ACTIVE
    assert svc.feature_enabled("a", Feature.CASH) is True  # entitlements live again


def test_reactivate_refused_after_grace_expires() -> None:
    clock = _Clock(NOW)
    svc = _svc(clock=clock, grace_days=30)
    svc.create_account("a", "Acme", "o@a.com", Tier.ASSISTED)
    svc.activate("a")
    svc.cancel("a")
    clock.t = NOW + 31 * _DAY  # past the grace window
    try:
        svc.reactivate("a")
        assert False, "expected grace window to be closed"
    except Exception as e:
        assert "expired" in str(e)


def test_pause_and_resume() -> None:
    svc = _svc()
    svc.create_account("a", "Acme", "o@a.com", Tier.ASSISTED)
    svc.activate("a")
    a = svc.pause("a")
    assert a.status == AccountStatus.PAUSED
    assert svc.feature_enabled("a", Feature.CASH) is False  # paused fails closed
    a = svc.resume("a")
    assert a.status == AccountStatus.ACTIVE
    assert svc.feature_enabled("a", Feature.CASH) is True


# --- billing portal seam -----------------------------------------------------

def test_stripe_portal_provider_posts_correct_shape() -> None:
    class RecordingHttp:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def post_form(
            self, url: str, data: dict[str, str], headers: dict[str, str],
        ) -> HttpResponse:
            assert headers["Authorization"].startswith("Bearer ")
            self.calls.append((url, data))
            return HttpResponse(200, json.dumps({"url": "https://billing/live"}))

    p = FakeBillingProvider()
    svc = _svc(p)
    svc.create_account("a", "Acme", "o@a.com", Tier.ASSISTED)
    acct = svc.get_account("a")
    assert acct is not None and acct.stripe_customer_id == "cus_fake_a"

    http = RecordingHttp()
    portal = StripeBillingPortalProvider(http, secret_key="sk_test_x")
    url = portal.create_portal_session(acct, "https://app.rgnr8.com/account")

    # url read back from the body, no network
    assert url == "https://billing/live"
    posted_url, data = http.calls[0]
    assert posted_url.endswith("/v1/billing_portal/sessions")
    assert data["customer"] == "cus_fake_a"
    assert data["return_url"] == "https://app.rgnr8.com/account"


def test_fake_portal_returns_stable_url_and_service_delegates() -> None:
    portal = FakeBillingPortalProvider()
    svc = _svc(portal=portal)
    svc.create_account("a", "Acme", "o@a.com", Tier.ASSISTED)

    url1 = svc.create_portal_session("a", "https://ret")
    url2 = svc.create_portal_session("a", "https://ret")
    assert url1 == url2 == "https://billing.test/portal/cus_fake_a"  # deterministic
    assert portal.calls[-1] == ("cus_fake_a", "https://ret")
