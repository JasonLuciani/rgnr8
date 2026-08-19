from datetime import datetime
from zoneinfo import ZoneInfo

from factory import inputs, tenant_source, usd
from rgnr8_briefing import RecordingDeliverer, Schedule, Subscription
from rgnr8_runtime import DeliveryRuntime, InMemorySubscriptionStore, InMemoryTenantSource

MT = ZoneInfo("America/Denver")
MON_8 = Schedule(weekday=0, hour=8, minute=0, timezone="America/Denver")
WED_NOON = datetime(2026, 8, 5, 12, 0, tzinfo=MT)  # a Wednesday; last Mon fire was 08-03 08:00


def runtime_with(subs: InMemorySubscriptionStore, deliverer: RecordingDeliverer) -> DeliveryRuntime:
    return DeliveryRuntime(subs, tenant_source(), deliverer)


def test_tick_delivers_a_due_subscription_and_advances_the_cursor() -> None:
    store = InMemorySubscriptionStore()
    store.save(Subscription("bright", "owner@bright.com", MON_8, last_sent=None))
    deliverer = RecordingDeliverer()
    rt = runtime_with(store, deliverer)

    outcomes = rt.tick(WED_NOON, at="2026-08-05T18:00:00Z")
    assert len(deliverer.sent) == 1
    assert outcomes[0].fired is True
    # cursor advanced to this week's Monday fire and persisted
    saved = store.list()[0]
    assert saved.last_sent == datetime(2026, 8, 3, 8, 0, tzinfo=MT)


def test_tick_is_idempotent_across_a_simulated_restart() -> None:
    store = InMemorySubscriptionStore()
    store.save(Subscription("bright", "owner@bright.com", MON_8, last_sent=None))
    d1 = RecordingDeliverer()
    runtime_with(store, d1).tick(WED_NOON)
    assert len(d1.sent) == 1

    # a fresh runtime + fresh deliverer against the SAME store (process restart)
    d2 = RecordingDeliverer()
    runtime_with(store, d2).tick(WED_NOON)
    assert len(d2.sent) == 0  # already sent for this week's fire


def test_not_due_subscription_is_not_delivered() -> None:
    store = InMemorySubscriptionStore()
    # already sent for this week's fire
    store.save(Subscription("bright", "owner@bright.com", MON_8, last_sent=datetime(2026, 8, 3, 8, 0, tzinfo=MT)))
    deliverer = RecordingDeliverer()
    outcomes = runtime_with(store, deliverer).tick(WED_NOON)
    assert len(deliverer.sent) == 0
    assert outcomes[0].fired is False


def test_unknown_tenant_is_skipped_and_cursor_not_advanced() -> None:
    store = InMemorySubscriptionStore()
    store.save(Subscription("ghost", "owner@ghost.com", MON_8, last_sent=None))
    deliverer = RecordingDeliverer()
    # tenant source has bright/acme, not ghost
    rt = DeliveryRuntime(store, tenant_source(), deliverer)
    outcomes = rt.tick(WED_NOON)
    assert len(deliverer.sent) == 0
    assert outcomes[0].fired is False
    assert outcomes[0].skipped_reason == "no_envelope"
    assert store.list()[0].last_sent is None  # not advanced — retries next tick


def test_two_tenants_each_get_their_own_delivery() -> None:
    store = InMemorySubscriptionStore()
    store.save(Subscription("bright", "o@bright.com", MON_8, last_sent=None))
    store.save(Subscription("acme", "o@acme.com", MON_8, last_sent=None))
    deliverer = RecordingDeliverer()
    outcomes = runtime_with(store, deliverer).tick(WED_NOON)
    assert len(deliverer.sent) == 2
    assert all(o.fired for o in outcomes)


def test_tenant_from_dto_source_delivers() -> None:
    from rgnr8_forecast.io import to_dto

    src = InMemoryTenantSource()
    src.add_from_dto("bright", "Bright", to_dto(inputs("42000.00")), minimum_cash=usd("9000.00"))
    store = InMemorySubscriptionStore()
    store.save(Subscription("bright", "o@bright.com", MON_8, last_sent=None))
    deliverer = RecordingDeliverer()
    outcomes = DeliveryRuntime(store, src, deliverer).tick(WED_NOON)
    assert len(deliverer.sent) == 1
    assert outcomes[0].fired is True
