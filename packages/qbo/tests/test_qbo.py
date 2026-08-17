"""The QBO connect flow: authorize URL, code exchange, refresh + rotation,
disconnect/revoke, CSRF state, and connection persistence — all against a fake
HTTP seam (no network, no real credentials)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

import pytest

from rgnr8_qbo import (
    ConnectionStore,
    HttpResponse,
    InMemoryConnectionStore,
    QboConnectService,
    QboConnection,
    QboEnvironment,
    QboOAuthConfig,
    QboOAuthError,
    QboStatus,
    SqlConnectionStore,
    StateError,
    StateSigner,
    authorize_url,
    connection_from_dict,
    connection_to_dict,
    exchange_code,
    refresh_tokens,
)

CONFIG = QboOAuthConfig(
    client_id="ABCclient",
    client_secret="s3cret",
    redirect_uri="http://localhost:8000/oauth/qbo/callback",
    environment=QboEnvironment.PRODUCTION,
)
T0 = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


class FakeHttp:
    """Records requests and returns queued responses (or a default token blob)."""

    def __init__(self, responses: list[HttpResponse] | None = None) -> None:
        self.calls: list[tuple[str, str, Mapping[str, str]]] = []
        self._responses = responses or []

    def _token_blob(self, access: str, refresh: str) -> HttpResponse:
        return HttpResponse(
            200,
            json.dumps(
                {
                    "access_token": access,
                    "refresh_token": refresh,
                    "expires_in": 3600,
                    "x_refresh_token_expires_in": 8_640_000,
                    "token_type": "bearer",
                }
            ),
        )

    def post(self, url: str, body: str, headers: Mapping[str, str]) -> HttpResponse:
        self.calls.append((url, body, headers))
        if self._responses:
            return self._responses.pop(0)
        return self._token_blob("access-1", "refresh-1")

    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        self.calls.append((url, "", headers))
        if self._responses:
            return self._responses.pop(0)
        return HttpResponse(200, json.dumps({"QueryResponse": {}}))


def _clock(t: datetime):
    return lambda: t


# --- authorize URL -----------------------------------------------------------
def test_authorize_url_carries_all_oauth_params() -> None:
    url = authorize_url(CONFIG, "the-state")
    q = parse_qs(urlsplit(url).query)
    assert url.startswith("https://appcenter.intuit.com/connect/oauth2?")
    assert q["client_id"] == ["ABCclient"]
    assert q["response_type"] == ["code"]
    assert q["scope"] == ["com.intuit.quickbooks.accounting"]
    assert q["redirect_uri"] == ["http://localhost:8000/oauth/qbo/callback"]
    assert q["state"] == ["the-state"]


def test_environment_api_base() -> None:
    assert QboEnvironment.PRODUCTION.api_base == "https://quickbooks.api.intuit.com"
    assert QboEnvironment.SANDBOX.api_base == "https://sandbox-quickbooks.api.intuit.com"


# --- token exchange + basic auth --------------------------------------------
def test_exchange_code_posts_grant_and_basic_auth() -> None:
    http = FakeHttp()
    tokens = exchange_code(CONFIG, http, "auth-code-xyz")
    assert tokens.access_token == "access-1"
    assert tokens.refresh_token == "refresh-1"
    assert tokens.expires_in == 3600
    url, body, headers = http.calls[0]
    assert url == "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
    assert "grant_type=authorization_code" in body
    assert "code=auth-code-xyz" in body
    # HTTP Basic base64("ABCclient:s3cret")
    assert headers["Authorization"] == "Basic QUJDY2xpZW50OnMzY3JldA=="
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"


def test_exchange_code_raises_on_non_2xx() -> None:
    http = FakeHttp([HttpResponse(400, '{"error":"invalid_grant"}')])
    with pytest.raises(QboOAuthError):
        exchange_code(CONFIG, http, "bad")


def test_refresh_rotates_refresh_token() -> None:
    http = FakeHttp([FakeHttp()._token_blob("access-2", "refresh-2")])
    tokens = refresh_tokens(CONFIG, http, "refresh-1")
    assert tokens.access_token == "access-2"
    assert tokens.refresh_token == "refresh-2"
    assert "grant_type=refresh_token" in http.calls[0][1]


# --- CSRF state --------------------------------------------------------------
def test_state_roundtrips_tenant() -> None:
    signer = StateSigner("state-key", clock=_clock(T0))
    state = signer.issue("acme")
    assert signer.verify(state) == "acme"


def test_state_rejects_tamper_and_expiry() -> None:
    signer = StateSigner("state-key", clock=_clock(T0))
    state = signer.issue("acme")
    with pytest.raises(StateError):
        signer.verify(state[:-2] + "xy")  # corrupt the MAC
    # a different key cannot verify
    other = StateSigner("other-key", clock=_clock(T0))
    with pytest.raises(StateError):
        other.verify(state)
    # expired after TTL
    later = StateSigner("state-key", clock=_clock(T0 + timedelta(minutes=20)))
    later._key = signer._key  # same key, later clock
    with pytest.raises(StateError):
        later.verify(state)


# --- service: begin / complete ----------------------------------------------
def _service(http: FakeHttp, store: ConnectionStore, t: datetime = T0) -> QboConnectService:
    return QboConnectService(CONFIG, http, store, state_secret="state-key", clock=_clock(t))


def test_begin_returns_authorize_url_with_signed_state() -> None:
    svc = _service(FakeHttp(), InMemoryConnectionStore())
    url = svc.begin("acme")
    state = parse_qs(urlsplit(url).query)["state"][0]
    # the same service can verify the state it issued
    assert svc._signer.verify(state) == "acme"


def test_complete_exchanges_and_persists_connection() -> None:
    http, store = FakeHttp(), InMemoryConnectionStore()
    svc = _service(http, store)
    state = svc.begin("acme").split("state=")[1]
    conn = svc.complete(state, "auth-code", "9130350000")
    assert conn.tenant_id == "acme"
    assert conn.realm_id == "9130350000"
    assert conn.status is QboStatus.CONNECTED
    assert conn.access_expires_at == T0 + timedelta(seconds=3600)
    # persisted + tenant recovered from the *signed* state, not a query param
    assert store.get("acme") is not None


def test_complete_rejects_bad_state_and_missing_realm() -> None:
    svc = _service(FakeHttp(), InMemoryConnectionStore())
    with pytest.raises(StateError):
        svc.complete("not-a-valid-state", "code", "123")
    good = svc.begin("acme").split("state=")[1]
    with pytest.raises(StateError):
        svc.complete(good, "code", "")  # missing realmId


# --- service: ensure_fresh (refresh logic) ----------------------------------
def _connect(store: ConnectionStore, *, access_exp: datetime, refresh_exp: datetime) -> None:
    store.save(
        QboConnection(
            tenant_id="acme", realm_id="R1",
            access_token="a0", refresh_token="r0",
            access_expires_at=access_exp, refresh_expires_at=refresh_exp,
            status=QboStatus.CONNECTED, connected_at=T0,
        )
    )


def test_ensure_fresh_returns_valid_without_refreshing() -> None:
    http, store = FakeHttp(), InMemoryConnectionStore()
    _connect(store, access_exp=T0 + timedelta(hours=1), refresh_exp=T0 + timedelta(days=100))
    svc = _service(http, store)
    conn = svc.ensure_fresh("acme")
    assert conn is not None and conn.access_token == "a0"
    assert http.calls == []  # no network — token still valid


def test_ensure_fresh_refreshes_near_expiry_and_rotates() -> None:
    http = FakeHttp([FakeHttp()._token_blob("a1", "r1")])
    store = InMemoryConnectionStore()
    _connect(store, access_exp=T0 + timedelta(seconds=30), refresh_exp=T0 + timedelta(days=90))
    svc = _service(http, store)
    conn = svc.ensure_fresh("acme")
    assert conn is not None
    assert conn.access_token == "a1" and conn.refresh_token == "r1"
    assert conn.access_expires_at == T0 + timedelta(seconds=3600)
    # the rotated token is what's persisted
    assert store.get("acme").refresh_token == "r1"


def test_ensure_fresh_marks_expired_when_refresh_lapsed() -> None:
    http, store = FakeHttp(), InMemoryConnectionStore()
    _connect(store, access_exp=T0 - timedelta(minutes=5), refresh_exp=T0 - timedelta(days=1))
    svc = _service(http, store)
    conn = svc.ensure_fresh("acme")
    assert conn is not None and conn.status is QboStatus.EXPIRED
    assert http.calls == []  # can't refresh — owner must reconnect


def test_ensure_fresh_none_when_never_connected() -> None:
    svc = _service(FakeHttp(), InMemoryConnectionStore())
    assert svc.ensure_fresh("nobody") is None


# --- service: disconnect -----------------------------------------------------
def test_disconnect_revokes_and_drops() -> None:
    http = FakeHttp([HttpResponse(200, "")])
    store = InMemoryConnectionStore()
    _connect(store, access_exp=T0 + timedelta(hours=1), refresh_exp=T0 + timedelta(days=100))
    svc = _service(http, store)
    assert svc.disconnect("acme") is True
    assert store.get("acme") is None
    assert http.calls[0][0] == "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
    assert svc.disconnect("acme") is False  # already gone


def test_disconnect_drops_even_if_revoke_fails() -> None:
    http = FakeHttp([HttpResponse(500, "boom")])
    store = InMemoryConnectionStore()
    _connect(store, access_exp=T0 + timedelta(hours=1), refresh_exp=T0 + timedelta(days=100))
    svc = _service(http, store)
    assert svc.disconnect("acme") is True
    assert store.get("acme") is None


# --- redaction ---------------------------------------------------------------
def test_redacted_never_leaks_tokens() -> None:
    conn = QboConnection(
        tenant_id="acme", realm_id="R1", access_token="SECRET_A", refresh_token="SECRET_R",
        access_expires_at=T0, refresh_expires_at=T0, status=QboStatus.CONNECTED, connected_at=T0,
    )
    blob = json.dumps(conn.redacted())
    assert "SECRET_A" not in blob and "SECRET_R" not in blob
    assert "R1" in blob


# --- persistence -------------------------------------------------------------
def test_connection_dict_roundtrip() -> None:
    conn = QboConnection(
        tenant_id="acme", realm_id="R1", access_token="a", refresh_token="r",
        access_expires_at=T0 + timedelta(hours=1), refresh_expires_at=T0 + timedelta(days=100),
        status=QboStatus.CONNECTED, connected_at=T0,
    )
    back = connection_from_dict(connection_to_dict(conn))
    assert back == conn


def test_sql_connection_store_roundtrip() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        store = SqlConnectionStore(conn)
        store.create_schema()
        c = QboConnection(
            tenant_id="acme", realm_id="R1", access_token="a", refresh_token="r",
            access_expires_at=T0 + timedelta(hours=1), refresh_expires_at=T0 + timedelta(days=100),
            status=QboStatus.CONNECTED, connected_at=T0,
        )
        store.save(c)
        assert store.get("acme") == c
        assert len(store.list_all()) == 1
        store.delete("acme")
        assert store.get("acme") is None
    finally:
        conn.close()


# --- Accounting API client ---------------------------------------------------
def _qr(body: dict) -> HttpResponse:
    return HttpResponse(200, json.dumps({"QueryResponse": body, "time": "2026-08-17T00:00:00Z"}))


def test_api_client_query_builds_url_and_auth() -> None:
    from rgnr8_qbo import QboApiClient

    http = FakeHttp([_qr({"CompanyInfo": [{"CompanyName": "Sandbox Co"}]})])
    client = QboApiClient(http=http, api_base="https://sandbox-quickbooks.api.intuit.com",
                          realm_id="R9", access_token="tok-abc")
    company = client.company_info()
    assert company.name == "Sandbox Co"
    url, _body, headers = http.calls[0]
    assert url.startswith("https://sandbox-quickbooks.api.intuit.com/v3/company/R9/query?")
    assert "minorversion=65" in url
    assert "from+CompanyInfo" in url or "from%20CompanyInfo" in url
    assert headers["Authorization"] == "Bearer tok-abc"


def test_api_client_bank_accounts() -> None:
    from rgnr8_qbo import QboApiClient

    http = FakeHttp([_qr({"Account": [
        {"Id": "35", "Name": "Checking", "AccountType": "Bank", "CurrentBalance": 1201.00},
        {"Id": "36", "Name": "Savings", "AccountType": "Bank", "CurrentBalance": 800.50},
    ]})])
    client = QboApiClient(http=http, api_base="https://x", realm_id="R", access_token="t")
    banks = client.bank_accounts()
    assert [b.name for b in banks] == ["Checking", "Savings"]
    assert banks[0].current_balance == "1201.0"
    assert banks[1].current_balance == "800.5"


def test_api_client_open_invoices_and_bills() -> None:
    from rgnr8_qbo import QboApiClient

    http = FakeHttp([
        _qr({"Invoice": [
            {"Id": "1", "DocNumber": "1001", "Balance": 150.00, "TxnDate": "2026-07-01",
             "DueDate": "2026-07-31", "CustomerRef": {"value": "3", "name": "Amy's Bird Sanctuary"}},
        ]}),
        _qr({"Bill": [
            {"Id": "7", "Balance": 400.00, "TxnDate": "2026-07-05", "DueDate": "2026-08-04",
             "VendorRef": {"value": "9", "name": "Norton Lumber"}},
        ]}),
    ])
    client = QboApiClient(http=http, api_base="https://x", realm_id="R", access_token="t")
    invs = client.open_invoices()
    assert invs[0].customer == "Amy's Bird Sanctuary" and invs[0].balance == "150.0"
    bills = client.open_bills()
    assert bills[0].vendor == "Norton Lumber" and bills[0].due_date == "2026-08-04"


def test_api_client_raises_on_error_status() -> None:
    from rgnr8_qbo import QboApiClient, QboApiError

    http = FakeHttp([HttpResponse(401, '{"fault":"unauthorized"}')])
    client = QboApiClient(http=http, api_base="https://x", realm_id="R", access_token="stale")
    with pytest.raises(QboApiError):
        client.bank_accounts()


def test_api_client_empty_query_response_is_safe() -> None:
    from rgnr8_qbo import QboApiClient

    http = FakeHttp([_qr({})])  # no rows
    client = QboApiClient(http=http, api_base="https://x", realm_id="R", access_token="t")
    assert client.bank_accounts() == []


def test_service_api_client_refreshes_and_builds() -> None:
    # a connected tenant whose access token is near expiry -> refreshed, then a
    # working api client is returned wired to the fresh token.
    http = FakeHttp([
        FakeHttp()._token_blob("fresh-access", "fresh-refresh"),   # refresh
        _qr({"Account": [{"Id": "1", "Name": "Checking", "AccountType": "Bank",
                          "CurrentBalance": 100.0}]}),               # query
    ])
    store = InMemoryConnectionStore()
    _connect(store, access_exp=T0 + timedelta(seconds=10), refresh_exp=T0 + timedelta(days=80))
    svc = _service(http, store)
    client = svc.api_client("acme")
    assert client is not None
    banks = client.bank_accounts()
    assert banks[0].name == "Checking"
    # the query used the refreshed token
    assert any("Bearer fresh-access" == h.get("Authorization") for _u, _b, h in http.calls)


def test_service_api_client_none_when_not_connected() -> None:
    svc = _service(FakeHttp(), InMemoryConnectionStore())
    assert svc.api_client("nobody") is None
