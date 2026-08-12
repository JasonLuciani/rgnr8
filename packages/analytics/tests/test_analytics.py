"""Product-analytics seam — event shape, sink queries, HTTP envelope, funnel math.

All time is injected (`lambda: 1000.0`, or a stepping fake) so every assertion is
exact and deterministic; no test reads the wall clock.
"""

from __future__ import annotations

from typing import Callable

from rgnr8_analytics import (
    EventName,
    EventTracker,
    HttpEventSink,
    InMemoryEventSink,
    ProductEvent,
    funnel_report,
)

FIXED = 1000.0


def _stepping_clock(values: list[float]) -> Callable[[], float]:
    """A clock that returns successive `values` on each call."""
    it = iter(values)
    return lambda: next(it)


class _RecordingHttpClient:
    """Captures each post_json call as a tuple for assertions (no network)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], dict[str, str]]] = []

    def post_json(
        self, url: str, body: dict[str, object], headers: dict[str, str]
    ) -> None:
        self.calls.append((url, body, headers))


# --------------------------------------------------------------------------- #
# tracker: shape, timestamp, default props
# --------------------------------------------------------------------------- #


def test_track_stamps_clock_and_builds_event_shape() -> None:
    sink = InMemoryEventSink()
    tracker = EventTracker(clock=lambda: FIXED, sink=sink)
    event = tracker.track(
        EventName.SIGNUP_STARTED,
        distinct_id="u_1",
        tenant_id="t_1",
        account_id="a_1",
        plan="pro",
    )
    assert event == ProductEvent(
        name=EventName.SIGNUP_STARTED,
        at=FIXED,
        distinct_id="u_1",
        tenant_id="t_1",
        account_id="a_1",
        props={"plan": "pro"},
    )
    # emitted through the sink, not just returned
    assert sink.events == [event]


def test_track_uses_injected_clock_per_call() -> None:
    sink = InMemoryEventSink()
    tracker = EventTracker(clock=_stepping_clock([10.0, 20.0]), sink=sink)
    tracker.signup_started(distinct_id="u_1")
    tracker.signup_completed(distinct_id="u_1")
    assert [e.at for e in sink.events] == [10.0, 20.0]


def test_default_props_merge_under_call_props() -> None:
    sink = InMemoryEventSink()
    tracker = EventTracker(
        clock=lambda: FIXED,
        sink=sink,
        default_props={"env": "prod", "app_version": "1.4.0"},
    )
    event = tracker.track(
        EventName.CHECKOUT_COMPLETED,
        distinct_id="u_2",
        app_version="1.5.0",  # call-site key wins
        amount_cents=4900,
    )
    assert event.props == {
        "env": "prod",
        "app_version": "1.5.0",
        "amount_cents": 4900,
    }


def test_tracker_defaults_to_in_memory_sink() -> None:
    tracker = EventTracker(clock=lambda: FIXED)
    tracker.signup_started(distinct_id="u_1")
    assert isinstance(tracker.sink, InMemoryEventSink)
    assert tracker.sink.events[0].name == EventName.SIGNUP_STARTED


def test_funnel_helpers_emit_the_right_names() -> None:
    sink = InMemoryEventSink()
    tracker = EventTracker(clock=lambda: FIXED, sink=sink)
    tracker.signup_started(distinct_id="u")
    tracker.signup_completed(distinct_id="u")
    tracker.email_verified(distinct_id="u")
    tracker.checkout_started(distinct_id="u")
    tracker.checkout_completed(distinct_id="u")
    tracker.account_activated(distinct_id="u")
    tracker.tenant_onboarded(distinct_id="u")
    tracker.connector_connected(distinct_id="u")
    tracker.first_forecast_viewed(distinct_id="u")
    tracker.briefing_delivered(distinct_id="u")
    tracker.cfo_question_asked(distinct_id="u")
    tracker.close_sealed(distinct_id="u")
    tracker.subscription_canceled(distinct_id="u")
    assert [e.name for e in sink.events] == [
        EventName.SIGNUP_STARTED,
        EventName.SIGNUP_COMPLETED,
        EventName.EMAIL_VERIFIED,
        EventName.CHECKOUT_STARTED,
        EventName.CHECKOUT_COMPLETED,
        EventName.ACCOUNT_ACTIVATED,
        EventName.TENANT_ONBOARDED,
        EventName.CONNECTOR_CONNECTED,
        EventName.FIRST_FORECAST_VIEWED,
        EventName.BRIEFING_DELIVERED,
        EventName.CFO_QUESTION_ASKED,
        EventName.CLOSE_SEALED,
        EventName.SUBSCRIPTION_CANCELED,
    ]


def test_helper_passes_tenancy_and_props_through() -> None:
    sink = InMemoryEventSink()
    tracker = EventTracker(clock=lambda: FIXED, sink=sink)
    tracker.connector_connected(
        distinct_id="u_9", tenant_id="t_9", account_id="a_9", provider="qbo"
    )
    event = sink.events[0]
    assert event.tenant_id == "t_9"
    assert event.account_id == "a_9"
    assert event.props == {"provider": "qbo"}


# --------------------------------------------------------------------------- #
# InMemory sink queries
# --------------------------------------------------------------------------- #


def test_in_memory_query_by_name_and_distinct_id() -> None:
    sink = InMemoryEventSink()
    tracker = EventTracker(clock=lambda: FIXED, sink=sink)
    tracker.signup_started(distinct_id="u_1")
    tracker.signup_started(distinct_id="u_2")
    tracker.signup_completed(distinct_id="u_1")

    started = sink.by_name(EventName.SIGNUP_STARTED)
    assert [e.distinct_id for e in started] == ["u_1", "u_2"]

    u1 = sink.by_distinct_id("u_1")
    assert [e.name for e in u1] == [
        EventName.SIGNUP_STARTED,
        EventName.SIGNUP_COMPLETED,
    ]

    assert sink.by_name(EventName.CLOSE_SEALED) == []
    assert sink.by_distinct_id("nobody") == []


# --------------------------------------------------------------------------- #
# HttpEventSink: capture envelope shape
# --------------------------------------------------------------------------- #


def test_http_sink_posts_capture_envelope() -> None:
    client = _RecordingHttpClient()
    sink = HttpEventSink(
        client, url="https://capture.example/v1/track", write_key="wk_test"
    )
    tracker = EventTracker(clock=lambda: FIXED, sink=sink)
    tracker.checkout_completed(
        distinct_id="u_1",
        tenant_id="t_1",
        account_id="a_1",
        amount_cents=4900,
    )

    assert len(client.calls) == 1
    url, body, headers = client.calls[0]
    assert url == "https://capture.example/v1/track"
    assert headers == {"Content-Type": "application/json"}
    assert body == {
        "write_key": "wk_test",
        "event": "checkout_completed",
        "timestamp": FIXED,
        "distinct_id": "u_1",
        "tenant_id": "t_1",
        "account_id": "a_1",
        "properties": {"amount_cents": 4900},
    }


def test_http_sink_event_name_is_bare_wire_string() -> None:
    client = _RecordingHttpClient()
    sink = HttpEventSink(client, url="https://x/track", write_key="wk")
    tracker = EventTracker(clock=lambda: FIXED, sink=sink)
    tracker.signup_started(distinct_id="u")
    _, body, _ = client.calls[0]
    assert body["event"] == "signup_started"


# --------------------------------------------------------------------------- #
# funnel_report: counts + conversion percentages
# --------------------------------------------------------------------------- #

STEPS = [
    EventName.SIGNUP_STARTED,
    EventName.SIGNUP_COMPLETED,
    EventName.ACCOUNT_ACTIVATED,
]


def _multi_user_fixture() -> list[ProductEvent]:
    """100 signup_started -> 60 signup_completed -> 30 account_activated."""
    tracker = EventTracker(clock=lambda: FIXED, sink=InMemoryEventSink())
    for i in range(100):
        tracker.signup_started(distinct_id=f"u_{i}")
    for i in range(60):
        tracker.signup_completed(distinct_id=f"u_{i}")
    for i in range(30):
        tracker.account_activated(distinct_id=f"u_{i}")
    assert isinstance(tracker.sink, InMemoryEventSink)
    return tracker.sink.events


def test_funnel_report_counts_and_conversion() -> None:
    report = funnel_report(_multi_user_fixture(), STEPS)
    assert report == [
        (EventName.SIGNUP_STARTED, 100, 100.0),
        (EventName.SIGNUP_COMPLETED, 60, 60.0),
        (EventName.ACCOUNT_ACTIVATED, 30, 30.0),
    ]


def test_funnel_report_dedupes_repeat_events_per_user() -> None:
    sink = InMemoryEventSink()
    tracker = EventTracker(clock=lambda: FIXED, sink=sink)
    # one user firing signup_started three times still counts once
    tracker.signup_started(distinct_id="u_1")
    tracker.signup_started(distinct_id="u_1")
    tracker.signup_started(distinct_id="u_1")
    tracker.signup_completed(distinct_id="u_1")
    report = funnel_report(sink.events, STEPS)
    assert report == [
        (EventName.SIGNUP_STARTED, 1, 100.0),
        (EventName.SIGNUP_COMPLETED, 1, 100.0),
        (EventName.ACCOUNT_ACTIVATED, 0, 0.0),
    ]


def test_funnel_report_rounds_to_two_decimals() -> None:
    sink = InMemoryEventSink()
    tracker = EventTracker(clock=lambda: FIXED, sink=sink)
    for i in range(3):
        tracker.signup_started(distinct_id=f"u_{i}")
    tracker.signup_completed(distinct_id="u_0")  # 1/3 = 33.33%
    report = funnel_report(sink.events, STEPS)
    assert report[1] == (EventName.SIGNUP_COMPLETED, 1, 33.33)


def test_funnel_report_empty_events_and_empty_steps() -> None:
    assert funnel_report([], STEPS) == [
        (EventName.SIGNUP_STARTED, 0, 0.0),
        (EventName.SIGNUP_COMPLETED, 0, 0.0),
        (EventName.ACCOUNT_ACTIVATED, 0, 0.0),
    ]
    assert funnel_report(_multi_user_fixture(), []) == []
