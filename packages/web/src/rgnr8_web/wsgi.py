"""A WSGI adapter for the framework-free `WebApp`.

`WebApp.handle(Request) -> Response` is transport-agnostic; this wraps it in a
standard WSGI callable so it can be served by gunicorn/uwsgi/waitress in
production without pulling in a web framework. Header names are normalized from
WSGI's ``HTTP_*`` environ keys back to their hyphenated form (so
``authorization`` reaches the app's auth seam). Body is read from
``wsgi.input`` honoring ``CONTENT_LENGTH``.

    from rgnr8_web import WebApp, wsgi_app
    application = wsgi_app(build_app())   # gunicorn rgnr8_web.entry:application
"""

from __future__ import annotations

from typing import Callable, Iterable

from rgnr8_obs import ErrorReporter, MetricsRegistry, StructuredLogger

from .app import Request, Response, WebApp
from .middleware import ObservedApp, RateLimiter, with_security_headers

WsgiEnviron = dict[str, object]
StartResponse = Callable[[str, list[tuple[str, str]]], object]

_STATUS_TEXT = {
    200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized",
    403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
    413: "Payload Too Large", 429: "Too Many Requests",
    500: "Internal Server Error", 501: "Not Implemented",
}

# Cap request bodies before reading them into memory, so a single oversized POST
# can't exhaust a worker (there is no framework doing this for us).
MAX_BODY_BYTES = 1_048_576  # 1 MiB


class _BodyTooLarge(Exception):
    pass


def _headers_from_environ(environ: WsgiEnviron) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[5:].replace("_", "-").lower()
            headers[name] = str(value)
    # content-type/length arrive without the HTTP_ prefix
    if "CONTENT_TYPE" in environ:
        headers["content-type"] = str(environ["CONTENT_TYPE"])
    return headers


def _read_body(environ: WsgiEnviron) -> str:
    try:
        length = int(str(environ.get("CONTENT_LENGTH") or "0"))
    except (TypeError, ValueError):
        length = 0
    if length <= 0:
        return ""
    if length > MAX_BODY_BYTES:
        raise _BodyTooLarge()
    stream = environ.get("wsgi.input")
    if stream is None or not hasattr(stream, "read"):
        return ""
    raw = stream.read(min(length, MAX_BODY_BYTES))
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def request_from_environ(environ: WsgiEnviron) -> Request:
    method = str(environ.get("REQUEST_METHOD", "GET")).upper()
    path = str(environ.get("PATH_INFO", "/")) or "/"
    query = str(environ.get("QUERY_STRING", ""))
    if query:
        path = f"{path}?{query}"
    return Request(method=method, path=path, headers=_headers_from_environ(environ), body=_read_body(environ))


def wsgi_app(
    app: WebApp,
    *,
    rate_limiter: RateLimiter | None = None,
    logger: StructuredLogger | None = None,
    metrics: MetricsRegistry | None = None,
    errors: ErrorReporter | None = None,
) -> Callable[[WsgiEnviron, StartResponse], Iterable[bytes]]:
    """Wrap a WebApp as a WSGI application callable.

    Called with no keyword args this is the original, unhardened adapter — byte
    identical behavior for the existing call sites. Opt in to the public-track
    hardening by passing any of:

      * ``rate_limiter`` — token-bucket rate limiting, returning ``429`` before
        the request is dispatched;
      * ``logger`` + ``metrics`` (+ optional ``errors``) — the observe wrapper,
        timing + logging + metering each request and capturing exceptions.

    Whenever any of these are supplied the response also gets the standard
    security headers merged on (see ``middleware.security_headers``). The body cap
    and catch-all from the base adapter are preserved in every mode.
    """
    hardened = any(x is not None for x in (rate_limiter, logger, metrics, errors))

    # The observe wrapper reuses the metrics registry's own injected clock for its
    # single latency measurement, so timing stays deterministic and wall-clock free.
    dispatch: ObservedApp | WebApp = app
    if logger is not None and metrics is not None:
        dispatch = ObservedApp(app, logger=logger, metrics=metrics, errors=errors, clock=metrics._clock)

    def _finish(resp: Response, start_response: StartResponse) -> Iterable[bytes]:
        if hardened:
            resp = with_security_headers(resp)
        status_line = f"{resp.status} {_STATUS_TEXT.get(resp.status, 'OK')}"
        body = resp.body.encode("utf-8")
        headers = [
            *resp.headers.items(),
            ("Content-Length", str(len(body))),
        ]
        start_response(status_line, headers)
        return [body]

    def application(environ: WsgiEnviron, start_response: StartResponse) -> Iterable[bytes]:
        try:
            request = request_from_environ(environ)
        except _BodyTooLarge:
            return _finish(Response(413, '{"error":"request body too large"}'), start_response)
        # Rate limit before doing any real work: a 429 costs nothing to produce.
        if rate_limiter is not None:
            limited = rate_limiter.limited(request)
            if limited is not None:
                return _finish(limited, start_response)
        try:
            resp: Response = dispatch.handle(request)
        except Exception:
            # Catch-all: never leak a stack trace or unhandled error to a client.
            # (Details should go to the error reporter / logs, not the response.)
            resp = Response(500, '{"error":"internal server error"}')
        return _finish(resp, start_response)

    return application
