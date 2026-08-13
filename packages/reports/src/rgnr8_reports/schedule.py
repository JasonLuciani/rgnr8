"""Scheduled reports — deliver a report on a recurring cadence.

Owners and accountants want the board pack in their inbox every Monday, not only
when they remember to open the app. This module is the scheduling half of the
reporting feature: a per-tenant :class:`ReportSchedule` (which report, in which
format, to whom, on what cadence), a persistence seam
(:class:`ReportScheduleStore` — Protocol + in-memory + SQL), a delivery seam
(:class:`ReportSink`), and :func:`run_due_reports`, which renders and delivers
every schedule that is due and advances its cursor.

Cadence + dueness reuse the briefing scheduler's proven, deterministic math
(:func:`rgnr8_briefing.is_due` / :func:`~rgnr8_briefing.most_recent_fire` over a
:class:`~rgnr8_briefing.Schedule`) with the same **catch-up semantics**: a
schedule is due when the most recent scheduled fire time is newer than the last
delivery, so a missed tick still sends once — and only once — on the next run.
Each schedule carries its own ``last_sent`` cursor, so the job is idempotent
across worker restarts and safe under the leased/distributed dispatcher.

The scheduler seam that drives this on a cadence in production lives in
``rgnr8_ops`` (``ReportJob``); this module is the pure, deterministic domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Protocol

from rgnr8_briefing import Schedule, is_due, most_recent_fire

from .context import DataContext
from .engine import render
from .model import Report, ReportSpec
from .render import render_csv, render_html, to_json
from .pdf import render_pdf
from .xlsx import render_xlsx

# The delivery formats a schedule can request, each bound to its renderer. HTML,
# CSV, and JSON are text; PDF and XLSX are binary (bytes).
VALID_FORMATS = ("html", "pdf", "xlsx", "csv", "json")


def render_in_format(report: Report, fmt: str) -> str | bytes:
    """Render ``report`` in one of :data:`VALID_FORMATS`. Unknown → ``ValueError``."""
    if fmt == "html":
        return render_html(report)
    if fmt == "pdf":
        return render_pdf(report)
    if fmt == "xlsx":
        return render_xlsx(report)
    if fmt == "csv":
        return render_csv(report)
    if fmt == "json":
        return to_json(report)
    raise ValueError(f"unknown report format {fmt!r}; valid: {VALID_FORMATS}")


def _is_binary(fmt: str) -> bool:
    return fmt in ("pdf", "xlsx")


@dataclass(slots=True)
class ReportSchedule:
    """A tenant's standing subscription to a recurring report.

    ``report_id`` names a baseline or a saved custom report. ``fmt`` is one of
    :data:`VALID_FORMATS`. ``last_sent`` is the cursor (the fire time last
    satisfied); a paused schedule stays stored but never fires."""

    tenant_id: str
    report_id: str
    recipient: str
    schedule: Schedule
    fmt: str = "pdf"
    last_sent: datetime | None = None
    active: bool = True

    def __post_init__(self) -> None:
        if self.fmt not in VALID_FORMATS:
            raise ValueError(f"unknown report format {self.fmt!r}; valid: {VALID_FORMATS}")


@dataclass(frozen=True, slots=True)
class ReportDelivery:
    """One rendered report, ready to hand to a :class:`ReportSink`. ``content`` is
    a ``str`` for text formats and ``bytes`` for PDF/XLSX; ``is_binary`` says
    which, so a sink can attach or inline correctly."""

    tenant_id: str
    report_id: str
    title: str
    recipient: str
    fmt: str
    content: str | bytes
    generated_at: datetime
    fire_time: datetime

    @property
    def is_binary(self) -> bool:
        return _is_binary(self.fmt)

    @property
    def filename(self) -> str:
        ext = self.fmt if self.fmt != "json" else "json"
        return f"{self.report_id}.{ext}"


@dataclass(frozen=True, slots=True)
class ReportReceipt:
    tenant_id: str
    report_id: str
    recipient: str
    fmt: str
    delivered_at: str
    status: str = "SENT"


@dataclass(frozen=True, slots=True)
class ReportOutcome:
    tenant_id: str
    report_id: str
    fired: bool
    fire_time: datetime | None
    receipt: ReportReceipt | None
    skipped_reason: str | None = None


class ReportSink(Protocol):
    """The transport a scheduled report is delivered through (email attachment,
    object store, webhook…). Real providers implement :meth:`send`."""

    def send(self, delivery: ReportDelivery) -> ReportReceipt: ...


class RecordingReportSink:
    """Test/dev sink: records deliveries instead of sending them."""

    def __init__(self) -> None:
        self.sent: list[ReportDelivery] = []

    def send(self, delivery: ReportDelivery) -> ReportReceipt:
        self.sent.append(delivery)
        return ReportReceipt(
            tenant_id=delivery.tenant_id,
            report_id=delivery.report_id,
            recipient=delivery.recipient,
            fmt=delivery.fmt,
            delivered_at=delivery.generated_at.isoformat(),
        )


# The two callbacks the runner needs to turn a schedule into a rendered report:
# resolve its spec (baseline or saved) and build its data context. Either
# returning ``None`` skips that schedule for this tick (spec/context not ready).
SpecResolver = Callable[[ReportSchedule], ReportSpec | None]
ContextBuilder = Callable[[ReportSchedule], DataContext | None]


def run_due_reports(
    schedules: list[ReportSchedule],
    now: datetime,
    *,
    spec_for: SpecResolver,
    context_for: ContextBuilder,
    sink: ReportSink,
) -> list[ReportOutcome]:
    """Render + deliver every schedule that is due, advancing each fired
    schedule's ``last_sent`` to the fire time it satisfied (not ``now``) so the
    cadence stays stable. ``now`` must be timezone-aware (determinism: inject the
    clock). Mutates ``schedules`` in place; one schedule's failure never aborts
    the batch — a spec/context that isn't ready simply skips."""
    outcomes: list[ReportOutcome] = []
    for sched in schedules:
        if not sched.active:
            outcomes.append(ReportOutcome(sched.tenant_id, sched.report_id, False, None, None, "paused"))
            continue
        if not is_due(sched.schedule, now, sched.last_sent):
            outcomes.append(ReportOutcome(sched.tenant_id, sched.report_id, False, None, None, "not_due"))
            continue
        fire = most_recent_fire(sched.schedule, now)
        spec = spec_for(sched)
        context = context_for(sched)
        if spec is None or context is None:
            outcomes.append(
                ReportOutcome(sched.tenant_id, sched.report_id, False, fire, None, "not_ready")
            )
            continue
        report = render(spec, context, clock=lambda: now)
        content = render_in_format(report, sched.fmt)
        delivery = ReportDelivery(
            tenant_id=sched.tenant_id,
            report_id=sched.report_id,
            title=report.title,
            recipient=sched.recipient,
            fmt=sched.fmt,
            content=content,
            generated_at=report.generated_at,
            fire_time=fire,
        )
        receipt = sink.send(delivery)
        sched.last_sent = fire
        outcomes.append(ReportOutcome(sched.tenant_id, sched.report_id, True, fire, receipt))
    return outcomes


