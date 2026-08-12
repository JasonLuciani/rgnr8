"""Structured JSON logging — one machine-parseable object per line.

Human-readable log strings are useless at fleet scale: you can't query them, you
can't alert on them, and free-text interpolation is exactly how secrets end up in
plaintext. So every record here is a single JSON object with a fixed spine
({ts, level, service, event}) plus arbitrary structured fields, emitted one per
line (JSONL) so log shippers can split on newlines.

Two seams keep this deterministic and testable:
  * the *clock* is injected (`Callable[[], float]` returning epoch seconds) — we
    never read the wall clock, so a test can pin `ts` to a constant;
  * the *sink* is a `LogSink` Protocol — production writes to a stream/collector,
    tests capture lines in memory.

Every user-supplied field is run through the shared redaction rule, so a stray
`api_key=` or `authorization=` renders as `***` regardless of the call site.
`bind(**ctx)` returns a child logger that merges standing context (request id,
tenant, etc.) into every record without re-passing it.
"""

from __future__ import annotations

import json
from typing import Callable, Protocol, TextIO

from .redact import DEFAULT_SECRET_PATTERNS, redact_fields


class LogSink(Protocol):
    def write(self, line: str) -> None: ...


class InMemoryLogSink:
    """Records every emitted line for assertions (tests/local)."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)

    def records(self) -> list[dict[str, object]]:
        """Parsed convenience view — each line decoded back into a dict."""
        return [json.loads(line) for line in self.lines]


class StreamLogSink:
    """Writes newline-terminated lines to any text file-like (stdout, a file)."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def write(self, line: str) -> None:
        self._stream.write(line + "\n")


class StructuredLogger:
    """Emits redacted, structured JSON records through an injected sink + clock."""

    def __init__(
        self,
        sink: LogSink,
        *,
        clock: Callable[[], float],
        service: str,
        redact_keys: frozenset[str] = DEFAULT_SECRET_PATTERNS,
        _context: dict[str, object] | None = None,
    ) -> None:
        self._sink = sink
        self._clock = clock
        self._service = service
        self._redact_keys = redact_keys
        self._context: dict[str, object] = dict(_context or {})

    def bind(self, **ctx: object) -> StructuredLogger:
        """Return a child logger that merges `ctx` into every future record.

        Shares the same sink/clock/service; standing context accumulates so
        successive binds layer (later keys win)."""
        merged = {**self._context, **ctx}
        return StructuredLogger(
            self._sink,
            clock=self._clock,
            service=self._service,
            redact_keys=self._redact_keys,
            _context=merged,
        )

    def _emit(self, level: str, event: str, fields: dict[str, object]) -> None:
        merged = {**self._context, **fields}
        record: dict[str, object] = {
            "ts": self._clock(),
            "level": level,
            "service": self._service,
            "event": event,
        }
        record.update(redact_fields(merged, self._redact_keys))
        self._sink.write(json.dumps(record, sort_keys=True, separators=(",", ":")))

    def info(self, event: str, **fields: object) -> None:
        self._emit("info", event, fields)

    def warn(self, event: str, **fields: object) -> None:
        self._emit("warn", event, fields)

    def error(self, event: str, **fields: object) -> None:
        self._emit("error", event, fields)
