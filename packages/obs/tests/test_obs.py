"""Observability seam — JSON log shape + redaction, bind, metrics, errors.

All time is injected (`lambda: 1000`, or a stepping fake) so every assertion is
exact and deterministic; no test reads the wall clock.
"""

from __future__ import annotations

import io
import json
from typing import Callable

from rgnr8_obs import (
    InMemoryErrorReporter,
    InMemoryLogSink,
    InMemoryMetricsSink,
    MetricsRegistry,
    SentryErrorReporter,
    StatsdMetricsSink,
    StreamLogSink,
    StructuredLogger,
    is_secret_key,
    redact_fields,
)

FIXED = 1000.0


def _stepping_clock(values: list[float]) -> Callable[[], float]:
    """A clock that returns successive `values` on each call."""
    it = iter(values)
    return lambda: next(it)


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #


def test_log_record_has_fixed_spine_and_fields() -> None:
    sink = InMemoryLogSink()
    log = StructuredLogger(sink, clock=lambda: FIXED, service="web")
    log.info("request_handled", route="/health", status=200)

    assert len(sink.lines) == 1
    record = json.loads(sink.lines[0])
    assert record == {
        "ts": FIXED,
        "level": "info",
        "service": "web",
        "event": "request_handled",
        "route": "/health",
        "status": 200,
    }


def test_log_levels_map_to_level_field() -> None:
    sink = InMemoryLogSink()
    log = StructuredLogger(sink, clock=lambda: FIXED, service="scheduler")
    log.info("a")
    log.warn("b")
    log.error("c")
    levels = [r["level"] for r in sink.records()]
    assert levels == ["info", "warn", "error"]


def test_log_redacts_secret_named_fields() -> None:
    sink = InMemoryLogSink()
    log = StructuredLogger(sink, clock=lambda: FIXED, service="web")
    log.info(
        "auth_attempt",
        api_key="sk_live_abc",
        Authorization="Bearer xyz",
        access_token="at_123",
        password="hunter2",
        user_id="u_1",
    )
    record = sink.records()[0]
    assert record["api_key"] == "***"
    assert record["Authorization"] == "***"
    assert record["access_token"] == "***"
    assert record["password"] == "***"
    # non-secret field survives untouched
    assert record["user_id"] == "u_1"


def test_log_output_is_single_json_line_each() -> None:
    sink = InMemoryLogSink()
    log = StructuredLogger(sink, clock=lambda: FIXED, service="web")
    log.info("one")
    log.info("two")
    assert len(sink.lines) == 2
    for line in sink.lines:
        assert "\n" not in line
        json.loads(line)  # each line parses independently


def test_bind_merges_context_into_every_record() -> None:
    sink = InMemoryLogSink()
    base = StructuredLogger(sink, clock=lambda: FIXED, service="web")
    child = base.bind(request_id="req_9", tenant="acme")
    child.info("started")
    child.warn("slow", ms=42)

    started, slow = sink.records()
    assert started["request_id"] == "req_9"
    assert started["tenant"] == "acme"
    assert slow["request_id"] == "req_9"
    assert slow["tenant"] == "acme"
    assert slow["ms"] == 42
    # parent logger is unaffected by bind
    base.info("bare")
    assert "request_id" not in sink.records()[-1]


def test_bind_layers_and_call_fields_win() -> None:
    sink = InMemoryLogSink()
    log = StructuredLogger(sink, clock=lambda: FIXED, service="web").bind(stage="a")
    log2 = log.bind(stage="b", extra="x")
    log2.info("evt", stage="c")
    record = sink.records()[0]
    # later bind overrode earlier; per-call field overrode bound context
    assert record["stage"] == "c"
    assert record["extra"] == "x"


def test_bound_context_is_also_redacted() -> None:
    sink = InMemoryLogSink()
    log = StructuredLogger(sink, clock=lambda: FIXED, service="web").bind(token="t_secret")
    log.info("evt")
    assert sink.records()[0]["token"] == "***"


def test_stream_log_sink_writes_newline_terminated_lines() -> None:
    buf = io.StringIO()
    log = StructuredLogger(StreamLogSink(buf), clock=lambda: FIXED, service="web")
    log.info("evt", n=1)
    out = buf.getvalue()
    assert out.endswith("\n")
    assert json.loads(out.strip())["event"] == "evt"


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #


def test_counter_inc_default_and_explicit() -> None:
    sink = InMemoryMetricsSink()
    reg = MetricsRegistry(clock=lambda: FIXED, sink=sink)
    reg.counter("requests", route="health").inc()
    reg.counter("requests", route="health").inc(5)
    assert sink.emissions == [
        ("counter", "requests", 1, (("route", "health"),)),
        ("counter", "requests", 5, (("route", "health"),)),
    ]


