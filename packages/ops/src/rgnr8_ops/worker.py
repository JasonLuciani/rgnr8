"""The production scheduler worker — the one place that composes the fleet's
recurring jobs and drives them on a cadence.

Until now each job had a factory (`BriefingDispatchJob`, `build_alerts_job`,
`Fleet.report_job`) and its own tests, but nothing assembled the *production* job
list. This module is that assembly: :func:`build_fleet_jobs` returns the ordered
job set — weekly briefings, proactive cash alerts, and scheduled reports — and
:func:`build_worker` wraps them in a `Dispatcher` (single worker) or a
`LeasedDispatcher` (at-most-one runner across N workers). A deployment's worker
process calls ``worker.tick(now[, holder])`` on a fixed interval; every job keeps
its own durable cursor, so the whole set is idempotent and failure-isolated.

The delivery transports are injected seams: a briefing `Deliverer`, an
`AlertSink`, and a `ReportSink`. Wrap a plain send-callable with
`CallableAlertSink` / `CallableReportSink` to point them at the real email/push
provider once its credentials land — no code change here.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from rgnr8_alerts import AlertSink
from rgnr8_briefing import Deliverer
from rgnr8_reports import ReportSink, SavedReportStore

from .alerts_job import DEFAULT_BREACH_WEEKS, build_alerts_job
from .fleet import Fleet
from .scheduler import (
    BriefingDispatchJob,
    Dispatcher,
    Job,
    JobRunStore,
    LeasedDispatcher,
    LeaseStore,
)


def build_fleet_jobs(
    fleet: Fleet,
    deliverer: Deliverer,
    *,
    alert_sink: AlertSink,
    report_sink: ReportSink,
    alert_clock: Callable[[], float],
    saved_reports: SavedReportStore | None = None,
    base_url: str = "https://app.rgnr8.com",
    breach_weeks: int = DEFAULT_BREACH_WEEKS,
) -> list[Job]:
    """Assemble the fleet's production job set, in dispatch order:

    1. **briefings** — the weekly owner briefing (`DeliveryRuntime.tick`, whose
       per-subscription cursor decides who is due),
    2. **alerts** — proactive cash alerts (deduped by the alert dispatcher),
    3. **reports** — scheduled reports (each `ReportSchedule`'s own cadence).

    Every job is due every tick; its own durable cursor (or the alert dedupe)
    keeps it quiet until there is real work. ``saved_reports`` lets scheduled
    *custom* reports resolve; the transports are injected seams."""
    runtime = fleet.delivery_runtime(deliverer)
    briefings: Job = BriefingDispatchJob("briefings", lambda now: list(runtime.tick(now)))
    alerts: Job = build_alerts_job(fleet, alert_sink, clock=alert_clock, weeks=breach_weeks)
    reports: Job = fleet.report_job(report_sink, saved=saved_reports)
    return [briefings, alerts, reports]


def build_worker(
    fleet: Fleet,
    deliverer: Deliverer,
    *,
    alert_sink: AlertSink,
    report_sink: ReportSink,
    alert_clock: Callable[[], float],
    saved_reports: SavedReportStore | None = None,
    base_url: str = "https://app.rgnr8.com",
    breach_weeks: int = DEFAULT_BREACH_WEEKS,
    job_store: JobRunStore | None = None,
    leases: LeaseStore | None = None,
    lease_ttl: timedelta = timedelta(minutes=5),
) -> Dispatcher | LeasedDispatcher:
    """Build the production scheduler over the fleet's job set. With ``leases``
    given, returns a `LeasedDispatcher` (at-most-one runner across workers —
    ``tick(now, holder)``); otherwise a single-process `Dispatcher`
    (``tick(now)``). Durable ``job_store`` cursors make either idempotent across
    restarts."""
    jobs = build_fleet_jobs(
        fleet,
        deliverer,
        alert_sink=alert_sink,
        report_sink=report_sink,
        alert_clock=alert_clock,
        saved_reports=saved_reports,
        base_url=base_url,
        breach_weeks=breach_weeks,
    )
    if leases is not None:
        return LeasedDispatcher(jobs, leases, job_store=job_store, ttl=lease_ttl)
    return Dispatcher(jobs, job_store)
