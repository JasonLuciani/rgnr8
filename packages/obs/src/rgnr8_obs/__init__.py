"""RGNR8 observability — the logging / metrics / errors seam.

This package is the single observability surface the rest of RGNR8 wires into:
the web tier and the scheduler construct a `StructuredLogger`, a
`MetricsRegistry`, and an `ErrorReporter` at startup and thread them through
their request/job handlers. Everything here is pure stdlib and deterministic —
clocks are injected, never read from the wall — so the same code path is exercised
identically in tests and in production.

The shape is the house Protocol-seam pattern: each concern is an interface with an
in-memory implementation for tests/local and a production implementation
(`StreamLogSink`, `StatsdMetricsSink`, `SentryErrorReporter`) that is
shape-correct over an injected sink/transport rather than owning a socket.
Production binds the real sinks; tests bind the in-memory ones and assert on what
was emitted. Secret-named fields are redacted through one shared rule
(`redact_fields`) so logs and error reports can't leak tokens.
"""

from __future__ import annotations

from .errors import (
    CapturedError,
    ErrorReporter,
    InMemoryErrorReporter,
    SentryErrorReporter,
)
from .logging import (
    InMemoryLogSink,
    LogSink,
    StreamLogSink,
    StructuredLogger,
)
from .metrics import (
    Counter,
    Gauge,
    InMemoryMetricsSink,
    MetricsRegistry,
    MetricsSink,
    StatsdMetricsSink,
    Tags,
    Timer,
)
from .redact import (
    DEFAULT_SECRET_PATTERNS,
    REDACTED,
    is_secret_key,
    redact_fields,
)

__version__ = "0.1.0"

__all__ = [
    # redaction
    "DEFAULT_SECRET_PATTERNS",
    "REDACTED",
    "is_secret_key",
    "redact_fields",
    # logging
    "LogSink",
    "InMemoryLogSink",
    "StreamLogSink",
    "StructuredLogger",
    # metrics
    "MetricsSink",
    "InMemoryMetricsSink",
    "StatsdMetricsSink",
    "MetricsRegistry",
    "Counter",
    "Gauge",
    "Timer",
    "Tags",
    # errors
    "ErrorReporter",
    "InMemoryErrorReporter",
    "SentryErrorReporter",
    "CapturedError",
]
