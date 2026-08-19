import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from factory import tenant_source
from rgnr8_briefing import RecordingDeliverer, Schedule, Subscription
from rgnr8_runtime import DeliveryRuntime, SqlSubscriptionStore, serve

MT = ZoneInfo("America/Denver")
MON_8 = Schedule(weekday=0, hour=8, minute=0, timezone="America/Denver")
WED = datetime(2026, 8, 5, 12, 0, tzinfo=MT)
NEXT_WED = datetime(2026, 8, 12, 12, 0, tzinfo=MT)


def _store(conn: sqlite3.Connection) -> SqlSubscriptionStore:
    # sqlite3.Connection is a valid DB-API 2.0 connection; mypy can't prove the
    # Protocol match against its overloaded cursor(), so narrow here once.
    return SqlSubscriptionStore(conn)  # type: ignore[arg-type]


def test_sql_store_roundtrips_subscriptions_including_last_sent(tmp_path: Path) -> None:
    db = f"{tmp_path}/subs.db"
    conn = sqlite3.connect(db)
    store = _store(conn)
    store.create_schema()
    store.save(Subscription("bright", "o@bright.com", MON_8, last_sent=None))

    # deliver once; the runtime advances + persists last_sent
    d1 = RecordingDeliverer()
    DeliveryRuntime(store, tenant_source(), d1).tick(WED)
    assert len(d1.sent) == 1
    conn.close()

    # reopen the DB (process restart): last_sent persisted, so no re-send this week
    conn2 = sqlite3.connect(db)
    store2 = _store(conn2)
    reloaded = store2.list()[0]
    assert reloaded.last_sent == datetime(2026, 8, 3, 8, 0, tzinfo=MT)

    d2 = RecordingDeliverer()
    DeliveryRuntime(store2, tenant_source(), d2).tick(WED)
    assert len(d2.sent) == 0
    # but next week's fire does send
    d3 = RecordingDeliverer()
    DeliveryRuntime(store2, tenant_source(), d3).tick(NEXT_WED)
    assert len(d3.sent) == 1
    conn2.close()


def test_serve_loop_runs_a_bounded_number_of_ticks_with_injected_clock() -> None:
    conn = sqlite3.connect(":memory:")
    store = _store(conn)
    store.create_schema()
    store.save(Subscription("bright", "o@bright.com", MON_8, last_sent=None))
    deliverer = RecordingDeliverer()
    rt = DeliveryRuntime(store, tenant_source(), deliverer)

    # a clock that jumps a week each tick; sleep is a no-op
    times = [WED, NEXT_WED, NEXT_WED]
    calls = {"i": 0}

    def clock() -> datetime:
        t = times[min(calls["i"], len(times) - 1)]
        calls["i"] += 1
        return t

    serve(rt, interval_seconds=0, now=clock, sleep=lambda _s: None, max_ticks=3)
    # fired on WED and again on NEXT_WED (a new fire), but the 3rd tick (same NEXT_WED) is idempotent
    assert len(deliverer.sent) == 2
    conn.close()
