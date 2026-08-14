"""The production worker assembly: briefings + alerts + reports composed into one
scheduler, each firing on its own cadence and idempotent on re-tick."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rgnr8_alerts import InMemoryAlertSink
from rgnr8_briefing import RecordingDeliverer
from rgnr8_ops import (
    InMemoryJobRunStore,
    Fleet,
    build_fleet_jobs,
    build_worker,
)
from rgnr8_ops.scheduler import InMemoryLeaseStore
from rgnr8_reports import RecordingReportSink
from factory import at_risk_tenant, steady_tenant

SECRET = "worker-secret"
NOW_EPOCH = 1_760_000_000
# 2026-08-10 is a Monday; 15:00 UTC is past 08:00 America/Denver → the weekly fire.
MON = datetime(2026, 8, 10, 15, 0, tzinfo=ZoneInfo("UTC"))


def _fleet() -> Fleet:
    f = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    f.onboard(steady_tenant())
    f.onboard(at_risk_tenant())
    return f


def _sinks():
    return RecordingDeliverer(), InMemoryAlertSink(), RecordingReportSink()


def test_build_fleet_jobs_has_all_three_in_order() -> None:
    fleet = _fleet()
    deliverer, alerts, reports = _sinks()
    jobs = build_fleet_jobs(
        fleet, deliverer, alert_sink=alerts, report_sink=reports,
        alert_clock=lambda: float(NOW_EPOCH),
    )
    assert [j.name for j in jobs] == ["briefings", "alerts", "reports"]


def test_worker_runs_briefings_alerts_and_reports_on_one_tick() -> None:
    fleet = _fleet()
    fleet.schedule_report("acme", "exec_board_pack", fmt="pdf")
    deliverer, alerts, reports = _sinks()
    worker = build_worker(
        fleet, deliverer, alert_sink=alerts, report_sink=reports,
        alert_clock=lambda: float(NOW_EPOCH), job_store=InMemoryJobRunStore(),
    )
    results = worker.tick(MON)
    by = {r.name: r for r in results}
    assert by["briefings"].ran and by["alerts"].ran and by["reports"].ran
    # the scheduled report actually went out
    assert any(d.tenant_id == "acme" and d.fmt == "pdf" for d in reports.sent)
    # the at-risk tenant fired a cash alert
    assert alerts.sent


def test_worker_report_cadence_is_idempotent_within_the_week() -> None:
    fleet = _fleet()
    fleet.schedule_report("acme", "runway", fmt="csv")
    deliverer, alerts, reports = _sinks()
    worker = build_worker(
        fleet, deliverer, alert_sink=alerts, report_sink=reports,
        alert_clock=lambda: float(NOW_EPOCH), job_store=InMemoryJobRunStore(),
    )
    worker.tick(MON)
    assert len(reports.sent) == 1
    worker.tick(MON + timedelta(hours=2))  # same week → no new report
    assert len(reports.sent) == 1
    worker.tick(MON + timedelta(days=7))    # next week → fires again
    assert len(reports.sent) == 2


def test_build_worker_leased_variant_is_at_most_one_runner() -> None:
    fleet = _fleet()
    fleet.schedule_report("acme", "runway", fmt="pdf")
    deliverer, alerts, reports = _sinks()
    leases = InMemoryLeaseStore()
    worker = build_worker(
        fleet, deliverer, alert_sink=alerts, report_sink=reports,
        alert_clock=lambda: float(NOW_EPOCH), job_store=InMemoryJobRunStore(),
        leases=leases,
    )
    # leader runs; a second holder in the same window is a no-op
    lead = worker.tick(MON, "worker-a")
    assert any(r.name == "reports" and r.ran for r in lead)
    follower = worker.tick(MON, "worker-b")
    assert follower[0].detail == "skipped: not leader"
    assert len(reports.sent) == 1


def test_callable_report_sink_wraps_a_delivery_function() -> None:
    from rgnr8_reports import CallableReportSink

    seen = []
    sink = CallableReportSink(lambda d: seen.append((d.tenant_id, d.fmt)) or None)
    fleet = _fleet()
    fleet.schedule_report("acme", "runway", fmt="pdf")
    deliverer, alerts, _ = _sinks()
    worker = build_worker(
        fleet, deliverer, alert_sink=alerts, report_sink=sink,
        alert_clock=lambda: float(NOW_EPOCH), job_store=InMemoryJobRunStore(),
    )
    worker.tick(MON)
    assert seen == [("acme", "pdf")]
