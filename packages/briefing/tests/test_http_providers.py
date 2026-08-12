import json

from rgnr8_briefing import (
    Channel,
    FakeHttpClient,
    HttpEmailTransport,
    HttpPushTransport,
    ProviderDeliverer,
    TransportError,
    build_briefing,
    build_envelope,
)
from factory import breach_forecast, healthy_forecast


def _email(http: FakeHttpClient) -> HttpEmailTransport:
    return HttpEmailTransport(http, api_key="sg-key", from_email="cfo@rgnr8.com")


def test_email_transport_posts_sendgrid_payload_and_returns_message_id() -> None:
    http = FakeHttpClient(status=202, headers={"X-Message-Id": "sg-777"})
    mid = _email(http).send_email("owner@acme.com", "Your cash", "text body", "<p>html</p>")
    assert mid == "sg-777"
    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["url"] == "https://api.sendgrid.com/v3/mail/send"
    assert call["headers"]["Authorization"] == "Bearer sg-key"  # type: ignore[index]
    payload = call["payload"]
    assert payload["personalizations"][0]["to"][0]["email"] == "owner@acme.com"  # type: ignore[index]
    assert payload["from"]["email"] == "cfo@rgnr8.com"  # type: ignore[index]
    assert payload["subject"] == "Your cash"  # type: ignore[index]
    types = [c["type"] for c in payload["content"]]  # type: ignore[index]
    assert types == ["text/plain", "text/html"]


def test_email_transport_synthesizes_id_when_header_absent() -> None:
    http = FakeHttpClient(status=202, headers={})
    mid = _email(http).send_email("o@a.com", "s", "t", "h")
    assert mid.startswith("accepted:")


def test_email_transport_raises_on_non_2xx() -> None:
    http = FakeHttpClient(status=401, body="unauthorized")
    try:
        _email(http).send_email("o@a.com", "s", "t", "h")
    except TransportError as e:
        assert "401" in str(e)
    else:
        raise AssertionError("expected TransportError")


def test_email_transport_raises_on_connection_failure() -> None:
    http = FakeHttpClient(raise_error=True)
    try:
        _email(http).send_email("o@a.com", "s", "t", "h")
    except TransportError:
        pass
    else:
        raise AssertionError("expected TransportError")


def test_push_transport_posts_and_reads_id_from_body() -> None:
    http = FakeHttpClient(status=200, body=json.dumps({"id": "push-42"}))
    t = HttpPushTransport(http, api_key="fcm-key", api_url="https://push.example/send")
    assert t.send_push("device-token", "Action needed", "Cash dips below floor") == "push-42"
    call = http.calls[0]
    assert call["url"] == "https://push.example/send"
    assert call["payload"]["to"] == "device-token"  # type: ignore[index]


def test_provider_deliverer_sends_a_real_urgent_envelope_over_http() -> None:
    email_http = FakeHttpClient(status=202, headers={"X-Message-Id": "sg-1"})
    push_http = FakeHttpClient(status=200, body=json.dumps({"id": "p-1"}))
    deliverer = ProviderDeliverer(
        HttpEmailTransport(email_http, api_key="k", from_email="cfo@rgnr8.com"),
        HttpPushTransport(push_http, api_key="k", api_url="https://push.example/send"),
    )
    env = build_envelope(build_briefing(breach_forecast()), "owner@acme.com", tenant_id="acme")
    assert Channel.PUSH in env.channels  # at-risk → email + push
    receipt = deliverer.send(env, at="2026-08-10T08:00:00Z")
    assert set(receipt.channels) == {Channel.EMAIL, Channel.PUSH}
    assert receipt.status.startswith("SENT")
    assert len(email_http.calls) == 1 and len(push_http.calls) == 1


def test_provider_deliverer_reports_partial_when_the_email_api_rejects() -> None:
    email_http = FakeHttpClient(status=500, body="boom")
    deliverer = ProviderDeliverer(HttpEmailTransport(email_http, api_key="k", from_email="c@r.com"))
    env = build_envelope(build_briefing(healthy_forecast()), "owner@acme.com", tenant_id="acme")
    receipt = deliverer.send(env, at="2026-08-10T08:00:00Z")
    assert receipt.channels == ()  # nothing went out
    assert receipt.status.startswith("FAILED")
