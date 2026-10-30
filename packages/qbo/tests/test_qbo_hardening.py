"""QBO connection hardening: retry and pacing on the data API, the pinned minor
version, refresh-token rotation, the single refresh on a 401, invalid_grant as
needs-reconnect, and the per-tenant refresh guard. All against fakes: no network,
no real credentials, and no real sleeping (sleeps are recorded by a fake clock)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

import pytest
from rgnr8_qbo import (
    MINOR_VERSION,
    HttpResponse,
    InMemoryConnectionStore,
    QboApiClient,
    QboApiError,
    QboConnection,
    QboConnectService,
    QboEnvironment,
    QboInvalidGrantError,
    QboNetworkError,
    QboOAuthConfig,
    QboStatus,
    RetryPolicy,
    SqlConnectionStore,
)

API_BASE = "https://sandbox-quickbooks.api.intuit.com"
CONFIG = QboOAuthConfig(
    client_id="ABCclient",
    client_secret="s3cret",
    redirect_uri="http://localhost:8000/oauth/qbo/callback",
    environment=QboEnvironment.SANDBOX,
)
T0 = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
INVALID_GRANT = HttpResponse(400, json.dumps({"error": "invalid_grant",
                                              "error_description": "Token invalid"}))
UNAUTHORIZED = HttpResponse(401, json.dumps({"Fault": {"Error": [{"Message": "AuthenticationFailed"}]}}))


class FakeClock:
    """A clock that only moves when something sleeps; every sleep is recorded."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


class ScriptedHttp:
    """Serves queued GET and POST responses (an exception in the queue is raised);
    records every call. Unqueued GETs answer an empty QueryResponse, unqueued POSTs
    answer a fresh token pair."""

    def __init__(self, gets: list[object] | None = None, posts: list[HttpResponse] | None = None) -> None:
        self.gets = list(gets or [])
        self.posts = list(posts or [])
        self.get_urls: list[str] = []
        self.get_headers: list[Mapping[str, str]] = []
        self.post_bodies: list[str] = []

    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        self.get_urls.append(url)
        self.get_headers.append(dict(headers))
        item = self.gets.pop(0) if self.gets else _qr({})
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, HttpResponse)
        return item

    def post(self, url: str, body: str, headers: Mapping[str, str]) -> HttpResponse:
        self.post_bodies.append(body)
        return self.posts.pop(0) if self.posts else _token("access-new", "refresh-new")


def _token(access: str, refresh: str) -> HttpResponse:
    return HttpResponse(200, json.dumps({
        "access_token": access, "refresh_token": refresh, "expires_in": 3600,
        "x_refresh_token_expires_in": 8_640_000, "token_type": "bearer",
    }))


def _qr(body: dict) -> HttpResponse:
    return HttpResponse(200, json.dumps({"QueryResponse": body}))


def _policy(clock: FakeClock, **kw: object) -> RetryPolicy:
    # jitter 0.5 is the middle of the [0.5, 1.5) range, so back-offs are 1, 2, 4, 8 s.
    return RetryPolicy(sleep=clock.sleep, clock=lambda: clock.now.timestamp(),
                       jitter=lambda: 0.5, **kw)  # type: ignore[arg-type]


def _client(http: ScriptedHttp, clock: FakeClock, **kw: object) -> QboApiClient:
    return QboApiClient(http=http, api_base=API_BASE, realm_id="R9", access_token="tok-1",
                        retry=_policy(clock), **kw)  # type: ignore[arg-type]


def _connect(store: object, *, access_exp: datetime, refresh_exp: datetime = T0 + timedelta(days=90),
             access: str = "a0", refresh: str = "r0") -> None:
    store.save(QboConnection(  # type: ignore[attr-defined]
        tenant_id="acme", realm_id="R42", access_token=access, refresh_token=refresh,
        access_expires_at=access_exp, refresh_expires_at=refresh_exp,
        status=QboStatus.CONNECTED, connected_at=T0,
    ))


def _service(http: ScriptedHttp, store: object, clock: FakeClock, **kw: object) -> QboConnectService:
    return QboConnectService(CONFIG, http, store, state_secret="state-key",  # type: ignore[arg-type]
                             clock=clock, retry=_policy(clock), **kw)  # type: ignore[arg-type]


# --- 1. rate limits and transient errors -------------------------------------
def test_429_honours_retry_after_then_succeeds() -> None:
    clock = FakeClock()
    http = ScriptedHttp(gets=[HttpResponse(429, "{}", retry_after="2"),
                              _qr({"CompanyInfo": [{"CompanyName": "Sandbox Co"}]})])
    assert _client(http, clock).company_info().name == "Sandbox Co"
    assert clock.slept == [2.0]
    assert len(http.get_urls) == 2


