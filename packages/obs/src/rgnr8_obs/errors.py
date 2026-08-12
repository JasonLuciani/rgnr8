"""Error-reporting seam — capture an exception + context, get an id back.

When something throws in the web tier or the scheduler, we want the exception and
its surrounding context (request id, tenant, operation) shipped to one place and a
stable *reference id* returned, so the same id can be surfaced to the user and
grepped in the error tracker. `ErrorReporter` is that seam.

`InMemoryErrorReporter` records every capture and hands back a deterministic
`err_{n}` id — reproducible, no clock, no network — for tests and local runs.
`SentryErrorReporter` builds a Sentry-shaped event payload and pushes it over an
injected `transport(payload)` callable: shape-correct, unit-testable, no socket;
wiring a real transport turns it live.

Context flows straight from call sites, so — exactly like the logger — it passes
through the shared redaction rule first. A secret smuggled in as
`context={"authorization": ...}` is reported as `***`.
"""

from __future__ import annotations

from typing import Callable, Mapping, Protocol

from .redact import DEFAULT_SECRET_PATTERNS, redact_fields


class ErrorReporter(Protocol):
    def capture(self, exc: BaseException, *, context: Mapping[str, object]) -> str: ...


class CapturedError:
    """One recorded capture (in-memory reporter)."""

    def __init__(self, error_id: str, exc: BaseException, context: dict[str, object]) -> None:
        self.id = error_id
        self.exc = exc
        self.context = context


class InMemoryErrorReporter:
    """Records captures with redacted context; returns deterministic `err_{n}`."""

    def __init__(self) -> None:
        self.captured: list[CapturedError] = []

    def capture(self, exc: BaseException, *, context: Mapping[str, object]) -> str:
        error_id = f"err_{len(self.captured)}"
        self.captured.append(
            CapturedError(error_id, exc, redact_fields(context))
        )
        return error_id


class SentryErrorReporter:
    """Builds a Sentry-style event payload over an injected transport.

    Shape-correct only — no network. `transport` is where a real SDK would ship
    the event; here it lets tests inspect the exact dict. The returned id is the
    event id we hand the payload (deterministic per instance so the surfaced id
    matches what was sent).
    """

    def __init__(
        self,
        transport: Callable[[dict[str, object]], None],
        *,
        redact_keys: frozenset[str] = DEFAULT_SECRET_PATTERNS,
    ) -> None:
        self._transport = transport
        self._redact_keys = redact_keys
        self._seq = 0

    def capture(self, exc: BaseException, *, context: Mapping[str, object]) -> str:
        event_id = f"evt_{self._seq}"
        self._seq += 1
        payload: dict[str, object] = {
            "event_id": event_id,
            "exception": {
                "type": type(exc).__name__,
                "value": str(exc),
            },
            "contexts": redact_fields(context, self._redact_keys),
        }
        self._transport(payload)
        return event_id
