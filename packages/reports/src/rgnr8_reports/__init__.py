"""RGNR8 reporting — one surface that composes every data engine into reports.

Owners, bookkeepers, and accountants each need reports: some standard (P&L, cash
flow, AR aging), some bespoke. The data already lives across the platform's
engines; this package is the unifying reporting layer over them.

Shape:

* **Model** (:mod:`.model`) — a declarative :class:`ReportSpec` / :class:`SectionSpec`
  and the rendered result types (:class:`Report`, :class:`ReportSection`, and the
  :data:`Block` union of :class:`KpiRow` / :class:`Table` / :class:`Narrative` /
  :class:`Chart`).
* **Context** (:mod:`.context`) — :class:`DataContext`, the single frozen bag of
  every optional data source a report might read.
* **Sections** (:mod:`.sections`) — a registry mapping each section ``kind`` to a
  builder that composes one platform engine (forecast, briefing, AR, billing,
  recon, scenario, or the ``financial-statements/1`` contract). Missing data
  degrades to a graceful "not available" block.
* **Engine** (:mod:`.engine`) — :func:`render`, which runs a spec against a context
  under an **injected clock** so output is fully deterministic.
* **Library** (:mod:`.library`) — ten ready-to-run baseline reports.
* **Builder** (:mod:`.builder`) — :func:`build_report` for custom specs, plus the
  per-tenant :class:`SavedReportStore` (in-memory + SQL).
* **Renderers** (:mod:`.render`) — self-contained branded HTML, CSV, and JSON.

Everything is deterministic (no wall clock, no randomness), money is exact integer
minor units throughout, dataclasses are frozen, and the package type-checks under
``mypy --strict``.
"""

from __future__ import annotations

from .builder import (
    InMemorySavedReportStore,
    ReportSpecError,
    SavedReportStore,
    SqlSavedReportStore,
    build_report,
    spec_from_dict,
    spec_to_dict,
)
from .context import DataContext, ReconFigures, Transaction
from .library import BASELINE_REPORTS, baseline
from .model import (
    Block,
    Chart,
    KpiRow,
    Narrative,
    Report,
    ReportSection,
    ReportSpec,
    SectionSpec,
    Table,
)
from .pdf import render_pdf

# Import the ``render`` submodule's functions before the engine's ``render``
# function, so that ``rgnr8_reports.render`` resolves to the engine entrypoint
# (importing the submodule binds its name on the package; the engine import wins
# by coming last). This ordering is deliberate — do not let isort reshuffle it.
from .render import render_csv, render_html, to_dict, to_json
from .engine import Clock, render  # noqa: E402  (must bind ``render`` last)
from .schedule import (
    VALID_FORMATS,
    CallableReportSink,
    ContextBuilder,
    InMemoryReportScheduleStore,
    RecordingReportSink,
    ReportDelivery,
    ReportOutcome,
    ReportReceipt,
    ReportSchedule,
    ReportScheduleStore,
    ReportSink,
    SpecResolver,
    SqlReportScheduleStore,
    render_in_format,
    run_due_reports,
    schedule_from_dict,
    schedule_to_dict,
)
from .sections import REGISTRY, known_kinds
from .xlsx import render_xlsx

__version__ = "0.1.0"

__all__ = [
    # context
    "DataContext",
    "Transaction",
    "ReconFigures",
    # model
    "SectionSpec",
    "ReportSpec",
    "KpiRow",
    "Table",
    "Narrative",
    "Chart",
    "Block",
    "ReportSection",
    "Report",
    # sections
    "REGISTRY",
    "known_kinds",
    # engine
    "render",
    "Clock",
    # library
    "BASELINE_REPORTS",
    "baseline",
    # builder
    "build_report",
    "spec_to_dict",
    "spec_from_dict",
    "ReportSpecError",
    "SavedReportStore",
    "InMemorySavedReportStore",
    "SqlSavedReportStore",
    # render
    "render_html",
    "render_csv",
    "render_pdf",
    "render_xlsx",
    "to_dict",
    "to_json",
    # schedule
    "ReportSchedule",
    "ReportDelivery",
    "ReportReceipt",
    "ReportOutcome",
    "ReportSink",
    "RecordingReportSink",
    "CallableReportSink",
    "ReportScheduleStore",
    "InMemoryReportScheduleStore",
    "SqlReportScheduleStore",
    "SpecResolver",
    "ContextBuilder",
    "VALID_FORMATS",
    "render_in_format",
    "run_due_reports",
    "schedule_to_dict",
    "schedule_from_dict",
]
