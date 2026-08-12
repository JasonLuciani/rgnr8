"""Public-track hardening middleware: rate limiting, security headers, observe."""

from __future__ import annotations

import io
import json
from typing import Callable, Iterable

from rgnr8_obs import (
    InMemoryErrorReporter,
    InMemoryLogSink,
    InMemoryMetricsSink,
    MetricsRegistry,
    StructuredLogger,
)

from rgnr8_web import (
    ObservedApp,
    RateLimiter,
    Request,
    Response,
    WebApp,
    default_rate_limit_key,
    security_headers,
    with_security_headers,
    wsgi_app,
)
from rgnr8_web.middleware import counter_request_ids


class _Clock:
    """A mutable, injected clock — tests advance it explicitly."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _seq_clock(values: list[float]) -> Callable[[], float]:
    it = iter(values)

    def _c() -> float:
        return next(it)

    return _c


# --- rate limiter -----------------------------------------------------------

def test_rate_limiter_allows_capacity_then_denies() -> None:
    clock = _Clock()
    rl = RateLimiter(capacity=3, refill_per_second=1.0, clock=clock)
    assert [rl.check("k") for _ in range(3)] == [True, True, True]
    assert rl.check("k") is False  # bucket empty


def test_rate_limiter_refills_over_time() -> None:
    clock = _Clock()
    rl = RateLimiter(capacity=2, refill_per_second=1.0, clock=clock)
    assert rl.check("k") and rl.check("k")
    assert rl.check("k") is False
    clock.t += 1.0  # one token refills
    assert rl.check("k") is True
    assert rl.check("k") is False


def test_rate_limiter_keys_are_independent() -> None:
    clock = _Clock()
    rl = RateLimiter(capacity=1, refill_per_second=1.0, clock=clock)
    assert rl.check("a") is True
    assert rl.check("b") is True  # different bucket
    assert rl.check("a") is False


def test_limited_returns_429_with_retry_after() -> None:
    clock = _Clock()
    rl = RateLimiter(capacity=1, refill_per_second=0.5, clock=clock)
    req = Request("GET", "/api/acme/today", headers={"x-forwarded-for": "9.9.9.9"})
    assert rl.limited(req) is None  # first request allowed
    resp = rl.limited(req)
    assert resp is not None
    assert resp.status == 429
    body = json.loads(resp.body)
    assert body["error"] == "rate limit exceeded"
    # refill is 0.5/s so a full token takes 2s
    assert resp.headers["Retry-After"] == "2"
    assert body["retry_after"] == 2


def test_default_key_prefers_principal_then_ip() -> None:
    assert default_rate_limit_key(
        Request("GET", "/", headers={"x-api-key": "rgk_abc"})
    ) == "key:rgk_abc"
    assert default_rate_limit_key(
        Request("GET", "/", headers={"authorization": "Bearer tok-123"})
    ) == "principal:tok-123"
    assert default_rate_limit_key(
        Request("GET", "/", headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8"})
    ) == "ip:1.2.3.4"
    assert default_rate_limit_key(Request("GET", "/")) == "anonymous"


# --- security headers -------------------------------------------------------

def test_security_headers_present_and_no_duplicate_nosniff() -> None:
    hdrs = dict(security_headers())
    assert hdrs["X-Frame-Options"] == "DENY"
    assert hdrs["Referrer-Policy"] == "no-referrer"
    assert "default-src 'self'" in hdrs["Content-Security-Policy"]
    assert hdrs["Strict-Transport-Security"].startswith("max-age=")
    # X-Content-Type-Options is set by Response.headers, not re-emitted here
    assert "X-Content-Type-Options" not in hdrs


def test_with_security_headers_merges_onto_response() -> None:
    resp = with_security_headers(Response(200, "{}"))
    merged = resp.headers
    assert merged["X-Frame-Options"] == "DENY"
    assert merged["Referrer-Policy"] == "no-referrer"
    # the header the Response already provides is present exactly once
    assert merged["X-Content-Type-Options"] == "nosniff"
    # merging is idempotent — no duplication of X-Frame-Options in extra_headers
    again = with_security_headers(resp)
    names = [name for name, _ in again.extra_headers]
    assert names.count("X-Frame-Options") == 1


# --- observe ----------------------------------------------------------------

class _Echo:
    def __init__(self, resp: Response) -> None:
        self._resp = resp

    def handle(self, request: Request) -> Response:
        return self._resp


class _Boom:
    def handle(self, request: Request) -> Response:
        raise RuntimeError("kaboom")


def _observed(
    handler: object,
    *,
    clock: Callable[[], float],
) -> tuple[ObservedApp, InMemoryLogSink, InMemoryMetricsSink, InMemoryErrorReporter]:
    log_sink = InMemoryLogSink()
    logger = StructuredLogger(log_sink, clock=lambda: 0.0, service="web")
    metrics_sink = InMemoryMetricsSink()
    metrics = MetricsRegistry(clock=lambda: 0.0, sink=metrics_sink)
    errors = InMemoryErrorReporter()
    observed = ObservedApp(
        handler,  # type: ignore[arg-type]
        logger=logger,
        metrics=metrics,
        clock=clock,
        errors=errors,
        request_id=counter_request_ids(),
    )
    return observed, log_sink, metrics_sink, errors


def test_observe_logs_and_meters_a_request() -> None:
    observed, log_sink, metrics_sink, errors = _observed(
        _Echo(Response(200, "{}")), clock=_seq_clock([100.0, 100.25])
    )
    resp = observed.handle(Request("GET", "/api/acme/today"))
    assert resp.status == 200

    records = log_sink.records()
    assert len(records) == 1
    line = records[0]
    assert line["event"] == "request"
    assert line["method"] == "GET"
    assert line["route"] == "/api/acme/today"
    assert line["status"] == 200
    assert line["latency_ms"] == 250.0  # (100.25 - 100.0) * 1000, binary-exact
    assert line["request_id"] == "req-0"

    kinds = [(kind, name) for kind, name, _v, _t in metrics_sink.emissions]
    assert ("counter", "web.requests") in kinds
    assert ("timer", "web.request.latency_ms") in kinds
    # the timer datapoint reports the same measured latency the log line did
    timer = [e for e in metrics_sink.emissions if e[0] == "timer"][0]
    assert timer[2] == 250.0
    assert errors.captured == []


def test_observe_captures_exception_and_returns_500() -> None:
    observed, log_sink, metrics_sink, errors = _observed(
        _Boom(), clock=_seq_clock([0.0, 0.001])
    )
    resp = observed.handle(Request("POST", "/api/acme/ask"))
    assert resp.status == 500
    body = json.loads(resp.body)
    assert body["error"] == "internal server error"
    # the surfaced ref matches the captured error id
    assert len(errors.captured) == 1
    captured = errors.captured[0]
    assert body["ref"] == captured.id
    assert isinstance(captured.exc, RuntimeError)
    # a metric + a log line are still emitted for the failed request
    assert log_sink.records()[0]["status"] == 500
    assert any(k == "counter" for k, *_ in metrics_sink.emissions)


# --- wsgi wiring ------------------------------------------------------------

def _environ(
    method: str, path: str, headers: dict[str, str] | None = None, body: str = ""
) -> dict[str, object]:
    env: dict[str, object] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "wsgi.input": io.BytesIO(body.encode("utf-8")),
        "CONTENT_LENGTH": str(len(body.encode("utf-8"))),
    }
    for k, v in (headers or {}).items():
        env["HTTP_" + k.upper().replace("-", "_")] = v
    return env


def _call(
    application: Callable[..., Iterable[bytes]], env: dict[str, object]
) -> tuple[str, dict[str, str], str]:
    status_box: list[str] = []
    headers_box: list[dict[str, str]] = []

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        status_box.append(status)
        headers_box.append(dict(headers))

    chunks = application(env, start_response)
    body = b"".join(chunks).decode("utf-8")
    return status_box[0], headers_box[0], body


def test_wsgi_no_middleware_is_unchanged() -> None:
    app = WebApp()
    application = wsgi_app(app)
    status, headers, body = _call(application, _environ("GET", "/ready"))
    assert status.startswith("200")
    assert "ready" in body
    # default path stays byte-for-byte: no hardening headers added
    assert "X-Frame-Options" not in headers
    assert "Content-Security-Policy" not in headers
    assert headers["Content-Type"].startswith("application/json")


def test_wsgi_hardened_merges_security_headers() -> None:
    app = WebApp()
    metrics_sink = InMemoryMetricsSink()
    logger = StructuredLogger(InMemoryLogSink(), clock=lambda: 0.0, service="web")
    metrics = MetricsRegistry(clock=lambda: 0.0, sink=metrics_sink)
    application = wsgi_app(app, logger=logger, metrics=metrics)
    status, headers, _ = _call(application, _environ("GET", "/ready"))
    assert status.startswith("200")
    assert headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    # observe ran
    assert any(k == "counter" for k, *_ in metrics_sink.emissions)


def test_wsgi_rate_limiter_returns_429_before_dispatch() -> None:
    app = WebApp()
    clock = _Clock()
    rl = RateLimiter(capacity=1, refill_per_second=1.0, clock=clock)
    application = wsgi_app(app, rate_limiter=rl)
    env_headers = {"x-forwarded-for": "7.7.7.7"}
    status1, _, _ = _call(application, _environ("GET", "/ready", env_headers))
    assert status1.startswith("200")
    status2, headers2, body2 = _call(application, _environ("GET", "/ready", env_headers))
    assert status2.startswith("429")
    assert headers2["Retry-After"] == "1"
    assert json.loads(body2)["error"] == "rate limit exceeded"
    # security headers are on the 429 too (hardened mode)
    assert headers2["X-Frame-Options"] == "DENY"


def test_wsgi_body_cap_still_enforced_when_hardened() -> None:
    app = WebApp()
    logger = StructuredLogger(InMemoryLogSink(), clock=lambda: 0.0, service="web")
    metrics = MetricsRegistry(clock=lambda: 0.0, sink=InMemoryMetricsSink())
    application = wsgi_app(app, logger=logger, metrics=metrics)
    env = _environ("POST", "/api/acme/ask")
    env["CONTENT_LENGTH"] = str(2_000_000)  # over the 1 MiB cap
    status, headers, body = _call(application, env)
    assert status.startswith("413")
    assert json.loads(body)["error"] == "request body too large"
    assert headers["X-Frame-Options"] == "DENY"
