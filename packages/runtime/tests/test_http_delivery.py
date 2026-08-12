"""End-to-end: the delivery runtime, wired to the real HTTP-backed email
transport (over a fake HTTP client), actually posts a briefing when a
subscription is due — subscription → forecast → validated briefing → envelope →
ProviderDeliverer → HttpEmailTransport → HTTP POST."""

from datetime import datetime
from zoneinfo import ZoneInfo

from rgnr8_briefing import (
    FakeHttpClient,
    HttpEmailTransport,
    ProviderDeliverer,
    Schedule,
    Subscription,
)
from rgnr8_runtime import DeliveryRuntime, InMemorySubscriptionStore
from factory import tenant_source

MT = ZoneInfo("America/Denver")
MON_8 = Schedule(weekday=0, hour=8, minute=0, timezone="America/Denver")
WED = datetime(2026, 8, 5, 12, 0, tzinfo=MT)


def test_runtime_delivers_over_the_real_http_email_transport() -> None:
    http = FakeHttpClient(status=202, headers={"X-Message-Id": "sg-999"})
    deliverer = ProviderDeliverer(HttpEmailTransport(http, api_key="k", from_email="cfo@rgnr8.com"))

    store = InMemorySubscriptionStore()
    store.save(Subscription("bright", "owner@bright.com", MON_8, last_sent=None))
    rt = DeliveryRuntime(store, tenant_source(), deliverer)

    outcomes = rt.tick(WED, at="2026-08-05T18:00:00Z")
    assert outcomes[0].fired is True

    # exactly one HTTP POST to SendGrid, addressed to the owner
    assert len(http.calls) == 1
    payload = http.calls[0]["payload"]
    assert payload["personalizations"][0]["to"][0]["email"] == "owner@bright.com"  # type: ignore[index]
    assert "steady" in str(payload["subject"]).lower() or "cash" in str(payload["subject"]).lower()  # type: ignore[index]

    # re-tick same week is idempotent — no second POST
    rt.tick(WED, at="2026-08-05T18:05:00Z")
    assert len(http.calls) == 1
