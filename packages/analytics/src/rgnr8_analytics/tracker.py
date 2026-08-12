"""Event-tracking seam — build a `ProductEvent` and hand it to a sink.

The `EventTracker` is the single call site the rest of RGNR8 uses to record a
product event: it stamps the injected clock, merges standing default props, and
forwards a fully-formed `ProductEvent` to an `EventSink`. The sink is the seam —
production binds an `HttpEventSink` that POSTs to a Segment/PostHog-style capture
endpoint; tests bind an `InMemoryEventSink` and assert on exactly what was
recorded.

Everything here is deterministic. Time is an injected `Callable[[], float]`
returning epoch seconds — we never read the wall clock, so a test pins every
event's `at` to a constant. There is no randomness and no implicit id generation;
the caller supplies `distinct_id`. The thin funnel helpers (`signup_started`,
`checkout_completed`, …) exist so instrumentation at the request/lifecycle
boundaries reads as one obvious line and can't misspell an event name.
"""

from __future__ import annotations

from typing import Callable, Protocol

from .events import EventName, ProductEvent


class EventSink(Protocol):
    def emit(self, event: ProductEvent) -> None: ...


class InMemoryEventSink:
    """Records every event for assertions/local runs; queryable by name/identity."""

    def __init__(self) -> None:
        self.events: list[ProductEvent] = []

    def emit(self, event: ProductEvent) -> None:
        self.events.append(event)

    def by_name(self, name: EventName) -> list[ProductEvent]:
        """Every recorded event with the given catalog name, in emit order."""
        return [e for e in self.events if e.name == name]

    def by_distinct_id(self, distinct_id: str) -> list[ProductEvent]:
        """Every recorded event for the given identity, in emit order."""
        return [e for e in self.events if e.distinct_id == distinct_id]


class HttpClient(Protocol):
    def post_json(
        self, url: str, body: dict[str, object], headers: dict[str, str]
    ) -> None: ...


class HttpEventSink:
    """POSTs each event as JSON to a capture endpoint over an injected client.

    Shape-correct only — no network. `client.post_json` is where a real HTTP
    client would ship bytes; here it lets tests capture the exact URL, body, and
    headers. The body is a Segment/PostHog-style capture envelope: a `write_key`
    plus the flat event dict from `ProductEvent.to_dict`. `Content-Type` is set to
    JSON; wiring a real client turns it live.
    """

    def __init__(
        self,
        client: HttpClient,
        *,
        url: str,
        write_key: str,
    ) -> None:
        self._client = client
        self._url = url
        self._write_key = write_key

    def emit(self, event: ProductEvent) -> None:
        body: dict[str, object] = {"write_key": self._write_key}
        body.update(event.to_dict())
        headers = {"Content-Type": "application/json"}
        self._client.post_json(self._url, body, headers)


