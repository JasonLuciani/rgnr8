"""RGNR8 product analytics — the funnel-instrumentation seam.

This package is the single product-analytics surface the rest of RGNR8 wires into.
An `EventTracker` is constructed at startup with an injected clock and an
`EventSink`, then threaded through the request/lifecycle boundaries that matter for
growth — signup, email verification, checkout, tenant onboarding, connector
linking, account activation, and first real value (first forecast, briefing, CFO
question, sealed close). Each boundary calls one thin helper (`signup_started`,
`checkout_completed`, …) drawn from the closed `EventName` catalog, so the
acquisition→activation funnel is emitted consistently and measurable from day one
— no ad-hoc string events, no drift.

The shape is the house Protocol-seam pattern, deterministic throughout: time is an
injected `Callable[[], float]` (never the wall clock) and there is no randomness,
so the same code path is exercised identically in tests and in production.
Production binds an `HttpEventSink` that POSTs a Segment/PostHog-style capture
envelope over an injected HTTP client; tests bind an `InMemoryEventSink` and assert
on exactly what was recorded. `funnel_report` turns a set of recorded events into
step-by-step conversion for any ordered funnel — the same events that flow to the
warehouse in production are the ones a test asserts conversion math against.
"""

from __future__ import annotations

from .events import (
    EventName,
    ProductEvent,
)
from .funnel import funnel_report
from .tracker import (
    EventSink,
    EventTracker,
    HttpClient,
    HttpEventSink,
    InMemoryEventSink,
)

__version__ = "0.1.0"

__all__ = [
    # events
    "EventName",
    "ProductEvent",
    # tracker
    "EventSink",
    "InMemoryEventSink",
    "HttpClient",
    "HttpEventSink",
    "EventTracker",
    # funnel
    "funnel_report",
]
