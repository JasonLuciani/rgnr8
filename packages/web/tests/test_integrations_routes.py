"""Outbound-webhook management through the web app, and event emission on seal."""

from __future__ import annotations

from datetime import date

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    PENDING,
    CloseBoard,
    CloseTask,
    InMemoryUserDirectory,
    InMemoryWebhookEndpointStore,
    InMemoryWebhookOutbox,
    JwtAuthenticator,
    Request,
    Role,
    User,
    WebApp,
    sign_jwt,
)

SECRET = "integ-secret"
NOW = 1_760_000_000


def _app() -> tuple[WebApp, InMemoryWebhookEndpointStore, InMemoryWebhookOutbox]:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.upsert_user(User("u-view", "view@acme.com", "Viewer"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    users.set_membership("u-view", "acme", Role.VIEWER)
    endpoints = InMemoryWebhookEndpointStore()
    outbox = InMemoryWebhookOutbox()
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users,
                 session_secret=SECRET, session_clock=lambda: NOW,
                 webhooks=endpoints, webhook_outbox=outbox)
    app.add_tenant("acme", "Acme Co",
                   ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31),
                                                       available=Money.from_decimal("50000.00"))),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    return app, endpoints, outbox


def _req(app: WebApp, path: str, sub: str, method: str = "GET", body: str = "") -> object:
    tok = sign_jwt({"sub": sub, "tenant": "acme", "exp": NOW + 3600}, SECRET)
    return app.handle(Request(method, path, {"authorization": f"Bearer {tok}"}, body))


def test_owner_adds_an_endpoint_with_a_generated_secret() -> None:
    app, endpoints, _ = _app()
    r = _req(app, "/t/acme/integrations", "u-owner", "POST",
             "id=crm-sync&url=https://example.com/hooks/rgnr8&events=close.sealed")
    assert r.status in (302, 303)
    saved = endpoints.for_tenant("acme")
    assert len(saved) == 1
    assert saved[0].id == "crm-sync"
    assert saved[0].url == "https://example.com/hooks/rgnr8"
    assert saved[0].secret, "a signing secret was generated"
    # the one-time secret is surfaced back in the redirect for the admin to copy
    assert "secret=" in str(r.headers.get("Location", ""))


def test_a_non_https_or_internal_endpoint_is_refused() -> None:
    app, endpoints, _ = _app()
    r = _req(app, "/t/acme/integrations", "u-owner", "POST",
             "id=bad&url=http://169.254.169.254/meta")
    assert r.status in (302, 303)
    assert "err=" in str(r.headers.get("Location", ""))
    assert endpoints.for_tenant("acme") == [], "the SSRF target was never saved"


def test_sealing_a_close_enqueues_a_durable_webhook_delivery() -> None:
    app, endpoints, outbox = _app()
    # register an endpoint that wants close.sealed
    _req(app, "/t/acme/integrations", "u-owner", "POST",
         "id=crm-sync&url=https://example.com/hooks/rgnr8&events=close.sealed")
    # a one-task close board, complete it, then seal it
    app.add_close("acme", CloseBoard("2026-08", (CloseTask("a", "A", "done"),)))
    sealed = _req(app, "/api/acme/close/publish", "u-owner", "POST")
    assert sealed.status == 200

    # the seal put a durable delivery in the outbox, pending, not yet sent
    rows = outbox.for_tenant("acme")
    assert len(rows) == 1
    assert rows[0].event_type == "close.sealed" and rows[0].status == PENDING
    assert rows[0].endpoint_id == "crm-sync"