def test_gauge_set() -> None:
    sink = InMemoryMetricsSink()
    reg = MetricsRegistry(clock=lambda: FIXED, sink=sink)
    reg.gauge("queue_depth", queue="jobs").set(12)
    assert sink.emissions == [("gauge", "queue_depth", 12, (("queue", "jobs"),))]


def test_timer_records_elapsed_ms_via_injected_clock() -> None:
    sink = InMemoryMetricsSink()
    # clock returns 1000.0 on enter, 1000.5 on exit -> 500 ms (exact in float)
    reg = MetricsRegistry(clock=_stepping_clock([1000.0, 1000.5]), sink=sink)
    with reg.timer("db_query", op="select"):
        pass
    kind, name, value, tags = sink.emissions[0]
    assert kind == "timer"
    assert name == "db_query"
    assert value == 500.0
    assert tags == (("op", "select"),)


def test_registry_defaults_to_in_memory_sink() -> None:
    reg = MetricsRegistry(clock=lambda: FIXED)
    reg.counter("c").inc()
    assert isinstance(reg.sink, InMemoryMetricsSink)
    assert reg.sink.emissions == [("counter", "c", 1, ())]


def test_tags_are_normalized_to_sorted_tuple() -> None:
    sink = InMemoryMetricsSink()
    reg = MetricsRegistry(clock=lambda: FIXED, sink=sink)
    reg.counter("c", b="2", a="1").inc()
    assert sink.emissions[0][3] == (("a", "1"), ("b", "2"))


def test_statsd_formats_counter_gauge_timer_lines() -> None:
    lines: list[str] = []
    sink = StatsdMetricsSink(lines.append)
    reg = MetricsRegistry(clock=_stepping_clock([0.0, 0.012]), sink=sink)
    reg.counter("name").inc()
    reg.gauge("name").set(3)
    with reg.timer("name"):
        pass
    assert lines == ["name:1|c", "name:3|g", "name:12|ms"]


def test_statsd_appends_tags() -> None:
    lines: list[str] = []
    sink = StatsdMetricsSink(lines.append)
    reg = MetricsRegistry(clock=lambda: FIXED, sink=sink)
    reg.counter("hits", route="home", method="get").inc(2)
    assert lines == ["hits:2|c|#method:get,route:home"]


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


def test_in_memory_reporter_returns_deterministic_ids() -> None:
    reporter = InMemoryErrorReporter()
    id0 = reporter.capture(ValueError("boom"), context={"op": "charge"})
    id1 = reporter.capture(KeyError("k"), context={"op": "refund"})
    assert id0 == "err_0"
    assert id1 == "err_1"
    assert len(reporter.captured) == 2
    assert isinstance(reporter.captured[0].exc, ValueError)
    assert reporter.captured[0].context == {"op": "charge"}


def test_in_memory_reporter_redacts_context() -> None:
    reporter = InMemoryErrorReporter()
    reporter.capture(
        RuntimeError("x"),
        context={"authorization": "Bearer secret", "tenant": "acme"},
    )
    captured = reporter.captured[0]
    assert captured.context["authorization"] == "***"
    assert captured.context["tenant"] == "acme"


def test_sentry_reporter_builds_payload_and_redacts() -> None:
    sent: list[dict[str, object]] = []
    reporter = SentryErrorReporter(sent.append)
    err_id = reporter.capture(
        ValueError("bad input"),
        context={"api_key": "sk_live", "request_id": "req_1"},
    )
    assert err_id == "evt_0"
    assert len(sent) == 1
    payload = sent[0]
    assert payload["event_id"] == "evt_0"
    assert payload["exception"] == {"type": "ValueError", "value": "bad input"}
    contexts = payload["contexts"]
    assert isinstance(contexts, dict)
    assert contexts["api_key"] == "***"
    assert contexts["request_id"] == "req_1"


def test_sentry_reporter_ids_increment_per_instance() -> None:
    reporter = SentryErrorReporter(lambda _payload: None)
    assert reporter.capture(ValueError("a"), context={}) == "evt_0"
    assert reporter.capture(ValueError("b"), context={}) == "evt_1"


# --------------------------------------------------------------------------- #
# redaction helper (shared)
# --------------------------------------------------------------------------- #


def test_is_secret_key_is_case_insensitive_contains() -> None:
    assert is_secret_key("API_KEY")
    assert is_secret_key("x-authorization")
    assert is_secret_key("SecretValue")
    assert not is_secret_key("username")


def test_redact_fields_copies_without_mutating_input() -> None:
    original = {"password": "p", "user": "u"}
    redacted = redact_fields(original)
    assert redacted == {"password": "***", "user": "u"}
    assert original["password"] == "p"  # input untouched
