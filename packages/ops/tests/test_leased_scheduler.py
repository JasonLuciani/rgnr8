"""Distributed coordination: a lease guarantees at-most-one active runner.

Multiple workers run the same job set; only the lease holder runs on a tick, so
a job's side effect happens once per window no matter how many workers tick. When
the lease expires, another worker can take over. The SqlLeaseStore is the
concurrency-safe backing lock (conditional upsert: exactly one racer wins).
"""

import sqlite3
from datetime import datetime, timedelta

from rgnr8_ops import (
    InMemoryLeaseStore,
    IntervalJob,
    LeasedDispatcher,
    SqlLeaseStore,
)

T0 = datetime(2026, 9, 2, 8, 0)
TTL = timedelta(minutes=5)


def _counter_job(counter: list[int]) -> IntervalJob:
    # runs every tick (interval 0); increments a shared counter as its side effect
    def bump(now: datetime) -> str:
        counter.append(1)
        return "bumped"

    return IntervalJob("counter", timedelta(0), bump)


def test_single_worker_acquires_lease_and_runs_jobs() -> None:
    counter: list[int] = []
    leases = InMemoryLeaseStore()
    w = LeasedDispatcher([_counter_job(counter)], leases, ttl=TTL)

    results = w.tick(T0, holder="worker-a")
    assert [r.name for r in results] == ["counter"]
    assert results[0].ran is True
    assert sum(counter) == 1


def test_second_worker_in_same_window_is_not_leader_and_runs_nothing() -> None:
    counter: list[int] = []
    leases = InMemoryLeaseStore()
    # Two workers share the SAME lease store and the SAME job-side-effect target.
    a = LeasedDispatcher([_counter_job(counter)], leases, ttl=TTL)
    b = LeasedDispatcher([_counter_job(counter)], leases, ttl=TTL)

    ra = a.tick(T0, holder="worker-a")           # A wins the lease, runs
    rb = b.tick(T0 + timedelta(seconds=10), holder="worker-b")  # same window, B loses

    assert ra[0].ran is True
    assert rb == [type(rb[0])("dispatcher", ran=False, detail="skipped: not leader")]
    assert all(r.ran is False for r in rb)
    # the counter incremented exactly ONCE across both workers in this window
    assert sum(counter) == 1


def test_lease_holder_keeps_leadership_across_consecutive_ticks() -> None:
    counter: list[int] = []
    leases = InMemoryLeaseStore()
    a = LeasedDispatcher([_counter_job(counter)], leases, ttl=TTL)
    b = LeasedDispatcher([_counter_job(counter)], leases, ttl=TTL)

    a.tick(T0, holder="worker-a")
    # B keeps losing while A's (renewed) lease stays live
    b.tick(T0 + timedelta(minutes=1), holder="worker-b")
    a.tick(T0 + timedelta(minutes=2), holder="worker-a")
    b.tick(T0 + timedelta(minutes=3), holder="worker-b")

    assert sum(counter) == 2  # only A's two ticks ran


def test_after_ttl_expires_another_worker_takes_over_and_runs() -> None:
    counter: list[int] = []
    leases = InMemoryLeaseStore()
    a = LeasedDispatcher([_counter_job(counter)], leases, ttl=TTL)
    b = LeasedDispatcher([_counter_job(counter)], leases, ttl=TTL)

    a.tick(T0, holder="worker-a")               # A runs, lease valid until T0+5m
    # B tries after the lease has expired -> B becomes leader and runs
    rb = b.tick(T0 + timedelta(minutes=6), holder="worker-b")
    assert rb[0].ran is True
    assert sum(counter) == 2                    # A once, then B once after hand-off


# --------------------------------------------------------------------------- #
# SqlLeaseStore: the concurrency-safe backing lock.                            #
# --------------------------------------------------------------------------- #


def _sql_store() -> SqlLeaseStore:
    conn = sqlite3.connect(":memory:")
    store = SqlLeaseStore(conn)
    store.create_schema()
    return store


def test_sql_two_holders_race_for_acquire_exactly_one_wins() -> None:
    store = _sql_store()
    won_a = store.acquire("dispatcher", "a", T0, TTL)
    won_b = store.acquire("dispatcher", "b", T0 + timedelta(seconds=1), TTL)
    assert won_a is True
    assert won_b is False  # A holds a live lease; B cannot take it


def test_sql_expired_lease_can_be_reacquired_by_another_holder() -> None:
    store = _sql_store()
    assert store.acquire("dispatcher", "a", T0, TTL) is True
    # still live for B before expiry
    assert store.acquire("dispatcher", "b", T0 + timedelta(minutes=1), TTL) is False
    # expired -> B wins
    assert store.acquire("dispatcher", "b", T0 + timedelta(minutes=6), TTL) is True
    # now A is the outsider and cannot take B's live lease
    assert store.acquire("dispatcher", "a", T0 + timedelta(minutes=7), TTL) is False


def test_sql_reacquire_by_same_holder_is_reentrant() -> None:
    store = _sql_store()
    assert store.acquire("dispatcher", "a", T0, TTL) is True
    assert store.acquire("dispatcher", "a", T0 + timedelta(seconds=30), TTL) is True


def test_sql_renew_extends_the_lease() -> None:
    store = _sql_store()
    assert store.acquire("dispatcher", "a", T0, TTL) is True
    # renew at T0+4m pushes expiry to T0+9m
    assert store.renew("dispatcher", "a", T0 + timedelta(minutes=4), TTL) is True
    # at T0+6m the un-renewed lease would have expired, but renew kept it live
    assert store.acquire("dispatcher", "b", T0 + timedelta(minutes=6), TTL) is False
    # a non-holder cannot renew
    assert store.renew("dispatcher", "b", T0 + timedelta(minutes=6), TTL) is False


def test_sql_release_frees_the_lease_for_others() -> None:
    store = _sql_store()
    assert store.acquire("dispatcher", "a", T0, TTL) is True
    # a non-holder cannot release it
    store.release("dispatcher", "b")
    assert store.acquire("dispatcher", "b", T0 + timedelta(seconds=1), TTL) is False
    # the holder releases -> immediately free even though TTL hasn't elapsed
    store.release("dispatcher", "a")
    assert store.acquire("dispatcher", "b", T0 + timedelta(seconds=2), TTL) is True


def test_sql_backed_leased_dispatcher_dedupes_across_two_workers() -> None:
    counter: list[int] = []
    store = _sql_store()
    a = LeasedDispatcher([_counter_job(counter)], store, ttl=TTL)
    b = LeasedDispatcher([_counter_job(counter)], store, ttl=TTL)

    a.tick(T0, holder="worker-a")
    b.tick(T0 + timedelta(seconds=5), holder="worker-b")
    assert sum(counter) == 1  # only the SQL-lease holder ran
