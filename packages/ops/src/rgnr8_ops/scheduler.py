"""The production scheduler — what actually fires the recurring work in prod.

The delivery *logic* (who's due, deliver + advance the cursor) lives in
`DeliveryRuntime.tick`; the connector sync logic lives in `@rgnr8/connectors`.
What was missing is the thing that *drives* them on a cadence in production. The
`Dispatcher` is it: a small, deterministic job runner that a single worker process
calls (`worker.py`) on a fixed interval (e.g. every minute). Each registered job
decides whether it's due from `now` + its own last-run cursor (kept in a durable
`JobRunStore`), runs at most once per due window, and is isolated — one job
throwing never stops the others. `now` is injected, so a tick is fully testable.

This is a *single-process* scheduler by design (one worker, idempotent jobs). It
scales to the beta fleet; the same `Dispatcher` API backs a queue/lease-based
distributed runner later without callers changing (see the architecture note).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


@dataclass(frozen=True, slots=True)
class JobResult:
    name: str
    ran: bool
    detail: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.error == ""


class Job(Protocol):
    name: str

    def due(self, now: datetime, last_run: datetime | None) -> bool: ...
    def run(self, now: datetime) -> str: ...


class JobRunStore(Protocol):
    def last_run(self, job: str) -> datetime | None: ...
    def mark_run(self, job: str, at: datetime) -> None: ...


class InMemoryJobRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, datetime] = {}

    def last_run(self, job: str) -> datetime | None:
        return self._runs.get(job)

    def mark_run(self, job: str, at: datetime) -> None:
        self._runs[job] = at


@dataclass(frozen=True, slots=True)
class IntervalJob:
    """A job that runs at most once per `interval`. `fn(now)` returns a one-line
    detail string for the dispatch report."""

    name: str
    interval: timedelta
    fn: Callable[[datetime], str]

    def due(self, now: datetime, last_run: datetime | None) -> bool:
        return last_run is None or (now - last_run) >= self.interval

    def run(self, now: datetime) -> str:
        return self.fn(now)


@dataclass(frozen=True, slots=True)
class BriefingDispatchJob:
    """Wraps `DeliveryRuntime.tick` — the per-subscription schedule already decides
    dueness, so this job runs every dispatcher tick and lets the runtime fire only
    the subscriptions that are actually due (and advance their cursors)."""

    name: str
    tick: Callable[[datetime], list[object]]

    def due(self, now: datetime, last_run: datetime | None) -> bool:
        return True  # the runtime's own per-sub cursor decides what actually sends

    def run(self, now: datetime) -> str:
        outcomes = self.tick(now)
        return f"{len(outcomes)} briefing(s) delivered"


class Dispatcher:
    """Runs registered jobs on each `tick(now)`. Durable last-run cursors make
    each job idempotent across worker restarts; per-job try/except isolates
    failures so one bad job can't wedge the others."""

    def __init__(self, jobs: list[Job], store: JobRunStore | None = None) -> None:
        self._jobs = jobs
        self._store: JobRunStore = store if store is not None else InMemoryJobRunStore()

    def tick(self, now: datetime) -> list[JobResult]:
        results: list[JobResult] = []
        for job in self._jobs:
            last = self._store.last_run(job.name)
            if not job.due(now, last):
                results.append(JobResult(job.name, ran=False, detail="not due"))
                continue
            try:
                detail = job.run(now)
            except Exception as exc:  # isolate: a failing job never stops the rest
                results.append(JobResult(job.name, ran=False, error=f"{type(exc).__name__}: {exc}"))
                continue
            self._store.mark_run(job.name, now)
            results.append(JobResult(job.name, ran=True, detail=detail))
        return results