# --- persistence -------------------------------------------------------------
def schedule_to_dict(sched: ReportSchedule) -> dict[str, object]:
    """A JSON-safe dict for a :class:`ReportSchedule` (the persisted form)."""
    return {
        "tenant_id": sched.tenant_id,
        "report_id": sched.report_id,
        "recipient": sched.recipient,
        "fmt": sched.fmt,
        "active": sched.active,
        "last_sent": sched.last_sent.isoformat() if sched.last_sent is not None else None,
        "schedule": {
            "weekday": sched.schedule.weekday,
            "hour": sched.schedule.hour,
            "minute": sched.schedule.minute,
            "timezone": sched.schedule.timezone,
        },
    }


def schedule_from_dict(data: Mapping[str, object]) -> ReportSchedule:
    """Rebuild a :class:`ReportSchedule` from its persisted dict."""
    raw_sched = data.get("schedule")
    sm: Mapping[str, object] = raw_sched if isinstance(raw_sched, Mapping) else {}

    def _int(m: Mapping[str, object], k: str, default: int) -> int:
        v = m.get(k, default)
        return v if isinstance(v, int) else default

    def _str(m: Mapping[str, object], k: str, default: str) -> str:
        v = m.get(k, default)
        return v if isinstance(v, str) else default

    schedule = Schedule(
        weekday=_int(sm, "weekday", 0),
        hour=_int(sm, "hour", 8),
        minute=_int(sm, "minute", 0),
        timezone=_str(sm, "timezone", "America/Denver"),
    )
    last_raw = data.get("last_sent")
    last_sent = datetime.fromisoformat(last_raw) if isinstance(last_raw, str) else None
    active_raw = data.get("active", True)
    return ReportSchedule(
        tenant_id=_str(data, "tenant_id", ""),
        report_id=_str(data, "report_id", ""),
        recipient=_str(data, "recipient", ""),
        schedule=schedule,
        fmt=_str(data, "fmt", "pdf"),
        last_sent=last_sent,
        active=active_raw if isinstance(active_raw, bool) else True,
    )


