from rgnr8_forecast import Money
from rgnr8_briefing import WeekActual, compute_variance, render_variance_text
from factory import healthy_forecast


def _published():
    return healthy_forecast().verified("controller").published("controller", "2026-08-06T22:00:00Z")


def test_perfect_actuals_have_zero_variance_and_track() -> None:
    fc = _published()
    actuals = [WeekActual(w.index, w.closing) for w in fc.projection.weeks[:4]]
    report = compute_variance(fc, actuals)
    assert report.mean_abs_pct_bps == 0
    assert report.within_tolerance
    assert all(w.delta.is_zero for w in report.weeks)
    assert report.forecast_version_id == fc.version.version_id


def test_divergent_actuals_exceed_tolerance() -> None:
    fc = _published()
    # actual closings 30% below expectation each week
    actuals = [
        WeekActual(w.index, w.closing - w.closing.scale_by(3000, 10000))
        for w in fc.projection.weeks[:4]
    ]
    report = compute_variance(fc, actuals, tolerance_bps=1000)
    assert not report.within_tolerance
    assert report.mean_abs_pct_bps is not None and report.mean_abs_pct_bps >= 2500
    assert all(w.delta.is_negative for w in report.weeks)


def test_only_elapsed_weeks_are_compared() -> None:
    fc = _published()
    actuals = [WeekActual(2, fc.projection.weeks[1].closing)]
    report = compute_variance(fc, actuals)
    assert len(report.weeks) == 1
    assert report.weeks[0].index == 2


def test_render_variance_text_is_readable() -> None:
    fc = _published()
    actuals = [WeekActual(w.index, w.closing) for w in fc.projection.weeks[:2]]
    text = render_variance_text(compute_variance(fc, actuals))
    assert "VS. LAST PUBLISHED FORECAST" in text
    assert "wk1" in text
