from factory import breach_forecast, healthy_forecast
from rgnr8_briefing import build_briefing, render_html, render_text


def test_text_render_contains_headline_and_badge() -> None:
    b = build_briefing(breach_forecast())
    out = render_text(b)
    assert "AT RISK" in out
    assert b.headline in out
    assert "13-WEEK GLANCE" in out
    assert "DO THIS" in out  # action present on a breach


def test_text_render_healthy_has_no_action_block() -> None:
    b = build_briefing(healthy_forecast())
    out = render_text(b)
    assert "STABLE" in out
    assert "DO THIS" not in out


def test_html_render_is_self_contained() -> None:
    b = build_briefing(breach_forecast())
    html = render_html(b)
    assert html.startswith("<!doctype html>")
    assert "<style>" in html and "http://" not in html and "https://" not in html
    assert b.headline in html
    # status color band present
    assert "#B4443C" in html  # RGNR8 brand risk color (AT_RISK)
