"""Proactive cash alerts on the scheduler: fire once, dedupe on re-run."""

from datetime import datetime, timedelta

from factory import at_risk_tenant, steady_tenant
from rgnr8_alerts import InMemoryAlertSink
from rgnr8_ops import (
    Dispatcher,
    Fleet,
    InMemoryJobRunStore,
    build_alerts_job,
    default_alert_rules,
)

SECRET = "alerts-secret"
NOW_EPOCH = 1_760_000_000.0
T0 = datetime(2026, 9, 2, 8, 0)


def _fleet() -> Fleet:
    f = Fleet(jwt_secret=SECRET, clock=lambda: int(NOW_EPOCH))
    f.onboard(steady_tenant())
    f.onboard(at_risk_tenant())
    return f


def test_alerts_job_fires_cash_at_risk_and_dedupes_on_rerun() -> None:
    sink = InMemoryAlertSink()
    fleet = _fleet()
    job = build_alerts_job(fleet, sink, clock=lambda: NOW_EPOCH)

    delivered = job.run(T0)
    # the at-risk tenant fired at least one alert, namespaced + attributed to it
    assert sink.sent, "expected an alert for the at-risk tenant"
    assert "alert(s) dispatched" in delivered
    at_risk_keys = [a for a in sink.sent if a.key.startswith("bright:")]
    assert at_risk_keys, "the at-risk tenant should have fired"
    assert any(a.evidence.get("tenant_id") == "bright" for a in sink.sent)
    fired = len(sink.sent)

    # re-run at a later tick with the same (still-firing) condition → deduped, quiet
    job.run(T0 + timedelta(minutes=1))
    assert len(sink.sent) == fired


def test_alerts_job_registers_alongside_briefing_job_and_runs_every_tick() -> None:
    sink = InMemoryAlertSink()
    fleet = _fleet()
    job = build_alerts_job(fleet, sink, clock=lambda: NOW_EPOCH)
    d = Dispatcher([job], InMemoryJobRunStore())

    r = d.tick(T0)[0]
    assert r.ran is True and "dispatched" in r.detail
    # due every tick (dedupe, not a coarse interval, keeps it quiet)
    assert d.tick(T0 + timedelta(minutes=1))[0].ran is True


def test_default_rules_cover_floor_breach_and_balance_below_floor() -> None:
    rules = default_alert_rules(steady_tenant().config.minimum_cash)
    assert len(rules) == 2  # FloorBreachWithinWeeks + BalanceBelow(floor)
