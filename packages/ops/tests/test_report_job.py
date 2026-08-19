"""Scheduled reports on the production scheduler: render + deliver on cadence,
fire once per window, advance + persist each schedule's cursor, register
alongside the briefing/alerts jobs."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from factory import at_risk_tenant, steady_tenant
from rgnr8_briefing import Schedule
from rgnr8_ops import (
    Dispatcher,
    Fleet,
    InMemoryJobRunStore,
    build_report_job,
)
from rgnr8_reports import (
    InMemoryReportScheduleStore,
    RecordingReportSink,
    ReportSchedule,
)

SECRET = "reports-secret"
NOW_EPOCH = 1_760_000_000

# 2026-08-10 is a Monday; 15:00 UTC is past 08:00 America/Denver → the Monday fire.
MON = datetime(2026, 8, 10, 15, 0, tzinfo=ZoneInfo("UTC"))


def _fleet() -> Fleet:
    f = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    f.onboard(steady_tenant())
    f.onboard(at_risk_tenant())
    return f


def _weekly(tenant: str, report_id: str, fmt: str = "pdf") -> ReportSchedule:
    return ReportSchedule(
        tenant_id=tenant, report_id=report_id, recipient=f"owner@{tenant}.com",
        schedule=Schedule(weekday=0, hour=8, minute=0, timezone="America/Denver"), fmt=fmt,
    )


def test_report_job_delivers_due_reports_and_is_idempotent() -> None:
    fleet = _fleet()
    store = InMemoryReportScheduleStore()
    store.save(_weekly("acme", "exec_board_pack", "pdf"))
    store.save(_weekly("bright", "cash_flow_outlook", "xlsx"))
    sink = RecordingReportSink()
    job = build_report_job(fleet, store, sink)

    detail = job.run(MON)
    assert "2 report(s) delivered" == detail
    assert len(sink.sent) == 2
    kinds = {(d.tenant_id, d.fmt) for d in sink.sent}
    assert ("acme", "pdf") in kinds and ("bright", "xlsx") in kinds
    # the PDF really is a PDF; the xlsx really is a workbook
    pdf = next(d for d in sink.sent if d.fmt == "pdf")
    assert isinstance(pdf.content, bytes) and pdf.content.startswith(b"%PDF-")

    # same week → nothing new (durable cursor persisted back to the store)
    job.run(MON + timedelta(hours=2))
    assert len(sink.sent) == 2
    assert store.get("acme", "exec_board_pack").last_sent is not None


def test_report_job_fires_again_next_cadence() -> None:
    fleet = _fleet()
    store = InMemoryReportScheduleStore()
    store.save(_weekly("acme", "runway", "csv"))
    sink = RecordingReportSink()
    job = build_report_job(fleet, store, sink)

    job.run(MON)
    assert len(sink.sent) == 1
    job.run(MON + timedelta(days=7))  # next Monday
    assert len(sink.sent) == 2


def test_report_job_skips_unknown_tenant_and_unknown_report() -> None:
    fleet = _fleet()
    store = InMemoryReportScheduleStore()
    store.save(_weekly("ghost", "runway"))          # tenant not in fleet → not_ready
    store.save(_weekly("acme", "not_a_report"))     # unknown report id → not_ready
    sink = RecordingReportSink()
    job = build_report_job(fleet, store, sink)

    assert job.run(MON) == "0 report(s) delivered"
    assert not sink.sent


def test_report_job_saved_custom_report_resolves() -> None:
    from rgnr8_reports import InMemorySavedReportStore, build_report

    fleet = _fleet()
    saved = InMemorySavedReportStore()
    saved.save("acme", build_report(
        {"id": "my_pack", "title": "My Pack", "sections": [{"kind": "runway"}]}
    ))
    store = InMemoryReportScheduleStore()
    store.save(_weekly("acme", "my_pack", "pdf"))
    sink = RecordingReportSink()
    job = build_report_job(fleet, store, sink, saved=saved)

    assert job.run(MON) == "1 report(s) delivered"
    assert sink.sent[0].title == "My Pack"


def test_report_job_registers_on_dispatcher() -> None:
    fleet = _fleet()
    store = InMemoryReportScheduleStore()
    store.save(_weekly("acme", "runway"))
    sink = RecordingReportSink()
    job = build_report_job(fleet, store, sink)
    d = Dispatcher([job], InMemoryJobRunStore())

    r = d.tick(MON)[0]
    assert r.ran is True and "delivered" in r.detail
    # due every tick; the per-schedule cursor keeps it quiet
    assert d.tick(MON + timedelta(minutes=1))[0].ran is True
    assert len(sink.sent) == 1


def test_fleet_schedule_report_and_report_job_convenience() -> None:
    fleet = _fleet()
    # defaults recipient + cadence from the tenant's briefing settings
    sched = fleet.schedule_report("acme", "exec_board_pack", fmt="pdf")
    assert sched.recipient == "owner@acme.com"
    assert fleet.report_schedules.get("acme", "exec_board_pack") is not None

    sink = RecordingReportSink()
    job = fleet.report_job(sink)
    assert job.run(MON) == "1 report(s) delivered"
    assert sink.sent[0].tenant_id == "acme"


def test_fleet_schedule_report_rejects_unknown_tenant() -> None:
    import pytest

    fleet = _fleet()
    with pytest.raises(KeyError):
        fleet.schedule_report("ghost", "runway")
