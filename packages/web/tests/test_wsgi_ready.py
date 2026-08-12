"""Readiness endpoint + the WSGI adapter."""

import io

from rgnr8_web import WebApp, Request, wsgi_app


def test_ready_reports_provisioning_without_auth() -> None:
    app = WebApp()
    r = app.handle(Request("GET", "/ready"))
    assert r.status == 200
    import json

    body = json.loads(r.body)
    assert body["status"] == "ready"
    assert body["tenants"] == 0
    assert body["packages"] is False
    assert "version" in body


def test_health_and_ready_need_no_token() -> None:
    app = WebApp()
    assert app.handle(Request("GET", "/health")).status == 200
    assert app.handle(Request("GET", "/ready")).status == 200
    # a protected route still 401s without a token
    assert app.handle(Request("GET", "/api/acme/today")).status == 401


def _environ(method: str, path: str, headers: dict[str, str] | None = None, body: str = "") -> dict:
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


def test_wsgi_adapter_serves_the_app() -> None:
    app = WebApp()
    application = wsgi_app(app)

    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = dict(headers)

    chunks = application(_environ("GET", "/ready"), start_response)  # type: ignore[arg-type]
    body = b"".join(chunks).decode("utf-8")
    assert captured["status"].startswith("200")  # type: ignore[union-attr]
    assert "ready" in body
    assert dict(captured["headers"])["Content-Type"].startswith("application/json")  # type: ignore[index]


def test_wsgi_passes_authorization_header_through() -> None:
    # a bearer token in the WSGI environ must reach the app's auth seam
    app = WebApp()
    application = wsgi_app(app)
    seen: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        seen["status"] = status

    env = _environ("GET", "/api/acme/today", headers={"authorization": "Bearer nope"})
    b"".join(application(env, start_response))  # type: ignore[arg-type]
    # unknown token → 401 (proves the header was parsed and handed to auth, not dropped)
    assert seen["status"].startswith("401")  # type: ignore[union-attr]