def test_503_three_times_then_200_backs_off_and_succeeds() -> None:
    clock = FakeClock()
    bad = HttpResponse(503, "{}")
    http = ScriptedHttp(gets=[bad, bad, bad, _qr({"CompanyInfo": [{"CompanyName": "Sandbox Co"}]})])
    assert _client(http, clock).company_info().name == "Sandbox Co"
    assert clock.slept == [1.0, 2.0, 4.0]          # exponential, jitter pinned
    assert len(http.get_urls) == 4


def test_six_consecutive_503s_raise_after_the_attempt_cap() -> None:
    clock = FakeClock()
    http = ScriptedHttp(gets=[HttpResponse(503, "{}")] * 6)
    with pytest.raises(QboApiError) as exc:
        _client(http, clock).company_info()
    assert exc.value.status == 503
    assert len(http.get_urls) == 5                 # the cap: five attempts, then give up
    assert clock.slept == [1.0, 2.0, 4.0, 8.0]


def test_400_is_not_retried() -> None:
    clock = FakeClock()
    http = ScriptedHttp(gets=[HttpResponse(400, '{"Fault":{}}')])
    with pytest.raises(QboApiError) as exc:
        _client(http, clock).company_info()
    assert exc.value.status == 400
    assert len(http.get_urls) == 1 and clock.slept == []


def test_connection_errors_and_timeouts_are_retried() -> None:
    clock = FakeClock()
    http = ScriptedHttp(gets=[QboNetworkError("timed out"),
                              _qr({"CompanyInfo": [{"CompanyName": "Sandbox Co"}]})])
    assert _client(http, clock).company_info().name == "Sandbox Co"
    assert clock.slept == [1.0]


def test_a_network_error_that_never_clears_raises_once_the_cap_is_hit() -> None:
    clock = FakeClock()
    http = ScriptedHttp(gets=[QboNetworkError("down")] * 5)
    with pytest.raises(QboNetworkError):
        _client(http, clock).company_info()
    assert len(http.get_urls) == 5


def test_retrying_stops_when_the_total_wait_would_be_exceeded() -> None:
    clock = FakeClock()
    http = ScriptedHttp(gets=[HttpResponse(503, "{}")] * 5)
    client = QboApiClient(http=http, api_base=API_BASE, realm_id="R9", access_token="t",
                          retry=_policy(clock, max_total_wait=1.5))
    with pytest.raises(QboApiError):
        client.company_info()
    assert clock.slept == [1.0]                    # the next 2 s wait would pass 1.5 s
    assert len(http.get_urls) == 2


def test_the_client_paces_itself_under_the_per_minute_limit() -> None:
    clock = FakeClock()
    http = ScriptedHttp()
    client = _client(http, clock)
    for _ in range(500):
        client.query("select * from CompanyInfo")
    assert clock.slept == []                       # 500 a minute is allowed
    client.query("select * from CompanyInfo")      # the 501st waits out the window
    assert clock.slept == [60.0]


def test_the_company_name_and_the_report_read_both_go_through_the_retry() -> None:
    clock = FakeClock()
    http = ScriptedHttp(gets=[HttpResponse(502, "{}"), HttpResponse(200, json.dumps({"Rows": {}}))])
    assert _client(http, clock).report("TrialBalance") == {"Rows": {}}
    assert clock.slept == [1.0]


# --- 2. minor version ---------------------------------------------------------
def test_minor_version_is_75_and_every_request_carries_it() -> None:
    assert MINOR_VERSION == "75"
    clock = FakeClock()
    http = ScriptedHttp(gets=[HttpResponse(429, "{}"), _qr({}), _qr({"Account": []}),
                              HttpResponse(200, json.dumps({"Rows": {}}))])
    client = _client(http, clock)
    client.company_info()          # a 429, then the answer
    client.bank_accounts()
    client.report("ProfitAndLoss", {"start_date": "2026-01-01"})
    assert len(http.get_urls) == 4
    for url in http.get_urls:
        assert parse_qs(urlsplit(url).query)["minorversion"] == ["75"], url


# --- 3. token rotation --------------------------------------------------------
def test_refresh_stores_the_rotated_refresh_token() -> None:
    clock = FakeClock()
    store = InMemoryConnectionStore()
    _connect(store, access_exp=T0 + timedelta(seconds=30))
    http = ScriptedHttp(posts=[_token("a1", "r1")])
    conn = _service(http, store, clock).ensure_fresh("acme")
    assert conn is not None and conn.refresh_token == "r1"
    assert store.get("acme").refresh_token == "r1"  # type: ignore[union-attr]
    assert "refresh_token=r0" in http.post_bodies[0]


