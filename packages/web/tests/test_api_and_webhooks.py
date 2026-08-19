"""Partner API keys, the OpenAPI spec, the new JSON endpoints, and outbound webhooks."""

import json
import sqlite3
from datetime import date
from typing import Any, cast

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    ApiKeyService,
    BankTransaction,
    CloseBoard,
    CloseTask,
    InMemoryApiKeyStore,
    InMemoryUserDirectory,
    InMemoryWebhookEndpointStore,
    JwtAuthenticator,
    Request,
    Role,
    SqlApiKeyStore,
    User,
    WebApp,
    WebhookDispatcher,
    WebhookEndpoint,
    close_sealed_event,
    sign_webhook,
)
from rgnr8_web.webhooks_out import HttpResponse

NOW = 1_760_000_000


def _app() -> tuple[WebApp, ApiKeyService]:
    users = InMemoryUserDirectory()
    users.upsert_user(User("owner@acme.com", "owner@acme.com", "Ada"))
    users.set_membership("owner@acme.com", "acme", Role.OWNER)
    keys = ApiKeyService(InMemoryApiKeyStore(), clock=lambda: NOW,
                         secret_factory=lambda: "s3cret")
    app = WebApp(authenticator=JwtAuthenticator("x", clock=lambda: NOW), users=users,
                 session_secret="x", session_clock=lambda: NOW, api_keys=keys)
    app.add_tenant("acme", "Acme Co",
                   ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31),
                                  available=Money.from_decimal("80000.00"))),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    app.add_transactions("acme", [
        BankTransaction("t1", "2026-08-15", "Deposit", Money.from_decimal("5000.00"),
                        category="Sales income", status="matched"),
        BankTransaction("t2", "2026-08-16", "Card charge", Money.from_decimal("-42.00"),
                        category="Uncategorized", status="review"),
    ])
    app.add_close("acme", CloseBoard("2026-08", (CloseTask("a", "A", "open"),)))
    return app, keys


def test_openapi_is_public_and_describes_routes() -> None:
    app, _ = _app()
    r = app.handle(Request("GET", "/openapi.json"))
    assert r.status == 200
    spec = json.loads(r.body)
    assert spec["openapi"].startswith("3.")
    assert "/api/{tenant}/today" in spec["paths"]
    assert spec["paths"]["/api/{tenant}/close/publish"]["post"]["x-permission"] == "publish_close"
    assert "bearerAuth" in spec["components"]["securitySchemes"]


def test_api_key_grants_scoped_access() -> None:
    app, keys = _app()
    _rec, plaintext = keys.issue("acme", "owner@acme.com", name="partner")
    assert plaintext.startswith("rgk_")
    # the key authenticates and resolves to the owner principal → today works
    r = app.handle(Request("GET", "/api/acme/today", {"authorization": f"Bearer {plaintext}"}))
    assert r.status == 200 and json.loads(r.body)["tenant"] == "acme"
    # also accepted via X-API-Key
    r2 = app.handle(Request("GET", "/api/acme/today", {"x-api-key": plaintext}))
    assert r2.status == 200


def test_revoked_key_is_rejected() -> None:
    app, keys = _app()
    rec, plaintext = keys.issue("acme", "owner@acme.com")
    keys.revoke(rec.key_id)
    r = app.handle(Request("GET", "/api/acme/today", {"authorization": f"Bearer {plaintext}"}))
    assert r.status == 401


def test_api_key_still_subject_to_rbac() -> None:
    # a key for a viewer principal can't categorize
    app, keys = _app()
    app._users.upsert_user(User("view@acme.com", "view@acme.com"))  # type: ignore[attr-defined]
    app._users.set_membership("view@acme.com", "acme", Role.VIEWER)  # type: ignore[attr-defined]
    _rec, plaintext = keys.issue("acme", "view@acme.com")
    r = app.handle(Request("POST", "/api/acme/transactions",
                           {"authorization": f"Bearer {plaintext}", "content-type": "application/json"},
                           '{"id":"t2","category":"Bank fees"}'))
    assert r.status == 403  # viewer lacks categorize_transactions


