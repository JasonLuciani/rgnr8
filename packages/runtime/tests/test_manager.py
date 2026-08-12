"""Subscription management surface + pause semantics."""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from rgnr8_briefing import Schedule, Subscription, run_due
from rgnr8_runtime import (
    InMemorySubscriptionStore,
    SqlSubscriptionStore,
    SubscriptionManager,
)

MT = ZoneInfo("America/Denver")
SCHED = Schedule(weekday=0, hour=8, minute=0, timezone="America/Denver")


def test_add_list_pause_resume_remove() -> None:
    mgr = SubscriptionManager(InMemorySubscriptionStore())
    mgr.add("acme", "owner@acme.com", SCHED)
    mgr.add("acme", "cfo@acme.com", SCHED)
    mgr.add("bright", "owner@bright.com", SCHED)

    assert [s.recipient for s in mgr.list("acme")] == ["cfo@acme.com", "owner@acme.com"]
    assert len(mgr.list()) == 3

    mgr.pause("acme", "cfo@acme.com")
    assert mgr._find("acme", "cfo@acme.com").active is False  # type: ignore[union-attr]
    mgr.resume("acme", "cfo@acme.com")
    assert mgr._find("acme", "cfo@acme.com").active is True  # type: ignore[union-attr]

    mgr.remove("acme", "cfo@acme.com")
    assert [s.recipient for s in mgr.list("acme")] == ["owner@acme.com"]


def test_paused_subscription_does_not_fire() -> None:
    subs = [
        Subscription("acme", "owner@acme.com", SCHED, last_sent=None, active=False),
    ]
    now = datetime(2026, 9, 2, 12, 0, tzinfo=MT)  # a Wednesday; Monday fire has passed

    sent: list[str] = []

    class Rec:
        def send(self, envelope: object, at: str) -> object:  # minimal Deliverer
            sent.append("x")
            return object()

    outcomes = run_due(subs, now, lambda s: object(), Rec(), at="2026-09-02T12:00:00")  # type: ignore[arg-type,return-value]
    assert outcomes[0].fired is False
    assert outcomes[0].skipped_reason == "paused"
    assert sent == []  # nothing delivered
    assert subs[0].last_sent is None  # cursor untouched


def test_pause_survives_a_restart_via_sql() -> None:
    conn = sqlite3.connect(":memory:")
    store = SqlSubscriptionStore(conn)
    store.create_schema()
    mgr = SubscriptionManager(store)
    mgr.add("acme", "owner@acme.com", SCHED)
    mgr.pause("acme", "owner@acme.com")

    # reopen over the same connection
    back = SubscriptionManager(SqlSubscriptionStore(conn))
    sub = back._find("acme", "owner@acme.com")  # type: ignore[union-attr]
    assert sub is not None and sub.active is False