def test_rotated_refresh_token_is_encrypted_at_rest_in_sql() -> None:
    pytest.importorskip("cryptography.fernet")
    from cryptography.fernet import Fernet
    from rgnr8_qbo import FernetCipher, is_encrypted

    clock = FakeClock()
    db = sqlite3.connect(":memory:")
    try:
        store = SqlConnectionStore(db, cipher=FernetCipher(Fernet.generate_key().decode()))
        store.create_schema()
        _connect(store, access_exp=T0 + timedelta(seconds=30))
        http = ScriptedHttp(posts=[_token("a1", "r1")])
        _service(http, store, clock).ensure_fresh("acme")
        raw = json.loads(db.execute("SELECT conn_json FROM rgnr8_qbo_connection").fetchone()[0])
        assert is_encrypted(raw["refresh_token"]) and is_encrypted(raw["access_token"])
        assert store.get("acme").refresh_token == "r1"  # type: ignore[union-attr]
    finally:
        db.close()


def test_refresh_happens_within_five_minutes_of_expiry_not_before() -> None:
    store = InMemoryConnectionStore()
    _connect(store, access_exp=T0 + timedelta(minutes=6))
    http = ScriptedHttp()
    conn = _service(http, store, FakeClock()).ensure_fresh("acme")
    assert conn is not None and conn.access_token == "a0" and http.post_bodies == []

    _connect(store, access_exp=T0 + timedelta(minutes=4))
    conn = _service(http, store, FakeClock()).ensure_fresh("acme")
    assert conn is not None and conn.access_token == "access-new" and len(http.post_bodies) == 1


def test_set_refresh_tokens_compare_and_swap_only_writes_the_expected_token() -> None:
    pytest.importorskip("cryptography.fernet")
    from cryptography.fernet import Fernet
    from rgnr8_qbo import FernetCipher

    def conn(refresh: str, status: QboStatus = QboStatus.CONNECTED) -> QboConnection:
        return QboConnection(tenant_id="acme", realm_id="R", access_token="a", refresh_token=refresh,
                             access_expires_at=T0, refresh_expires_at=T0, status=status)

    db = sqlite3.connect(":memory:")
    try:
        sql = SqlConnectionStore(db, cipher=FernetCipher(Fernet.generate_key().decode()))
        sql.create_schema()
        for store in (InMemoryConnectionStore(), sql):
            store.save(conn("r0"))
            assert store.replace_if_refresh_token("acme", "stale", conn("x", QboStatus.NEEDS_RECONNECT)) is False
            assert store.get("acme").refresh_token == "r0"  # type: ignore[union-attr]
            assert store.replace_if_refresh_token("acme", "r0", conn("r1", QboStatus.NEEDS_RECONNECT)) is True
            assert store.get("acme").status is QboStatus.NEEDS_RECONNECT  # type: ignore[union-attr]
    finally:
        db.close()


# --- 3b. a 401 on a data call: one refresh, one retry -------------------------
def test_401_on_a_data_call_refreshes_once_and_retries_once() -> None:
    clock = FakeClock()
    store = InMemoryConnectionStore()
    _connect(store, access_exp=T0 + timedelta(hours=1))
    http = ScriptedHttp(gets=[UNAUTHORIZED, _qr({"CompanyInfo": [{"CompanyName": "Sandbox Co"}]})],
                        posts=[_token("a1", "r1")])
    client = _service(http, store, clock).api_client("acme")
    assert client is not None
    assert client.company_info().name == "Sandbox Co"
    assert len(http.post_bodies) == 1                       # one refresh
    assert [h["Authorization"] for h in http.get_headers] == ["Bearer a0", "Bearer a1"]
    assert store.get("acme").refresh_token == "r1"  # type: ignore[union-attr]


def test_a_second_401_after_the_refresh_is_reported_not_looped() -> None:
    clock = FakeClock()
    store = InMemoryConnectionStore()
    _connect(store, access_exp=T0 + timedelta(hours=1))
    http = ScriptedHttp(gets=[UNAUTHORIZED, UNAUTHORIZED], posts=[_token("a1", "r1")])
    client = _service(http, store, clock).api_client("acme")
    assert client is not None
    with pytest.raises(QboApiError) as exc:
        client.company_info()
    assert exc.value.status == 401
    assert len(http.get_urls) == 2 and len(http.post_bodies) == 1


