"""A revoked QuickBooks connection, end to end through the web UI: the owner's sync
hits invalid_grant, the connection is persisted as needs-reconnect, Intuit is not
called again, the Connections page shows a Reconnect notice, and a successful
reconnect clears it. Fake HTTP only: no network, no real credentials."""

import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from rgnr8_forecast import ForecastConfig
from rgnr8_qbo import (
    HttpResponse,
    InMemoryConnectionStore,
    QboConnection,
    QboConnectService,
    QboStatus,
    RetryPolicy,
)
from rgnr8_web import JwtAuthenticator, Request, WebApp
from test_qbo_connect import CONFIG, NOW, SECRET, T0, _h, _inputs, _users, usd


class RevokedHttp:
    """Intuit refuses the refresh token with invalid_grant. Records every call."""

    def __init__(self) -> None:
        self.get_urls: list[str] = []
        self.post_calls = 0
        self.revoked = True  # the owner has not reconnected yet

    def post(self, url: str, body: str, headers):  # type: ignore[no-untyped-def]
        self.post_calls += 1
        if not self.revoked:  # the code exchange of a fresh reconnect
            return HttpResponse(200, json.dumps({
                "access_token": "a-new", "refresh_token": "r-new", "expires_in": 3600,
                "x_refresh_token_expires_in": 8_640_000, "token_type": "bearer"}))
        return HttpResponse(400, json.dumps({"error": "invalid_grant",
                                             "error_description": "Token invalid"}))

    def get(self, url: str, headers):  # type: ignore[no-untyped-def]
        self.get_urls.append(url)
        return HttpResponse(200, json.dumps({"QueryResponse": {}}))


def _app(http: RevokedHttp, store: InMemoryConnectionStore) -> WebApp:
    qbo = QboConnectService(CONFIG, http, store, state_secret=SECRET, clock=lambda: T0,
                            retry=RetryPolicy(sleep=lambda _s: None))
    app = WebApp(
        authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=_users(),
        session_secret=SECRET, session_clock=lambda: NOW, qbo=qbo,
    )
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=usd("10000.00")), token="unused")
    return app


def test_revoked_connection_shows_a_reconnect_notice_and_a_successful_reconnect_clears_it() -> None:
    store = InMemoryConnectionStore()
    # A connection whose access token is about to expire, so the next sync refreshes.
    store.save(QboConnection(
        tenant_id="acme", realm_id="R42", access_token="a0", refresh_token="r0",
        access_expires_at=T0 + timedelta(seconds=30), refresh_expires_at=T0 + timedelta(days=90),
        status=QboStatus.CONNECTED, connected_at=T0,
    ))
    http = RevokedHttp()
    app = _app(http, store)

    # The owner's sync: the refresh is refused, so the connection is marked and the
    # owner is sent back to the Connections page.
    r = app.handle(Request("POST", "/t/acme/connect/qbo/sync", _h("owner@acme.com")))
    assert r.status == 302 and dict(r.extra_headers)["Location"] == "/t/acme/connect"
    stored = store.get("acme")
    assert stored is not None and stored.status is QboStatus.NEEDS_RECONNECT
    assert http.get_urls == []                       # no data call was made

    # The Connections page carries the notice and a Reconnect link to the OAuth route.
    page = app.handle(Request("GET", "/t/acme/connect", _h("owner@acme.com")))
    assert page.status == 200
    assert "Reconnect needed" in page.body
    assert "Reconnect QuickBooks" in page.body
    assert 'href="/t/acme/connect/qbo"' in page.body

    # Asking again makes no Intuit call either: the state is persisted.
    app.handle(Request("POST", "/t/acme/connect/qbo/sync", _h("owner@acme.com")))
    assert http.post_calls == 1 and http.get_urls == []

    # Reconnect through the existing OAuth route; the callback stores a live connection.
    http.revoked = False
    begin =app.handle(Request("GET", "/t/acme/connect/qbo", _h("owner@acme.com")))
    state = parse_qs(urlsplit(dict(begin.extra_headers)["Location"]).query)["state"][0]
    done = app.handle(Request("GET", f"/oauth/qbo/callback?code=c&state={state}&realmId=R42"))
    assert done.status == 302

    page = app.handle(Request("GET", "/t/acme/connect", _h("owner@acme.com")))
    assert "Reconnect needed" not in page.body
    assert "Connected" in page.body
    assert store.get("acme").status is QboStatus.CONNECTED  # type: ignore[union-attr]
