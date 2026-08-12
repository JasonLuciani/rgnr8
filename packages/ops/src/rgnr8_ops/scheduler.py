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

from rgnr8_runtime.subscriptions import DbApiConnection


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


# --------------------------------------------------------------------------- #
# Distributed coordination: at-most-one active runner across N workers.        #
#                                                                              #
# The `Dispatcher` above is single-process by design. To run the *same* job    #
# set from more than one worker safely, we put a lease (a mutually-exclusive,   #
# time-bounded lock) in front of a tick. Only the worker that currently holds  #
# the "dispatcher" lease runs the jobs on that tick; everyone else is a no-op   #
# for that window. Combined with each job's own durable cursor, this gives      #
# at-most-one-runner-per-tick without ever double-sending a briefing, even if   #
# a stale worker keeps ticking after a hand-off (its lease is expired, so it    #
# simply can't acquire). Everything takes an injected `now` and an explicit     #
# `holder` id, so a hand-off is fully deterministic and testable.               #
# --------------------------------------------------------------------------- #


class LeaseStore(Protocol):
    """A mutually-exclusive, time-bounded lock keyed by `name`.

    `acquire` grants the lease iff it is free, already expired, or already held
    by the *same* `holder` (re-entrant). `renew` extends the current holder's
    lease. `release` frees it. `now` is the injected clock; `ttl` is how long
    the grant is valid from `now`."""

    def acquire(self, name: str, holder: str, now: datetime, ttl: timedelta) -> bool: ...
    def renew(self, name: str, holder: str, now: datetime, ttl: timedelta) -> bool: ...
    def release(self, name: str, holder: str) -> None: ...


@dataclass(slots=True)
class _LeaseRecord:
    holder: str
    expires_at: datetime


class InMemoryLeaseStore:
    """A single-process `LeaseStore` for tests and single-worker deployments."""

    def __init__(self) -> None:
        self._leases: dict[str, _LeaseRecord] = {}

    def acquire(self, name: str, holder: str, now: datetime, ttl: timedelta) -> bool:
        rec = self._leases.get(name)
        if rec is None or rec.expires_at <= now or rec.holder == holder:
            self._leases[name] = _LeaseRecord(holder, now + ttl)
            return True
        return False

    def renew(self, name: str, holder: str, now: datetime, ttl: timedelta) -> bool:
        rec = self._leases.get(name)
        if rec is not None and rec.holder == holder:
            rec.expires_at = now + ttl
            return True
        return False

    def release(self, name: str, holder: str) -> None:
        rec = self._leases.get(name)
        if rec is not None and rec.holder == holder:
            del self._leases[name]


# Fixed-width serialization (always 6 microsecond digits) so lexical string
# comparison in SQL matches chronological order for naive UTC timestamps.
_TS_FMT = "%Y-%m-%dT%H:%M:%S.%f"


def _ser(dt: datetime) -> str:
    return dt.strftime(_TS_FMT)


class SqlLeaseStore:
    """A concurrency-safe `LeaseStore` over any DB-API 2.0 connection.

    The atomicity lives in a single conditional upsert: the `ON CONFLICT DO
    UPDATE ... WHERE` clause only overwrites the row when the existing lease is
    expired (`expires_at <= now`) or already owned by the caller. Under a race,
    the database serializes the two statements, so exactly one worker's upsert
    satisfies the predicate and wins; the loser's predicate is false and its row
    is left untouched. We read the row back to learn whether we are the holder.
    `placeholder` is the driver's marker (``"?"`` sqlite3, ``"%s"`` psycopg)."""

    def __init__(
        self,
        connection: DbApiConnection,
        *,
        table: str = "scheduler_lease",
        placeholder: str = "?",
    ) -> None:
        self._conn = connection
        self._table = table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._table} ("
                "name TEXT PRIMARY KEY, holder TEXT NOT NULL, expires_at TEXT NOT NULL)"
            )
        finally:
            cur.close()
        self._conn.commit()

    def _holder_is(self, name: str, holder: str) -> bool:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(f"SELECT holder FROM {self._table} WHERE name={p}", (name,))
            rows = cur.fetchall()
        finally:
            cur.close()
        return len(rows) == 1 and str(rows[0][0]) == holder

    def acquire(self, name: str, holder: str, now: datetime, ttl: timedelta) -> bool:
        p, t = self._ph, self._table
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {t} (name, holder, expires_at) VALUES ({p}, {p}, {p}) "
                "ON CONFLICT (name) DO UPDATE SET "
                "holder=excluded.holder, expires_at=excluded.expires_at "
                f"WHERE {t}.expires_at <= {p} OR {t}.holder = excluded.holder",
                (name, holder, _ser(now + ttl), _ser(now)),
            )
        finally:
            cur.close()
        self._conn.commit()
        return self._holder_is(name, holder)

    def renew(self, name: str, holder: str, now: datetime, ttl: timedelta) -> bool:
        p, t = self._ph, self._table
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"UPDATE {t} SET expires_at={p} WHERE name={p} AND holder={p}",
                (_ser(now + ttl), name, holder),
            )
        finally:
            cur.close()
        self._conn.commit()
        return self._holder_is(name, holder)

    def release(self, name: str, holder: str) -> None:
        p, t = self._ph, self._table
        cur = self._conn.cursor()
        try:
            cur.execute(f"DELETE FROM {t} WHERE name={p} AND holder={p}", (name, holder))
        finally:
            cur.close()
        self._conn.commit()


class LeasedDispatcher:
    """A distributed front-end to `Dispatcher`: at-most-one active runner.

    Each `tick(now, holder)` first tries to acquire the named lease. If it can't
    (another worker holds a live lease), it runs *nothing* and returns a single
    "skipped: not leader" marker. If it wins the lease, it delegates to a normal
    `Dispatcher` — so every job keeps its own durable cursor and failure
    isolation — then renews the lease to keep leadership for the next tick. The
    lease guarantees no two workers run the job set in the same window; the
    per-job cursors keep each job idempotent even across a leadership hand-off."""

    def __init__(
        self,
        jobs: list[Job],
        leases: LeaseStore,
        *,
        job_store: JobRunStore | None = None,
        lease_name: str = "dispatcher",
        ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        self._dispatcher = Dispatcher(jobs, job_store)
        self._leases = leases
        self._lease_name = lease_name
        self._ttl = ttl

    def tick(self, now: datetime, holder: str) -> list[JobResult]:
        if not self._leases.acquire(self._lease_name, holder, now, self._ttl):
            return [JobResult(self._lease_name, ran=False, detail="skipped: not leader")]
        results = self._dispatcher.tick(now)
        self._leases.renew(self._lease_name, holder, now, self._ttl)
        return results
