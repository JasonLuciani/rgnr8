from factory import breach_forecast, healthy_forecast
from rgnr8_briefing import (
    Channel,
    FakeEmailTransport,
    FakePushTransport,
    ProviderDeliverer,
    build_briefing,
    build_envelope,
)


def _normal_env():  # type: ignore[no-untyped-def]
    return build_envelope(build_briefing(healthy_forecast()), "owner@acme.com", tenant_id="acme")


def _urgent_env():  # type: ignore[no-untyped-def]
    return build_envelope(build_briefing(breach_forecast()), "owner@acme.com", tenant_id="acme")


def test_normal_envelope_goes_email_only() -> None:
    email = FakeEmailTransport()
    push = FakePushTransport()
    deliverer = ProviderDeliverer(email, push)
    receipt = deliverer.send(_normal_env(), at="2026-08-10T08:00:00Z")
    assert receipt.channels == (Channel.EMAIL,)
    assert receipt.status.startswith("SENT")
    assert len(email.sent) == 1
    assert len(push.sent) == 0
    assert email.sent[0]["recipient"] == "owner@acme.com"


def test_urgent_envelope_sends_email_and_push() -> None:
    email = FakeEmailTransport()
    push = FakePushTransport()
    deliverer = ProviderDeliverer(email, push)
    env = _urgent_env()
    assert Channel.PUSH in env.channels
    receipt = deliverer.send(env, at="2026-08-10T08:00:00Z")
    assert set(receipt.channels) == {Channel.EMAIL, Channel.PUSH}
    assert receipt.status.startswith("SENT")
    assert len(email.sent) == 1 and len(push.sent) == 1
    # push body carries the recommended-action label, not the whole briefing
    assert push.sent[0]["body"]


def test_push_requested_but_no_transport_is_partial_not_crash() -> None:
    email = FakeEmailTransport()
    deliverer = ProviderDeliverer(email)  # no push transport
    receipt = deliverer.send(_urgent_env(), at="2026-08-10T08:00:00Z")
    assert receipt.channels == (Channel.EMAIL,)  # email still went out
    assert receipt.status.startswith("PARTIAL")
    assert "no push transport" in receipt.status


def test_email_failure_yields_failed_receipt() -> None:
    email = FakeEmailTransport(fail=True)
    deliverer = ProviderDeliverer(email)
    receipt = deliverer.send(_normal_env(), at="2026-08-10T08:00:00Z")
    assert receipt.channels == ()
    assert receipt.status.startswith("FAILED")
    assert "simulated email failure" in receipt.status


def test_partial_when_email_ok_but_push_fails() -> None:
    email = FakeEmailTransport()
    push = FakePushTransport(fail=True)
    deliverer = ProviderDeliverer(email, push)
    receipt = deliverer.send(_urgent_env(), at="2026-08-10T08:00:00Z")
    assert receipt.channels == (Channel.EMAIL,)
    assert receipt.status.startswith("PARTIAL")
    assert "simulated push failure" in receipt.status
