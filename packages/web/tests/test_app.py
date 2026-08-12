import json

from rgnr8_web import Request
from factory import app_with_two_tenants

BRIGHT = {"authorization": "Bearer tok-bright"}


def test_health_needs_no_auth() -> None:
    app = app_with_two_tenants()
    r = app.handle(Request("GET", "/health"))
    assert r.status == 200
    assert json.loads(r.body)["status"] == "ok"


def test_missing_token_is_401() -> None:
    app = app_with_two_tenants()
    assert app.handle(Request("GET", "/t/bright")).status == 401


def test_today_dashboard_html() -> None:
    app = app_with_two_tenants()
    r = app.handle(Request("GET", "/t/bright", BRIGHT))
    assert r.status == 200
    assert r.content_type.startswith("text/html")
    assert "Bright Agency" in r.body and "<svg" in r.body


def test_today_json_shape() -> None:
    app = app_with_two_tenants()
    r = app.handle(Request("GET", "/api/bright/today", BRIGHT))
    assert r.status == 200
    data = json.loads(r.body)
    assert data["tenant"] == "bright"
    assert set(data) >= {"status", "headline", "cash_today", "floor", "trough", "weeks", "confidence", "version"}
    assert len(data["weeks"]) == 13


def test_ask_endpoint() -> None:
    app = app_with_two_tenants()
    r = app.handle(Request("POST", "/api/bright/ask", BRIGHT, json.dumps({"text": "am I going to run short?"})))
    assert r.status == 200
    data = json.loads(r.body)
    assert data["supported"] is True
    assert "floor" in data["answer"].lower()


def test_ask_rejects_empty_body() -> None:
    app = app_with_two_tenants()
    r = app.handle(Request("POST", "/api/bright/ask", BRIGHT, "{}"))
    assert r.status == 400


def test_tenant_isolation_403() -> None:
    app = app_with_two_tenants()
    # bright's token may not read acme
    r = app.handle(Request("GET", "/t/acme", BRIGHT))
    assert r.status == 403
    r2 = app.handle(Request("GET", "/api/acme/today", BRIGHT))
    assert r2.status == 403


def test_unknown_route_404() -> None:
    app = app_with_two_tenants()
    assert app.handle(Request("GET", "/api/bright/nonsense", BRIGHT)).status == 404


def test_briefing_text_endpoint() -> None:
    app = app_with_two_tenants()
    r = app.handle(Request("GET", "/api/bright/briefing.txt", BRIGHT))
    assert r.status == 200
    assert r.content_type.startswith("text/plain")
    assert "RGNR8 weekly cash briefing" in r.body
