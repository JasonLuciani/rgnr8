from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from rgnr8_briefing import (
    FakeEmailTransport,
    ProviderDeliverer,
    Schedule,
    Subscription,
    build_briefing,
    build_envelope,
    is_due,
    most_recent_fire,
    next_fire,
    run_due,
)
from factory import healthy_forecast

MT = ZoneInfo("America/Denver")
# Monday 08:00 local
MON_8 = Schedule(weekday=0, hour=8, minute=0, timezone="America/Denver")


def test_most_recent_and_next_fire_are_inverse_around_now() -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=MT)  # Wednesday noon
    prev = most_recent_fire(MON_8, now)
    nxt = next_fire(MON_8, now)
    assert prev.weekday() == 0 and prev.hour == 8  # Monday 08:00
    assert prev == datetime(2026, 8, 3, 8, 0, tzinfo=MT)
    assert nxt == datetime(2026, 8, 10, 8, 0, tzinfo=MT)
    assert prev < now < nxt


def test_fire_computed_in_schedule_timezone_not_utc() -> None:
    # 2026-08-03 13:30 UTC == 07:30 America/Denver (MDT, UTC-6) — before the 08:00 local fire
    before = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
    assert most_recent_fire(MON_8, before) == datetime(2026, 7, 27, 8, 0, tzinfo=MT)
    # 2026-08-03 14:30 UTC == 08:30 local — after the fire
    after = datetime(2026, 8, 3, 14, 30, tzinfo=timezone.utc)
    assert most_recent_fire(MON_8, after) == datetime(2026, 8, 3, 8, 0, tzinfo=MT)


def test_is_due_catches_up_a_missed_tick_exactly_once() -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=MT)  # Wednesday; last fire was Mon 08-03 08:00
    # never sent -> due
    assert is_due(MON_8, now, last_sent=None) is True
    # sent before this week's fire -> due
    assert is_due(MON_8, now, last_sent=datetime(2026, 7, 27, 8, 0, tzinfo=MT)) is True
    # already sent for this week's fire -> not due
    assert is_due(MON_8, now, last_sent=datetime(2026, 8, 3, 8, 0, tzinfo=MT)) is False


def test_naive_datetime_is_rejected() -> None:
    try:
        most_recent_fire(MON_8, datetime(2026, 8, 5, 12, 0))
    except ValueError as e:
        assert "timezone-aware" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for naive datetime")


def test_run_due_sends_only_due_subs_and_advances_cadence() -> None:
    email = FakeEmailTransport()
    deliverer = ProviderDeliverer(email)
    subs = [
        Subscription("acme", "owner@acme.com", MON_8, last_sent=None),
        Subscription("beta", "owner@beta.com", MON_8, last_sent=datetime(2026, 8, 3, 8, 0, tzinfo=MT)),
    ]

    def envelope_for(sub: Subscription):  # type: ignore[no-untyped-def]
        return build_envelope(build_briefing(healthy_forecast()), sub.recipient, tenant_id=sub.tenant_id)

    now = datetime(2026, 8, 5, 12, 0, tzinfo=MT)
    outcomes = run_due(subs, now, envelope_for, deliverer)

    fired = {o.tenant_id: o for o in outcomes}
    assert fired["acme"].fired is True
    assert fired["beta"].fired is False and fired["beta"].skipped_reason == "not_due"
    # exactly one email sent (acme), and its cadence advanced to this week's fire
    assert len(email.sent) == 1
    assert subs[0].last_sent == datetime(2026, 8, 3, 8, 0, tzinfo=MT)

    # running again at the same now is now idempotent — nothing new goes out
    outcomes2 = run_due(subs, now, envelope_for, deliverer)
    assert all(o.fired is False for o in outcomes2)
    assert len(email.sent) == 1


def test_run_due_skips_when_envelope_unavailable() -> None:
    email = FakeEmailTransport()
    deliverer = ProviderDeliverer(email)
    subs = [Subscription("acme", "owner@acme.com", MON_8, last_sent=None)]
    now = datetime(2026, 8, 5, 12, 0, tzinfo=MT)
    outcomes = run_due(subs, now, lambda _s: None, deliverer)
    assert outcomes[0].fired is False
    assert outcomes[0].skipped_reason == "no_envelope"
    assert subs[0].last_sent is None  # not advanced — it must retry next run
    assert len(email.sent) == 0