def test_transactions_and_close_json_endpoints() -> None:
    app, keys = _app()
    _rec, k = keys.issue("acme", "owner@acme.com")
    tx = json.loads(app.handle(Request("GET", "/api/acme/transactions", {"x-api-key": k})).body)
    assert tx["summary"]["total"] == 2 and len(tx["for_review"]) == 1
    cl = json.loads(app.handle(Request("GET", "/api/acme/close", {"x-api-key": k})).body)
    assert cl["period"] == "2026-08" and cl["complete"] is False


def test_sql_api_key_store_roundtrip() -> None:
    conn = sqlite3.connect(":memory:")
    store = SqlApiKeyStore(cast("Any", conn), placeholder="?")
    store.create_schema()
    svc = ApiKeyService(store, clock=lambda: NOW, secret_factory=lambda: "abc")
    _rec, plaintext = svc.issue("acme", "owner@acme.com", name="ci")
    # a fresh service on the same connection verifies the persisted key
    svc2 = ApiKeyService(SqlApiKeyStore(cast("Any", conn), placeholder="?"))
    assert svc2.verify(plaintext) == ("acme", "owner@acme.com")


def test_non_expiring_key_verifies_forever() -> None:
    now = {"t": NOW}
    svc = ApiKeyService(clock=lambda: now["t"])  # ttl_days defaults to 0 = never
    rec, plaintext = svc.issue("acme", "owner@acme.com")
    assert rec.expires_at == 0
    now["t"] = NOW + 3650 * 86_400  # ten years later
    assert svc.verify(plaintext) == ("acme", "owner@acme.com")


def test_expired_key_is_rejected() -> None:
    now = {"t": NOW}
    svc = ApiKeyService(clock=lambda: now["t"], ttl_days=30)
    rec, plaintext = svc.issue("acme", "owner@acme.com")
    assert rec.expires_at == NOW + 30 * 86_400
    # still valid one second before expiry
    now["t"] = rec.expires_at - 1
    assert svc.verify(plaintext) == ("acme", "owner@acme.com")
    # rejected at/after expiry
    now["t"] = rec.expires_at
    assert svc.verify(plaintext) is None
    now["t"] = rec.expires_at + 10 * 86_400
    assert svc.verify(plaintext) is None


def test_expiry_survives_sql_roundtrip() -> None:
    conn = sqlite3.connect(":memory:")
    store = SqlApiKeyStore(cast("Any", conn), placeholder="?")
    store.create_schema()
    now = {"t": NOW}
    svc = ApiKeyService(store, clock=lambda: now["t"], secret_factory=lambda: "abc", ttl_days=7)
    _rec, plaintext = svc.issue("acme", "owner@acme.com", name="ttl")
    # persisted expires_at is read back and enforced by a fresh service
    svc2 = ApiKeyService(SqlApiKeyStore(cast("Any", conn), placeholder="?"), clock=lambda: now["t"])
    now["t"] = NOW + 8 * 86_400
    assert svc2.verify(plaintext) is None


def test_outbound_webhook_signs_and_delivers() -> None:
    store = InMemoryWebhookEndpointStore()
    store.save(WebhookEndpoint("ep1", "acme", "https://hooks.acme.com/rgnr8", "whsec",
                               events=("close.sealed",)))
    sent: list[tuple[str, str, dict[str, str]]] = []

    class OkHttp:
        def post_json(self, url: str, body: str, headers: dict[str, str]) -> HttpResponse:
            sent.append((url, body, headers))
            return HttpResponse(200)

    disp = WebhookDispatcher(store, OkHttp(), clock=lambda: NOW)
    results = disp.dispatch(close_sealed_event("evt_1", "acme", NOW, period="2026-08"))
    assert len(results) == 1 and results[0].ok and results[0].attempts == 1
    url, body, headers = sent[0]
    assert url.endswith("/rgnr8")
    # signature verifies against the endpoint secret
    assert headers["X-RGNR8-Signature"] == sign_webhook(body, "whsec")
    assert headers["X-RGNR8-Event-Id"] == "evt_1"


