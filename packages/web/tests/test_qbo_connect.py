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


# --- sync: pull the connected company into the forecast ----------------------
class FakeHttpWithGet:
    """Fake HTTP whose GETs return queued QBO query responses in order."""

    def __init__(self, gets: list) -> None:
        self._gets = list(gets)
        self.get_calls: list[str] = []

    def post(self, url: str, body: str, headers):  # pragma: no cover - unused here
        return HttpResponse(200, json.dumps({
            "access_token": "a", "refresh_token": "r", "expires_in": 3600,
            "x_refresh_token_expires_in": 8_640_000, "token_type": "bearer"}))

    def get(self, url: str, headers):
        self.get_calls.append(url)
        return self._gets.pop(0) if self._gets else HttpResponse(200, json.dumps({"QueryResponse": {}}))


def _qr(body: dict) -> HttpResponse:
    return HttpResponse(200, json.dumps({"QueryResponse": body}))


def _connected_app(*, ledger=None, extra_queries: list | None = None):
    from datetime import timedelta

    from rgnr8_qbo import QboConnection, QboStatus

    store = InMemoryConnectionStore()
    store.save(QboConnection(
        tenant_id="acme", realm_id="R42", access_token="live-tok", refresh_token="r",
        access_expires_at=T0 + timedelta(hours=1), refresh_expires_at=T0 + timedelta(days=90),
        status=QboStatus.CONNECTED, connected_at=T0,
    ))
    http = FakeHttpWithGet([
        _qr({"CompanyInfo": [{"CompanyName": "Sandbox Co"}]}),
        _qr({"Account": [
            {"Id": "1", "Name": "Checking", "AccountType": "Bank", "CurrentBalance": 1201.00},
            {"Id": "2", "Name": "Savings", "AccountType": "Bank", "CurrentBalance": 800.50},
        ]}),
        _qr({"Invoice": [
            {"Id": "9", "DocNumber": "1001", "Balance": 150.00, "TxnDate": "2026-07-01",
             "DueDate": "2026-08-01", "CustomerRef": {"name": "Amy"}},
        ]}),
        _qr({"Bill": [
            {"Id": "5", "Balance": 400.00, "TxnDate": "2026-07-10", "DueDate": "2026-08-10",
             "VendorRef": {"name": "Norton"}},
        ]}),
        *(extra_queries or []),
    ])
    qbo = QboConnectService(CONFIG, http, store, state_secret=SECRET, clock=lambda: T0)
    app = WebApp(
        authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=_users(),
        session_secret=SECRET, session_clock=lambda: NOW, qbo=qbo,
    )
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=usd("10000.00")), token="unused")
    if ledger is not None:
        app.set_ledger(ledger)
    return app


def test_sync_pulls_company_into_forecast() -> None:
    app = _connected_app()
    r = app.handle(Request("POST", "/t/acme/connect/qbo/sync", _h("owner@acme.com")))
    assert r.status == 302
    assert dict(r.extra_headers)["Location"] == "/t/acme/connect?sync=ok"

    # the connect page now shows the last-sync summary with the real cash total
    page = app.handle(Request("GET", "/t/acme/connect?sync=ok", _h("owner@acme.com")))
    assert "Last sync" in page.body and "Sandbox Co" in page.body
    assert "$2,001.50" in page.body      # 1201.00 + 800.50 bank balances
    assert "Synced from QuickBooks" in page.body

    # and the dashboard cash-today reflects the synced opening balance
    home = app.handle(Request("GET", "/app", _h("owner@acme.com")))
    assert "$2,001.50" in home.body


def test_sync_requires_manage_connectors() -> None:
    app = _connected_app()
    r = app.handle(Request("POST", "/t/acme/connect/qbo/sync", _h("view@acme.com")))
    assert r.status == 403


def test_sync_without_connection_bounces_to_connect() -> None:
    # configured QBO but tenant not connected -> redirect to connect (reconnect)
    app, _ = _app()
    r = app.handle(Request("POST", "/t/acme/connect/qbo/sync", _h("owner@acme.com")))
    assert r.status == 302
    assert dict(r.extra_headers)["Location"] == "/t/acme/connect"


# --- the same sync also feeds the LEDGER, not just the forecast --------------

class RecordingLedgerTransport:
    """A ledger that answers plausibly and remembers what it was asked to do."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def request(self, method: str, path: str, body: str, headers):
        from rgnr8_web import LedgerResponse
        self.calls.append((method, path, body))
        if method == "GET" and (path.endswith("/invoices") or path.endswith("/bills")):
            return LedgerResponse(200, {"documents": []})
        if "/feed/" in path:
            payload = json.loads(body)
            n = len(payload["transactions"])
            return LedgerResponse(201, {"received": n, "added": n, "duplicates": 0,
                                        "auto_posted": 0, "pending": n})
        return LedgerResponse(201, {"ok": True})


def test_a_sync_brings_the_books_across_not_just_the_forecast() -> None:
    from rgnr8_web import LedgerClient
    transport = RecordingLedgerTransport()
    app = _connected_app(
        ledger=LedgerClient(transport),
        extra_queries=[
            # the ledger sync re-reads open items, then asks for bank movements
            _qr({"Invoice": [
                {"Id": "9", "DocNumber": "1001", "Balance": 150.00, "TxnDate": "2026-07-01",
                 "DueDate": "2026-08-01", "CustomerRef": {"name": "Amy"}},
            ]}),
            _qr({"Bill": [
                {"Id": "5", "Balance": 400.00, "TxnDate": "2026-07-10", "DueDate": "2026-08-10",
                 "VendorRef": {"name": "Norton"}},
            ]}),
            _qr({"Purchase": [
                {"Id": "77", "TotalAmt": 42.50, "TxnDate": "2026-07-14",
                 "PrivateNote": "OFFICE DEPOT", "EntityRef": {"name": "Office Depot"}},
            ]}),
            _qr({"Deposit": []}),
        ],
    )
    r = app.handle(Request("POST", "/t/acme/connect/qbo/sync", _h("owner@acme.com")))
    assert r.status == 302
    assert dict(r.extra_headers)["Location"] == "/t/acme/connect?sync=ok"

    posts = [c for c in transport.calls if c[0] == "POST"]
    paths = [c[1] for c in posts]
    assert any(p.endswith("/invoices") for p in paths), "the open invoice became a document"
    assert any(p.endswith("/bills") for p in paths)
    assert any("/feed/1000" in p for p in paths), "the purchase went to the review queue"
    # and NOTHING was posted straight to the general ledger
    assert not any(p.endswith("/entries") for p in paths)

    # the connect page reports both halves
    page = app.handle(Request("GET", "/t/acme/connect", _h("owner@acme.com")))
    assert "Brought into your books" in page.body
    assert "queued for review rather than posted" in page.body
    assert "/t/acme/inbox" in page.body


def test_without_a_ledger_the_sync_still_feeds_the_forecast_alone() -> None:
    app = _connected_app()
    r = app.handle(Request("POST", "/t/acme/connect/qbo/sync", _h("owner@acme.com")))
    assert r.status == 302
    page = app.handle(Request("GET", "/t/acme/connect", _h("owner@acme.com")))
    assert "Last sync" in page.body
    assert "Brought into your books" not in page.body
