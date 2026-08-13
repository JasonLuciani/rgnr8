"""Scheduled reports on the production scheduler.

The reporting engine can render + deliver a report on a cadence
(``rgnr8_reports.run_due_reports``); ``ReportJob`` is the scheduler seam that
drives it across the beta fleet. On every dispatcher tick it loads every stored
:class:`~rgnr8_reports.ReportSchedule`, renders + delivers the ones whose cadence
is due (through an injected :class:`~rgnr8_reports.ReportSink`), advances each
fired schedule's cursor, and persists that cursor back to the store — so the job
is idempotent across worker restarts and safe under the leased/distributed
dispatcher, exactly like ``BriefingDispatchJob`` and ``AlertsJob``.

Each schedule's report is rendered against a :class:`~rgnr8_reports.DataContext`
built from the tenant's fleet inputs (a fresh forecast + its open invoices).
Sections whose data isn't available at the operator layer (financial statements,
usage, recon) degrade gracefully in the render, so an operator-scheduled report
always produces something useful.

    job = build_report_job(fleet, store, sink)   # register alongside BriefingDispatchJob
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rgnr8_forecast import run_forecast
from rgnr8_reports import (
    BASELINE_REPORTS,
    ContextBuilder,
    DataContext,
    ReportSchedule,
    ReportScheduleStore,
    ReportSink,
    ReportSpec,
    SavedReportStore,
    SpecResolver,
    baseline,
    run_due_reports,
)

from .fleet import Fleet


def fleet_context_builder(fleet: Fleet) -> ContextBuilder:
    """A :class:`ContextBuilder` that assembles a report ``DataContext`` for a
    schedule's tenant from the fleet's forecast inputs (a fresh forecast + open
    invoices). Returns ``None`` when the tenant isn't in the fleet, so that
    schedule skips cleanly."""

    def build(sched: ReportSchedule) -> DataContext | None:
        bt = fleet.tenants.get(sched.tenant_id)
        if bt is None:
            return None
        forecast = run_forecast(bt.inputs, bt.config)
        as_of = bt.inputs.opening.as_of
        return DataContext(
            period=as_of.strftime("%b %Y"),
            as_of=as_of,
            currency=bt.inputs.currency,
            forecast=forecast,
            invoices=bt.inputs.invoices,
            forecast_inputs=bt.inputs,
            forecast_config=bt.config,
        )

    return build


def make_spec_resolver(
    saved: SavedReportStore | None = None,
) -> SpecResolver:
    """A spec resolver: a baseline id resolves to its baseline spec; otherwise a
    per-tenant saved custom report is looked up (when a ``saved`` store is given).
    Unknown ids resolve to ``None`` so that schedule skips."""

    def resolve(sched: ReportSchedule) -> ReportSpec | None:
        if sched.report_id in BASELINE_REPORTS:
            return baseline(sched.report_id)
        if saved is not None:
            return saved.get(sched.tenant_id, sched.report_id)
        return None

    return resolve


@dataclass(slots=True)
class ReportJob:
    """A scheduler `Job` that renders + delivers every due scheduled report.

    Due every tick (each schedule's own cursor decides what actually fires). After
    delivery it re-saves each fired schedule so its advanced ``last_sent`` cursor
    is durable regardless of whether the store returns live objects or copies."""

    name: str
    store: ReportScheduleStore
    sink: ReportSink
    context_for: ContextBuilder
    spec_for: SpecResolver

    def due(self, now: datetime, last_run: datetime | None) -> bool:
        return True  # per-schedule cursors decide; the job just drives the tick

    def run(self, now: datetime) -> str:
        schedules = self.store.list_all()
        outcomes = run_due_reports(
            schedules,
            now,
            spec_for=self.spec_for,
            context_for=self.context_for,
            sink=self.sink,
        )
        fired = [o for o in outcomes if o.fired]
        # Persist advanced cursors (idempotent; safe for copy-returning stores).
        by_key = {(s.tenant_id, s.report_id): s for s in schedules}
        for o in fired:
            sched = by_key.get((o.tenant_id, o.report_id))
            if sched is not None:
                self.store.save(sched)
        return f"{len(fired)} report(s) delivered"


def build_report_job(
    fleet: Fleet,
    store: ReportScheduleStore,
    sink: ReportSink,
    *,
    saved: SavedReportStore | None = None,
    name: str = "reports",
) -> ReportJob:
    """Job factory: wire a `ReportJob` to the fleet's context builder and a spec
    resolver (baselines + optional saved custom reports) in one call, so the
    worker can register it alongside `BriefingDispatchJob` / `AlertsJob`."""
    return ReportJob(
        name=name,
        store=store,
        sink=sink,
        context_for=fleet_context_builder(fleet),
        spec_for=make_spec_resolver(saved),
    )