# --- 4. invalid_grant: needs-reconnect ----------------------------------------
def test_invalid_grant_marks_needs_reconnect_and_stops_calling_intuit() -> None:
    clock = FakeClock()
    store = InMemoryConnectionStore()
    _connect(store, access_exp=T0 + timedelta(seconds=30))
    marked: list[QboConnection] = []
    http = ScriptedHttp(posts=[INVALID_GRANT])
    svc = _service(http, store, clock, on_needs_reconnect=marked.append)

    conn = svc.ensure_fresh("acme")
    assert conn is not None and conn.status is QboStatus.NEEDS_RECONNECT
    assert store.get("acme").status is QboStatus.NEEDS_RECONNECT  # type: ignore[union-attr]
    assert [c.tenant_id for c in marked] == ["acme"]

    # no further calls of any kind: no refresh, no data call, no client
    assert svc.api_client("acme") is None
    assert svc.ensure_fresh("acme").status is QboStatus.NEEDS_RECONNECT  # type: ignore[union-attr]
    assert len(http.post_bodies) == 1 and http.get_urls == []
    assert len(marked) == 1


def test_invalid_grant_during_a_401_retry_raises_a_reconnect_error() -> None:
    clock = FakeClock()
    store = InMemoryConnectionStore()
    _connect(store, access_exp=T0 + timedelta(hours=1))
    http = ScriptedHttp(gets=[UNAUTHORIZED], posts=[INVALID_GRANT])
    client = _service(http, store, clock).api_client("acme")
    assert client is not None
    with pytest.raises(QboInvalidGrantError):
        client.company_info()
    assert store.get("acme").status is QboStatus.NEEDS_RECONNECT  # type: ignore[union-attr]


def test_a_successful_reconnect_clears_needs_reconnect() -> None:
    clock = FakeClock()
    store = InMemoryConnectionStore()
    _connect(store, access_exp=T0 + timedelta(seconds=30))
    http = ScriptedHttp(posts=[INVALID_GRANT, _token("a-new", "r-new"), _token("a-new", "r-new")])
    svc = _service(http, store, clock)
    svc.ensure_fresh("acme")
    assert svc.api_client("acme") is None

    state = svc.begin("acme").split("state=")[1]
    conn = svc.complete(state, "auth-code", "R42")
    assert conn.status is QboStatus.CONNECTED
    assert store.get("acme").status is QboStatus.CONNECTED  # type: ignore[union-attr]
    assert svc.api_client("acme") is not None


def test_a_rotation_by_another_caller_is_not_overwritten_by_a_needs_reconnect() -> None:
    clock = FakeClock()
    store = InMemoryConnectionStore()
    _connect(store, access_exp=T0 + timedelta(seconds=30))
    marked: list[QboConnection] = []

    class RotatedElsewhere(ScriptedHttp):
        def post(self, url: str, body: str, headers: Mapping[str, str]) -> HttpResponse:
            # Another worker rotates the token while this refresh is in flight.
            _connect(store, access_exp=T0 + timedelta(hours=1), refresh="r9", access="a9")
            return INVALID_GRANT

    svc = _service(RotatedElsewhere(), store, clock, on_needs_reconnect=marked.append)
    conn = svc.ensure_fresh("acme")
    assert conn is not None and conn.status is QboStatus.CONNECTED and conn.refresh_token == "r9"
    assert marked == []
    assert store.get("acme").status is QboStatus.CONNECTED  # type: ignore[union-attr]


# --- 5. one refresh for concurrent callers ------------------------------------
def test_concurrent_callers_refresh_the_token_once() -> None:
    store = InMemoryConnectionStore()
    _connect(store, access_exp=T0 + timedelta(seconds=30))

    class SlowRefresh(ScriptedHttp):
        def post(self, url: str, body: str, headers: Mapping[str, str]) -> HttpResponse:
            time.sleep(0.1)  # long enough for the second caller to arrive
            return super().post(url, body, headers)

    http = SlowRefresh()
    svc = QboConnectService(CONFIG, http, store, state_secret="state-key",
                            clock=lambda: T0, retry=RetryPolicy(sleep=lambda _s: None))
    start = threading.Barrier(2)
    results: list[str] = []

    def caller() -> None:
        start.wait()
        conn = svc.ensure_fresh("acme")
        results.append(conn.access_token if conn else "")

    threads = [threading.Thread(target=caller) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(http.post_bodies) == 1
    assert results == ["access-new", "access-new"]
