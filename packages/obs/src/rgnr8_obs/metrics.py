"""Metrics registry — counters, gauges, and timers behind a sink seam.

Instrumentation should be cheap to call and boring to reason about, so the
registry hands back tiny handle objects (`Counter`, `Gauge`, `Timer`) that just
forward to a `MetricsSink`. The sink is the seam: production points at statsd (or
anything shaped like it), tests point at an in-memory recorder and assert on the
exact tuples emitted.

Timing is where wall-clock coupling usually sneaks in, so the *clock* is injected
as a `Callable[[], float]` returning seconds. We never call `time.monotonic`
here; a test passes a clock whose readings step by a known amount and gets a
deterministic elapsed-milliseconds value. Tags are keyword args, normalized to a
stable sorted tuple so identical tag sets compare and format identically.
"""

from __future__ import annotations

from types import TracebackType
from typing import Callable, Protocol


Tags = tuple[tuple[str, str], ...]


def _normalize_tags(tags: dict[str, str]) -> Tags:
    """Freeze tags into a deterministic, sorted, hashable tuple."""
    return tuple(sorted(tags.items()))


class MetricsSink(Protocol):
    def emit(self, kind: str, name: str, value: float, tags: Tags) -> None: ...


class InMemoryMetricsSink:
    """Records every emission as a tuple for assertions (tests/local)."""

    def __init__(self) -> None:
        self.emissions: list[tuple[str, str, float, Tags]] = []

    def emit(self, kind: str, name: str, value: float, tags: Tags) -> None:
        self.emissions.append((kind, name, value, tags))


class StatsdMetricsSink:
    """Formats statsd wire lines over an injected `send(line)` callable.

    Shape-correct only — no socket. `send` is where a real client would push
    bytes to a UDP endpoint; here it lets tests capture the exact strings. Line
    grammar: `name:value|type`, with `c` (counter), `g` (gauge), `ms` (timer),
    plus optional `|#k:v,k:v` dogstatsd-style tags.
    """

    _SUFFIX = {"counter": "c", "gauge": "g", "timer": "ms"}

    def __init__(self, send: Callable[[str], None]) -> None:
        self._send = send

    def emit(self, kind: str, name: str, value: float, tags: Tags) -> None:
        suffix = self._SUFFIX[kind]
        # Timers/counters are whole numbers on the wire; gauges may be fractional.
        rendered = f"{value:g}"
        line = f"{name}:{rendered}|{suffix}"
        if tags:
            line += "|#" + ",".join(f"{k}:{v}" for k, v in tags)
        self._send(line)


class Counter:
    def __init__(self, sink: MetricsSink, name: str, tags: Tags) -> None:
        self._sink = sink
        self._name = name
        self._tags = tags

    def inc(self, n: float = 1) -> None:
        self._sink.emit("counter", self._name, n, self._tags)


class Gauge:
    def __init__(self, sink: MetricsSink, name: str, tags: Tags) -> None:
        self._sink = sink
        self._name = name
        self._tags = tags

    def set(self, v: float) -> None:
        self._sink.emit("gauge", self._name, v, self._tags)


class Timer:
    """Context manager that records elapsed milliseconds via the injected clock."""

    def __init__(
        self,
        sink: MetricsSink,
        name: str,
        tags: Tags,
        clock: Callable[[], float],
    ) -> None:
        self._sink = sink
        self._name = name
        self._tags = tags
        self._clock = clock
        self._start = 0.0

    def __enter__(self) -> Timer:
        self._start = self._clock()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        elapsed_ms = (self._clock() - self._start) * 1000.0
        self._sink.emit("timer", self._name, elapsed_ms, self._tags)


class MetricsRegistry:
    """Factory for metric handles bound to a shared sink + clock."""

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        sink: MetricsSink | None = None,
    ) -> None:
        self._clock = clock
        self._sink: MetricsSink = sink if sink is not None else InMemoryMetricsSink()

    @property
    def sink(self) -> MetricsSink:
        return self._sink

    def counter(self, name: str, **tags: str) -> Counter:
        return Counter(self._sink, name, _normalize_tags(tags))

    def gauge(self, name: str, **tags: str) -> Gauge:
        return Gauge(self._sink, name, _normalize_tags(tags))

    def timer(self, name: str, **tags: str) -> Timer:
        return Timer(self._sink, name, _normalize_tags(tags), self._clock)