class EventTracker:
    """Stamps + merges + forwards product events through an injected sink + clock."""

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        sink: EventSink | None = None,
        default_props: dict[str, object] | None = None,
    ) -> None:
        self._clock = clock
        self._sink: EventSink = sink if sink is not None else InMemoryEventSink()
        self._default_props: dict[str, object] = dict(default_props or {})

    @property
    def sink(self) -> EventSink:
        return self._sink

    def track(
        self,
        name: EventName,
        *,
        distinct_id: str,
        tenant_id: str = "",
        account_id: str = "",
        **props: object,
    ) -> ProductEvent:
        """Record one event; returns the `ProductEvent` that was emitted.

        `at` is stamped from the injected clock. Default props merge underneath
        per-call props, so a call-site key of the same name wins.
        """
        merged = {**self._default_props, **props}
        event = ProductEvent(
            name=name,
            at=self._clock(),
            distinct_id=distinct_id,
            tenant_id=tenant_id,
            account_id=account_id,
            props=merged,
        )
        self._sink.emit(event)
        return event

    # --- thin funnel helpers -------------------------------------------------- #
    # One obvious line per lifecycle boundary; the name can't be misspelled.

    def signup_started(
        self, *, distinct_id: str, tenant_id: str = "", account_id: str = "", **props: object
    ) -> ProductEvent:
        return self.track(
            EventName.SIGNUP_STARTED,
            distinct_id=distinct_id,
            tenant_id=tenant_id,
            account_id=account_id,
            **props,
        )

    def signup_completed(
        self, *, distinct_id: str, tenant_id: str = "", account_id: str = "", **props: object
    ) -> ProductEvent:
        return self.track(
            EventName.SIGNUP_COMPLETED,
            distinct_id=distinct_id,
            tenant_id=tenant_id,
            account_id=account_id,
            **props,
        )

    def email_verified(
        self, *, distinct_id: str, tenant_id: str = "", account_id: str = "", **props: object
    ) -> ProductEvent:
        return self.track(
            EventName.EMAIL_VERIFIED,
            distinct_id=distinct_id,
            tenant_id=tenant_id,
            account_id=account_id,
            **props,
        )

    def checkout_started(
        self, *, distinct_id: str, tenant_id: str = "", account_id: str = "", **props: object
    ) -> ProductEvent:
        return self.track(
            EventName.CHECKOUT_STARTED,
            distinct_id=distinct_id,
            tenant_id=tenant_id,
            account_id=account_id,
            **props,
        )

    def checkout_completed(
        self, *, distinct_id: str, tenant_id: str = "", account_id: str = "", **props: object
    ) -> ProductEvent:
        return self.track(
            EventName.CHECKOUT_COMPLETED,
            distinct_id=distinct_id,
            tenant_id=tenant_id,
            account_id=account_id,
            **props,
        )

    def account_activated(
        self, *, distinct_id: str, tenant_id: str = "", account_id: str = "", **props: object
    ) -> ProductEvent:
        return self.track(
            EventName.ACCOUNT_ACTIVATED,
            distinct_id=distinct_id,
            tenant_id=tenant_id,
            account_id=account_id,
            **props,
        )

    def tenant_onboarded(
        self, *, distinct_id: str, tenant_id: str = "", account_id: str = "", **props: object
    ) -> ProductEvent:
        return self.track(
            EventName.TENANT_ONBOARDED,
            distinct_id=distinct_id,
            tenant_id=tenant_id,
            account_id=account_id,
            **props,
        )

    def connector_connected(
        self, *, distinct_id: str, tenant_id: str = "", account_id: str = "", **props: object
    ) -> ProductEvent:
        return self.track(
            EventName.CONNECTOR_CONNECTED,
            distinct_id=distinct_id,
            tenant_id=tenant_id,
            account_id=account_id,
            **props,
        )

    def first_forecast_viewed(
        self, *, distinct_id: str, tenant_id: str = "", account_id: str = "", **props: object
    ) -> ProductEvent:
        return self.track(
            EventName.FIRST_FORECAST_VIEWED,
            distinct_id=distinct_id,
            tenant_id=tenant_id,
            account_id=account_id,
            **props,
        )

    def briefing_delivered(
        self, *, distinct_id: str, tenant_id: str = "", account_id: str = "", **props: object
    ) -> ProductEvent:
        return self.track(
            EventName.BRIEFING_DELIVERED,
            distinct_id=distinct_id,
            tenant_id=tenant_id,
            account_id=account_id,
            **props,
        )

    def cfo_question_asked(
        self, *, distinct_id: str, tenant_id: str = "", account_id: str = "", **props: object
    ) -> ProductEvent:
        return self.track(
            EventName.CFO_QUESTION_ASKED,
            distinct_id=distinct_id,
            tenant_id=tenant_id,
            account_id=account_id,
            **props,
        )

    def close_sealed(
        self, *, distinct_id: str, tenant_id: str = "", account_id: str = "", **props: object
    ) -> ProductEvent:
        return self.track(
            EventName.CLOSE_SEALED,
            distinct_id=distinct_id,
            tenant_id=tenant_id,
            account_id=account_id,
            **props,
        )

    def subscription_canceled(
        self, *, distinct_id: str, tenant_id: str = "", account_id: str = "", **props: object
    ) -> ProductEvent:
        return self.track(
            EventName.SUBSCRIPTION_CANCELED,
            distinct_id=distinct_id,
            tenant_id=tenant_id,
            account_id=account_id,
            **props,
        )
