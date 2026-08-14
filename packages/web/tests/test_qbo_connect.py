"""The QuickBooks Online connect flow wired into the web UI: a connections page,
the authorize redirect, the OAuth callback (signed-state → exchange → persist),
disconnect, and RBAC gating (MANAGE_CONNECTORS). The token exchange goes through a
fake HTTP seam — no network, no real credentials."""

import json
from datetime import date, datetime, timezone
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_qbo import (
    HttpResponse,
    InMemoryConnectionStore,
    QboConnectService,
    QboOAuthConfig,
)
from rgnr8_web import (
    InMemoryUserDirectory,
    JwtAuthenticator,
    Request,
    Role,
    User,
    WebApp,
    sign_jwt,
)

SECRET = "qbo-secret"
NOW = 1_760_000_000
T0 = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
CONFIG = QboOAuthConfig(
    client_id="ABCclient", client_secret="s3cret",
    redirect_uri="http://localhost:8000/oauth/qbo/callback",
)


def usd(s: str) -> Money:
    return Money.from_decimal(s)


class FakeHttp:
    def post(self, url: str, body: str, headers: Mapping[str, str]) -> HttpResponse:
        return HttpResponse(200, json.dumps({
            "access_token": "access-1", "refresh_token": "refresh-1",
            "expires_in": 3600, "x_refresh_token_expires_in": 8_640_000,
            "token_type": "bearer",
        }))


def _inputs() -> ForecastInputs:
    return ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 3), available=usd("80000.00")))


def _users() -> InMemoryUserDirectory:
    d = InMemoryUserDirectory()
    d.upsert_user(User("owner@acme.com", "owner@acme.com", "owner"))
    d.set_membership("owner@acme.com", "acme", Role.OWNER)
    d.upsert_user(User("view@acme.com", "view@acme.com", "view"))
    d.set_membership("view@acme.com", "acme", Role.VIEWER)
    return d


def _app(*, with_qbo: bool = True) -> tuple[WebApp, InMemoryConnectionStore]:
    store = InMemoryConnectionStore()
    qbo = (
        QboConnectService(CONFIG, FakeHttp(), store, state_secret=SECRET, clock=lambda: T0)
        if with_qbo else None
    )
    app = WebApp(
        authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=_users(),
        session_secret=SECRET, session_clock=lambda: NOW, qbo=qbo,
    )
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=usd("10000.00")), token="unused")
    return app, store


def _h(sub: str, tenant: str = "acme") -> dict[str, str]:
    tok = sign_jwt({"sub": sub, "tenant": tenant, "exp": NOW + 3600}, SECRET)
    return {"authorization": f"Bearer {tok}"}


# --- connections page --------------------------------------------------------
def test_connect_page_shows_connect_button_when_not_connected() -> None:
    app, _ = _app()
    r = app.handle(Request("GET", "/t/acme/connect", _h("owner@acme.com")))
    assert r.status == 200 and "text/html" in r.content_type
    assert "QuickBooks Online" in r.body
    assert 'href="/t/acme/connect/qbo"' in r.body
    assert "http://" not in r.body and "https://" not in r.body  # self-contained


def test_connect_page_reports_not_configured_without_service() -> None:
    app, _ = _app(with_qbo=False)
    r = app.handle(Request("GET", "/t/acme/connect", _h("owner@acme.com")))
    assert r.status == 200
    assert "isn&#x27;t configured" in r.body or "configured" in r.body


def test_connect_nav_item_present_for_owner() -> None:
    app, _ = _app()
    r = app.handle(Request("GET", "/t/acme/connect", _h("owner@acme.com")))
    assert 'href="/t/acme/connect"' in r.body and ">Connect<" in r.body


# --- authorize redirect ------------------------------------------------------
def test_begin_redirects_to_intuit_with_state() -> None:
    app, _ = _app()
    r = app.handle(Request("GET", "/t/acme/connect/qbo", _h("owner@acme.com")))
    assert r.status == 302
    loc = dict(r.extra_headers)["Location"]
    assert loc.startswith("https://appcenter.intuit.com/connect/oauth2?")
    q = parse_qs(urlsplit(loc).query)
    assert q["client_id"] == ["ABCclient"]
    assert q["scope"] == ["com.intuit.quickbooks.accounting"]
    assert q["state"]  # signed state present


# --- callback (public) -> exchange + persist --------------------------------
def test_callback_completes_connection_and_persists() -> None:
    app, store = _app()
    # get a real signed state by starting the flow
    begin = app.handle(Request("GET", "/t/acme/connect/qbo", _h("owner@acme.com")))
    state = parse_qs(urlsplit(dict(begin.extra_headers)["Location"]).query)["state"][0]

    cb = app.handle(Request(
        "GET", f"/oauth/qbo/callback?code=auth-code&state={state}&realmId=9130350000"))
    assert cb.status == 302
    assert dict(cb.extra_headers)["Location"] == "/t/acme/connect"
    # connection persisted for the tenant carried in the signed state
    conn = store.get("acme")
    assert conn is not None and conn.realm_id == "9130350000"
    assert conn.access_token == "access-1"


def test_callback_rejects_forged_state() -> None:
    app, store = _app()
    cb = app.handle(Request(
        "GET", "/oauth/qbo/callback?code=c&state=forged.bad&realmId=1"))
    assert cb.status == 400
    assert store.get("acme") is None


def test_callback_handles_user_declined() -> None:
    app, _ = _app()
    cb = app.handle(Request(
        "GET", "/oauth/qbo/callback?error=access_denied&state=x"))
    assert cb.status == 400
    assert "declined" in cb.body


# --- connected state + disconnect -------------------------------------------
def test_connected_page_then_disconnect() -> None:
    app, store = _app()
    begin = app.handle(Request("GET", "/t/acme/connect/qbo", _h("owner@acme.com")))
    state = parse_qs(urlsplit(dict(begin.extra_headers)["Location"]).query)["state"][0]
    app.handle(Request("GET", f"/oauth/qbo/callback?code=c&state={state}&realmId=42"))

    page = app.handle(Request("GET", "/t/acme/connect", _h("owner@acme.com")))
    assert "Connected" in page.body
    assert 'action="/t/acme/connect/qbo/disconnect"' in page.body

    dis = app.handle(Request("POST", "/t/acme/connect/qbo/disconnect", _h("owner@acme.com")))
    assert dis.status == 302
    assert store.get("acme") is None


# --- RBAC + isolation --------------------------------------------------------
def test_viewer_cannot_connect_or_disconnect() -> None:
    app, _ = _app()
    assert app.handle(Request("GET", "/t/acme/connect", _h("view@acme.com"))).status == 403
    assert app.handle(Request("GET", "/t/acme/connect/qbo", _h("view@acme.com"))).status == 403
    assert app.handle(
        Request("POST", "/t/acme/connect/qbo/disconnect", _h("view@acme.com"))).status == 403


def test_callback_is_public_but_requires_valid_state() -> None:
    # no bearer token at all — the callback is reachable, but a bad state 400s
    app, _ = _app()
    r = app.handle(Request("GET", "/oauth/qbo/callback?code=c&state=nope&realmId=1"))
    assert r.status == 400