def test_outbound_webhook_retries_then_reports_failure() -> None:
    store = InMemoryWebhookEndpointStore()
    store.save(WebhookEndpoint("ep1", "acme", "https://down.example", "s"))
    attempts = {"n": 0}

    class FailHttp:
        def post_json(self, url: str, body: str, headers: dict[str, str]) -> HttpResponse:
            attempts["n"] += 1
            return HttpResponse(503)

    disp = WebhookDispatcher(store, FailHttp(), clock=lambda: NOW, max_attempts=3)
    r = disp.dispatch(close_sealed_event("e", "acme", NOW, period="2026-08"))[0]
    assert r.ok is False and r.attempts == 3 and attempts["n"] == 3


# --- SSRF hardening ----------------------------------------------------------


class _RecordingHttp:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def post_json(self, url: str, body: str, headers: dict[str, str]) -> HttpResponse:
        self.calls.append(url)
        return HttpResponse(200)


def _dispatch_one(url: str, *, allow_hosts: list[str] | None = None) -> tuple[Any, list[str]]:
    store = InMemoryWebhookEndpointStore()
    store.save(WebhookEndpoint("ep1", "acme", url, "whsec", events=("close.sealed",)))
    http = _RecordingHttp()
    disp = WebhookDispatcher(store, http, clock=lambda: NOW, allow_hosts=allow_hosts)
    res = disp.dispatch(close_sealed_event("evt", "acme", NOW, period="2026-08"))[0]
    return res, http.calls


def test_webhook_ssrf_blocks_non_public_targets() -> None:
    blocked_urls = [
        "http://hooks.acme.com/x",        # non-https
        "https://localhost/x",            # loopback name
        "https://127.0.0.1/x",            # loopback IPv4
        "https://169.254.169.254/latest", # cloud metadata / link-local
        "https://10.0.0.5/x",             # private 10/8
        "https://172.16.0.1/x",           # private 172.16/12
        "https://192.168.1.10/x",         # private 192.168/16
        "https://[::1]/x",                # loopback IPv6
        "https://[fc00::1]/x",            # unique-local IPv6 (fc00::/7)
    ]
    for url in blocked_urls:
        res, calls = _dispatch_one(url)
        assert res.ok is False, url
        assert res.attempts == 0 and res.status == 0, url
        assert res.error.startswith("blocked:"), url
        assert calls == [], f"no request should be made for {url}"


def test_webhook_ssrf_allows_public_https_target() -> None:
    res, calls = _dispatch_one("https://hooks.acme.com/rgnr8")
    assert res.ok is True and res.attempts == 1
    assert calls == ["https://hooks.acme.com/rgnr8"]


def test_webhook_host_allowlist() -> None:
    # allowlisted host is delivered
    ok, ok_calls = _dispatch_one("https://hooks.acme.com/rgnr8", allow_hosts=["hooks.acme.com"])
    assert ok.ok is True and ok_calls == ["https://hooks.acme.com/rgnr8"]
    # a public host NOT on the allowlist is blocked without a request
    bad, bad_calls = _dispatch_one("https://evil.example/x", allow_hosts=["hooks.acme.com"])
    assert bad.ok is False and bad.error.startswith("blocked:") and bad_calls == []


def test_webhook_dedupe_within_and_across_runs() -> None:
    ep = WebhookEndpoint("ep1", "acme", "https://hooks.acme.com/rgnr8", "whsec",
                         events=("close.sealed",))

    class DupStore:
        def save(self, endpoint: WebhookEndpoint) -> None: ...
        def for_tenant(self, tenant_id: str) -> list[WebhookEndpoint]:
            return [ep, ep]  # same endpoint returned twice in one run

    http = _RecordingHttp()
    disp = WebhookDispatcher(DupStore(), http, clock=lambda: NOW)
    evt = close_sealed_event("evt_1", "acme", NOW, period="2026-08")
    first = disp.dispatch(evt)
    assert len(first) == 1 and len(http.calls) == 1     # deduped within the run
    # re-dispatching the same event delivers nothing new (idempotent)
    second = disp.dispatch(evt)
    assert second == [] and len(http.calls) == 1
