"""The production scheduler: cadence, idempotency, and failure isolation."""

from datetime import datetime, timedelta

from rgnr8_ops import (
    BriefingDispatchJob,
    Dispatcher,
    InMemoryJobRunStore,
    IntervalJob,
)

T0 = datetime(2026, 9, 2, 8, 0)


def test_interval_job_fires_once_per_interval() -> None:
    calls: list[datetime] = []
    job = IntervalJob("sync", timedelta(hours=1), lambda now: (calls.append(now), "synced")[1])
    d = Dispatcher([job], InMemoryJobRunStore())

    assert d.tick(T0)[0].ran is True                       # first tick runs
    assert d.tick(T0 + timedelta(minutes=30))[0].ran is False  # not due yet
    assert d.tick(T0 + timedelta(minutes=61))[0].ran is True   # interval elapsed
    assert len(calls) == 2


def test_failure_is_isolated_and_other_jobs_still_run() -> None:
    def boom(now: datetime) -> str:
        raise RuntimeError("provider down")

    good: list[datetime] = []
    d = Dispatcher([
        IntervalJob("bad", timedelta(minutes=1), boom),
        IntervalJob("good", timedelta(minutes=1), lambda now: (good.append(now), "ok")[1]),
    ], InMemoryJobRunStore())

    results = {r.name: r for r in d.tick(T0)}
    assert results["bad"].ran is False and "provider down" in results["bad"].error
    assert results["good"].ran is True and good == [T0]


def test_failed_job_retries_next_tick() -> None:
    attempts: list[datetime] = []

    def flaky(now: datetime) -> str:
        attempts.append(now)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return "recovered"

    d = Dispatcher([IntervalJob("j", timedelta(hours=1), flaky)], InMemoryJobRunStore())
    assert d.tick(T0)[0].ran is False           # failed, cursor NOT advanced
    assert d.tick(T0 + timedelta(seconds=5))[0].ran is True  # retried immediately
    assert len(attempts) == 2


def test_briefing_dispatch_job_delegates_to_runtime_tick() -> None:
    seen: list[datetime] = []

    def fake_tick(now: datetime) -> list[object]:
        seen.append(now)
        return ["outcome-a", "outcome-b"]  # 2 delivered

    d = Dispatcher([BriefingDispatchJob("briefings", fake_tick)], InMemoryJobRunStore())
    r = d.tick(T0)[0]
    assert r.ran is True and "2 briefing(s) delivered" in r.detail
    assert seen == [T0]
    # runs every tick (runtime decides real dueness)
    d.tick(T0 + timedelta(minutes=1))
    assert len(seen) == 2
