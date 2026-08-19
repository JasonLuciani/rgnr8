from factory import breach_forecast, healthy_forecast
from rgnr8_briefing import (
    Channel,
    Priority,
    RecordingDeliverer,
    WeekActual,
    build_briefing,
    build_envelope,
    compute_variance,
)


def test_stable_briefing_is_email_only_normal_priority() -> None:
    b = build_briefing(healthy_forecast())
    env = build_envelope(b, "owner@acme.com", tenant_id="acme")
    assert env.priority is Priority.NORMAL
    assert env.channels == (Channel.EMAIL,)
    assert "steady" in env.subject.lower()


def test_at_risk_briefing_pushes_urgent_with_action_link() -> None:
    b = build_briefing(breach_forecast())
    env = build_envelope(b, "owner@acme.com", tenant_id="acme")
    assert env.priority is Priority.URGENT
    assert Channel.PUSH in env.channels
    assert any("action" in dl.url for dl in env.deep_links)
    assert any("briefing" in dl.url for dl in env.deep_links)


def test_material_worsening_vs_published_forecast_escalates_to_push() -> None:
    published = healthy_forecast().published("controller", "2026-08-06T22:00:00Z")
    # actuals 40% below expectation -> divergent and worse
    actuals = [
        WeekActual(w.index, w.closing - w.closing.scale_by(4000, 10000))
        for w in published.projection.weeks[:4]
    ]
    variance = compute_variance(published, actuals, tolerance_bps=1000)

    b = build_briefing(healthy_forecast())  # current status still STABLE...
    env = build_envelope(b, "owner@acme.com", tenant_id="acme", variance=variance)
    assert env.priority is Priority.URGENT  # ...but worsening actuals push it
    assert Channel.PUSH in env.channels
    assert "VS. LAST PUBLISHED FORECAST" in env.text_body


def test_recording_deliverer_captures_send() -> None:
    b = build_briefing(breach_forecast())
    env = build_envelope(b, "owner@acme.com", tenant_id="acme")
    deliverer = RecordingDeliverer()
    receipt = deliverer.send(env, at="2026-08-10T08:00:00Z")
    assert len(deliverer.sent) == 1
    assert receipt.status == "SENT"
    assert receipt.delivered_at == "2026-08-10T08:00:00Z"
    assert receipt.channels == env.channels


def test_variance_section_appears_in_text_body() -> None:
    published = healthy_forecast().published("controller", "2026-08-06T22:00:00Z")
    actuals = [WeekActual(w.index, w.closing) for w in published.projection.weeks[:3]]
    variance = compute_variance(published, actuals)
    env = build_envelope(build_briefing(healthy_forecast()), "o@a.com", tenant_id="acme", variance=variance)
    assert "VS. LAST PUBLISHED FORECAST" in env.text_body
