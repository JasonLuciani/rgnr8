"""Framework-free, composable middleware for the public web tier.

These pieces operate directly on the app's ``Request``/``Response`` value types —
no WSGI, no framework — so they compose the same way in unit tests and in the
``wsgi.py`` wiring. Everything is deterministic: the rate limiter and the observe
wrapper take *injected* clocks and an *injected* request-id seam, so the same code
path is exercised identically in tests and production (the house rule — never read
the wall clock here).

Three concerns live here:

  * ``RateLimiter`` — a per-key token bucket. ``check(key)`` is the raw predicate;
    ``limited(request)`` maps a request to its key and returns a ready-made ``429``
    ``Response`` (with ``Retry-After``) when the caller is over budget, else ``None``.
  * ``security_headers()`` / ``with_security_headers()`` — the standard hardening
    headers, and a helper that merges them onto a ``Response`` without duplicating
    ``X-Content-Type-Options`` (already set by ``Response.headers``).
  * ``ObservedApp`` — a wrapper that, per request, times the handler, logs one
    structured line, increments a request counter, records a latency timer, and
    captures exceptions to an ``ErrorReporter``. It reuses the ``rgnr8_obs`` seam
    types rather than reinventing them.
"""

from __future__ import annotations

import dataclasses
import json
import math
from typing import Callable, Protocol

from rgnr8_obs import (
    ErrorReporter,
    MetricsRegistry,
    StructuredLogger,
    Tags,
)

from .app import Request, Response

__all__ = [
    "RateLimiter",
    "default_rate_limit_key",
    "security_headers",
    "with_security_headers",
    "SECURITY_HEADERS",
    "ObservedApp",
    "counter_request_ids",
]


# --- rate limiting ----------------------------------------------------------

def default_rate_limit_key(request: Request) -> str:
    """Derive the bucket key for a request.

    Prefer a stable *principal*: the ``X-API-Key`` header, else the bearer token
    in ``Authorization``. Anonymous traffic falls back to the client IP as
    forwarded by the edge (``X-Forwarded-For``, first hop). Never the wall clock,
    never random — the key is a pure function of the request.
    """
    api_key = request.headers.get("x-api-key", "").strip()
    if api_key:
        return f"key:{api_key}"
    auth = request.headers.get("authorization", "").strip()
    if auth:
        if auth.lower().startswith("bearer "):
            return f"principal:{auth[7:].strip()}"
        return f"principal:{auth}"
    fwd = request.headers.get("x-forwarded-for", "").strip()
    if fwd:
        # X-Forwarded-For is a comma-separated chain; the left-most is the client.
        return f"ip:{fwd.split(',')[0].strip()}"
    return "anonymous"


class RateLimiter:
    """A per-key token bucket with an injected clock.

    Each key gets a bucket of ``capacity`` tokens that refills at
    ``refill_per_second`` tokens/sec (capped at ``capacity``). ``check`` consumes
    one token and returns whether the request is allowed; the whole thing is a
    deterministic function of the injected clock, so tests pin time and assert
    exact allow/deny sequences.
    """

    def __init__(
        self,
        *,
        capacity: float,
        refill_per_second: float,
        clock: Callable[[], float],
        key_func: Callable[[Request], str] = default_rate_limit_key,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive")
        self._capacity = float(capacity)
        self._refill = float(refill_per_second)
        self._clock = clock
        self._key_func = key_func
        # key -> (tokens, last_refill_epoch_seconds)
        self._buckets: dict[str, tuple[float, float]] = {}

    def _refresh(self, key: str) -> tuple[float, float]:
        now = self._clock()
        tokens, last = self._buckets.get(key, (self._capacity, now))
        if now > last:
            tokens = min(self._capacity, tokens + (now - last) * self._refill)
        return tokens, now

    def check(self, key: str) -> bool:
        """Consume one token for ``key``; return True if it was available."""
        tokens, now = self._refresh(key)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return True
        self._buckets[key] = (tokens, now)
        return False

    def retry_after(self, key: str) -> int:
        """Whole seconds until at least one token is available for ``key`` (>= 1)."""
        tokens, _ = self._refresh(key)
        if tokens >= 1.0:
            return 0
        return max(1, math.ceil((1.0 - tokens) / self._refill))

    def limited(self, request: Request) -> Response | None:
        """Return a ``429`` response if ``request`` is over budget, else ``None``.

        Consumes a token on success; on failure emits a JSON body and a
        ``Retry-After`` header sized to when the next token frees up.
        """
        key = self._key_func(request)
        if self.check(key):
            return None
        retry = self.retry_after(key)
        return Response(
            429,
            json.dumps({"error": "rate limit exceeded", "retry_after": retry}),
            "application/json",
            (("Retry-After", str(retry)),),
        )


# --- security headers -------------------------------------------------------

# The app is self-contained and serves its own inline styles/scripts, so the CSP
# allows 'self' plus inline style/script but nothing remote. X-Content-Type-Options
# is intentionally omitted here — Response.headers already sets it.
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    (
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'",
    ),
    ("Strict-Transport-Security", "max-age=63072000; includeSubDomains"),
)


