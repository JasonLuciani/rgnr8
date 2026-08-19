from factory import breach_forecast, healthy_forecast
from rgnr8_briefing import render_today_html


def test_today_is_self_contained_html() -> None:
    html = render_today_html(breach_forecast(), "Bright Agency")
    assert html.startswith("<!doctype html>")
    assert "http://" not in html and "https://" not in html  # no external assets
    assert "Bright Agency" in html
    assert "<svg" in html  # the cash chart
    assert "Ask your CFO" in html
    assert "ANSWERS" in html  # embedded Q&A


def test_today_reflects_status_and_action() -> None:
    at_risk = render_today_html(breach_forecast())
    assert "At risk" in at_risk
    assert "Do this" in at_risk  # a breach carries an action
    assert "#B4443C" in at_risk  # RGNR8 brand risk color

    stable = render_today_html(healthy_forecast())
    assert "Stable" in stable
    assert "#3E7C5A" in stable  # RGNR8 brand positive color
    assert "RGNR<span" in stable  # the brand wordmark is present


def test_today_chart_has_points_for_all_weeks() -> None:
    html = render_today_html(healthy_forecast())
    # 14 points (opening + 13 weeks) embedded for the hover layer
    assert html.count('"label":') >= 14
