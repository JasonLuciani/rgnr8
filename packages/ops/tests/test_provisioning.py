"""The composed signup→checkout→provisioning→metering growth seam."""

from rgnr8_analytics import EventName, EventTracker, InMemoryEventSink
from rgnr8_billing import Tier
from rgnr8_ops import Fleet, build_provisioning
from factory import steady_tenant

SECRET = "prov-secret"


def _completed_event(account_id: str, subscription_id: str) -> dict[str, object]:
    return {
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_test",
            "client_reference_id": account_id,
            "customer": "cus_test",
            "subscription": subscription_id,
            "metadata": {"account_id": account_id, "tier": Tier.SELF_SERVE.value},
        }},
    }


def test_signup_then_checkout_provisions_and_emits_funnel() -> None:
    sink = InMemoryEventSink()
    tracker = EventTracker(clock=lambda: 1000.0, sink=sink)
    prov = build_provisioning(tracker=tracker)

    fleet = Fleet(jwt_secret=SECRET, clock=lambda: 1)
    fleet.onboard(steady_tenant())  # owner@acme.com / tenant "acme"
    prov.bind_fleet(fleet)

    acct, session = prov.signup.start_checkout("owner@acme.com", Tier.SELF_SERVE)
    assert session.url  # a hosted checkout session was opened

    provisioned = prov.signup.complete_checkout(_completed_event(acct.id, "sub_1"))
    assert provisioned is not None and provisioned.id == acct.id
    # the funnel fired on provisioning
    assert sink.by_name(EventName.ACCOUNT_ACTIVATED)
    assert sink.by_name(EventName.TENANT_ONBOARDED)
    # the account↔tenant link is recorded so metering resolves the tenant
    assert prov.account_of.get("acme") == acct.id

    # replaying the same completed event is idempotent (no second activation)
    before = len(sink.by_name(EventName.ACCOUNT_ACTIVATED))
    prov.signup.complete_checkout(_completed_event(acct.id, "sub_1"))
    assert len(sink.by_name(EventName.ACCOUNT_ACTIVATED)) == before


def test_usage_recorder_is_safe_for_unmapped_tenant() -> None:
    prov = build_provisioning()
    # unmapped tenant / unknown kind → no-op, never raises
    prov.usage_recorder("nobody", "briefing_sent", 1)
    prov.usage_recorder("nobody", "not_a_kind", 1)