def security_headers() -> tuple[tuple[str, str], ...]:
    """The standard hardening headers to merge onto responses."""
    return SECURITY_HEADERS


def with_security_headers(resp: Response) -> Response:
    """Return ``resp`` with the hardening headers merged into ``extra_headers``.

    Headers already present on the response (via ``extra_headers`` or the
    ``Content-Type`` / ``X-Content-Type-Options`` the property adds) are left
    untouched, so we never emit a duplicate.
    """
    present = {name.lower() for name in resp.headers}
    additions = tuple(
        (name, value) for name, value in SECURITY_HEADERS if name.lower() not in present
    )
    if not additions:
        return resp
    return dataclasses.replace(resp, extra_headers=resp.extra_headers + additions)


# --- observe ----------------------------------------------------------------

class _Handler(Protocol):
    def handle(self, request: Request) -> Response: ...


def counter_request_ids(prefix: str = "req") -> Callable[[], str]:
    """A deterministic request-id seam: ``req-0``, ``req-1``, ... (no clock/random)."""
    counter = 0

    def _next() -> str:
        nonlocal counter
        rid = f"{prefix}-{counter}"
        counter += 1
        return rid

    return _next


class ObservedApp:
    """Wrap a handler so every request is timed, logged, metered, and error-captured.

    Reuses the ``rgnr8_obs`` seam: a ``StructuredLogger`` for the one-line record,
    a ``MetricsRegistry`` for the request counter + latency timer, and an optional
    ``ErrorReporter`` for exceptions. Latency is measured with the injected
    ``clock`` (a single measurement feeds both the log line and the timer metric),
    and the request id comes from the injected ``request_id`` seam — never random,
    never the wall clock.
    """

    def __init__(
        self,
        app: _Handler,
        *,
        logger: StructuredLogger,
        metrics: MetricsRegistry,
        clock: Callable[[], float],
        errors: ErrorReporter | None = None,
        request_id: Callable[[], str] | None = None,
        counter_name: str = "web.requests",
        latency_name: str = "web.request.latency_ms",
    ) -> None:
        self._app = app
        self._logger = logger
        self._metrics = metrics
        self._clock = clock
        self._errors = errors
        self._request_id = request_id if request_id is not None else counter_request_ids()
        self._counter_name = counter_name
        self._latency_name = latency_name

    def handle(self, request: Request) -> Response:
        rid = self._request_id()
        method, route = request.method, request.route
        start = self._clock()
        error_id: str | None = None
        try:
            resp = self._app.handle(request)
        except Exception as exc:
            if self._errors is not None:
                error_id = self._errors.capture(
                    exc, context={"request_id": rid, "method": method, "route": route}
                )
            payload: dict[str, object] = {"error": "internal server error"}
            if error_id is not None:
                payload["ref"] = error_id
            resp = Response(500, json.dumps(payload))
        latency_ms = (self._clock() - start) * 1000.0

        status = resp.status
        self._metrics.counter(
            self._counter_name, method=method, route=route, status=str(status)
        ).inc()
        # Record the measured latency as a timer datapoint. We emit through the
        # registry's sink so the single measurement above is the one number the
        # log line reports too (rather than timing twice with the metrics clock).
        tags: Tags = tuple(sorted({"method": method, "route": route}.items()))
        self._metrics.sink.emit("timer", self._latency_name, latency_ms, tags)

        self._logger.info(
            "request",
            method=method,
            route=route,
            status=status,
            latency_ms=latency_ms,
            request_id=rid,
            **({"error_id": error_id} if error_id is not None else {}),
        )
        return resp
