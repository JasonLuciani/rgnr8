from factory import breach_forecast, healthy_forecast
from rgnr8_briefing import StatusLevel, build_briefing


def test_healthy_is_stable() -> None:
    b = build_briefing(healthy_forecast())
    assert b.status is StatusLevel.STABLE
    assert "stays above" in b.headline
    assert b.primary_action is None


def test_breach_is_at_risk_with_action() -> None:
    b = build_briefing(breach_forecast())
    assert b.status is StatusLevel.AT_RISK
    assert b.primary_action is not None
    assert any(f.key == "shortfall" for f in b.facts)


def test_core_facts_present() -> None:
    b = build_briefing(healthy_forecast())
    keys = {f.key for f in b.facts}
    assert {"cash_today", "floor", "low_point", "cushion", "total_in", "total_out"} <= keys


def test_drivers_sorted_descending_and_share_computed() -> None:
    b = build_briefing(breach_forecast())
    amounts = [d.amount.minor_units for d in b.drivers]
    assert amounts == sorted(amounts, reverse=True)
    assert all(0 <= d.share_bps <= 10000 for d in b.drivers)
    # payroll net should be the top driver
    assert b.drivers[0].amount.minor_units >= b.drivers[-1].amount.minor_units


def test_week_glance_has_13_entries_and_status() -> None:
    b = build_briefing(breach_forecast())
    assert len(b.week_glance) == 13
    assert any(w.status is StatusLevel.AT_RISK for w in b.week_glance)