class ReportScheduleStore(Protocol):
    """Per-tenant persistence for report schedules, plus a fleet-wide ``list_all``
    the scheduler job reads each tick."""

    def save(self, sched: ReportSchedule) -> None: ...
    def get(self, tenant_id: str, report_id: str) -> ReportSchedule | None: ...
    def list_for_tenant(self, tenant_id: str) -> list[ReportSchedule]: ...
    def list_all(self) -> list[ReportSchedule]: ...
    def delete(self, tenant_id: str, report_id: str) -> None: ...


class InMemoryReportScheduleStore:
    """Report schedules held in memory — for tests and local development.

    Returns the *stored* schedule objects (not copies), so a runner that advances
    ``last_sent`` in place persists that cursor without a re-save — matching how
    the briefing runtime mutates its subscriptions."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], ReportSchedule] = {}

    def save(self, sched: ReportSchedule) -> None:
        self._by_key[(sched.tenant_id, sched.report_id)] = sched

    def get(self, tenant_id: str, report_id: str) -> ReportSchedule | None:
        return self._by_key.get((tenant_id, report_id))

    def list_for_tenant(self, tenant_id: str) -> list[ReportSchedule]:
        return [s for (tid, _rid), s in sorted(self._by_key.items()) if tid == tenant_id]

    def list_all(self) -> list[ReportSchedule]:
        return [s for _key, s in sorted(self._by_key.items())]

    def delete(self, tenant_id: str, report_id: str) -> None:
        self._by_key.pop((tenant_id, report_id), None)


class _DbApiCursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def close(self) -> None: ...


class _DbApiConnection(Protocol):
    def cursor(self) -> _DbApiCursor: ...
    def commit(self) -> None: ...


class SqlReportScheduleStore:
    """Report schedules over any DB-API 2.0 connection; each schedule is stored as
    JSON text keyed by ``(tenant_id, report_id)``. ``placeholder`` is ``?``
    (sqlite) or ``%s`` (psycopg). Mirrors ``SqlSavedReportStore``."""

    def __init__(
        self,
        connection: _DbApiConnection,
        *,
        table: str = "rgnr8_report_schedule",
        placeholder: str = "?",
    ) -> None:
        self._conn = connection
        self._t = table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._t} "
                "(tenant_id TEXT NOT NULL, report_id TEXT NOT NULL, sched_json TEXT NOT NULL, "
                "PRIMARY KEY (tenant_id, report_id))"
            )
        finally:
            cur.close()
        self._conn.commit()

    def save(self, sched: ReportSchedule) -> None:
        p = self._ph
        payload = _dumps(schedule_to_dict(sched))
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._t} (tenant_id, report_id, sched_json) "
                f"VALUES ({p}, {p}, {p}) "
                "ON CONFLICT (tenant_id, report_id) DO UPDATE SET sched_json=excluded.sched_json",
                (sched.tenant_id, sched.report_id, payload),
            )
        finally:
            cur.close()
        self._conn.commit()

    def _rows(self, sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            cur.close()

    def get(self, tenant_id: str, report_id: str) -> ReportSchedule | None:
        p = self._ph
        rows = self._rows(
            f"SELECT sched_json FROM {self._t} WHERE tenant_id={p} AND report_id={p}",
            (tenant_id, report_id),
        )
        if not rows:
            return None
        return schedule_from_dict(_loads(str(rows[0][0])))

    def list_for_tenant(self, tenant_id: str) -> list[ReportSchedule]:
        rows = self._rows(
            f"SELECT sched_json FROM {self._t} WHERE tenant_id={self._ph} ORDER BY report_id",
            (tenant_id,),
        )
        return [schedule_from_dict(_loads(str(r[0]))) for r in rows]

    def list_all(self) -> list[ReportSchedule]:
        rows = self._rows(
            f"SELECT sched_json FROM {self._t} ORDER BY tenant_id, report_id", ()
        )
        return [schedule_from_dict(_loads(str(r[0]))) for r in rows]

    def delete(self, tenant_id: str, report_id: str) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"DELETE FROM {self._t} WHERE tenant_id={p} AND report_id={p}",
                (tenant_id, report_id),
            )
        finally:
            cur.close()
        self._conn.commit()


def _dumps(data: Mapping[str, object]) -> str:
    import json

    return json.dumps(data, sort_keys=True)


def _loads(text: str) -> Mapping[str, object]:
    import json

    obj = json.loads(text)
    if not isinstance(obj, Mapping):
        raise ValueError("stored report schedule is not a JSON object")
    return obj
