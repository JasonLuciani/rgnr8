"""Public-track additions: owner-held data reset (the export's twin), the owner-gated
audit-log viewer (JSON + in-shell page), and the cached-briefing perf win."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    BankTransaction,
    CloseBoard,
    CloseTask,
    InMemoryAuditLog,
    InMemoryUserDirectory,
    JwtAuthenticator,
    Request,
    Role,
    User,
    WebApp,
    sign_jwt,
)

SECRET = "erase-secret"
NOW = 1_760_000_000


def _inputs() -> ForecastInputs:
    return ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31),
                                               available=Money.from_decimal("90000.00")))


def _app(*, audit: InMemoryAuditLog | None = None) -> tuple[WebApp, InMemoryAuditLog | None]:
    users = InMemoryUserDirectory()
    for email, role in {"owner@acme.com": Role.OWNER, "view@acme.com": Role.VIEWER}.items():
        users.upsert_user(User(email, email, email.split("@")[0]))
        users.set_membership(email, "acme", role)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users,
                 session_secret=SECRET, session_clock=lambda: NOW, audit=audit)
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    app.add_transactions("acme", [
        BankTransaction("t1", "2026-08-15", "Deposit", Money.from_decimal("5000.00"),
                        category="Sales income", status="matched"),
        BankTransaction("t2", "2026-08-16", "Card", Money.from_decimal("-42.00")),
    ])
    app.add_close("acme", CloseBoard("2026-08", (CloseTask("a", "A", "open"),)))
    return app, audit


def _h(sub: str) -> dict[str, str]:
    return {"authorization": f"Bearer {sign_jwt({'sub': sub, 'tenant': 'acme', 'exp': NOW + 3600}, SECRET)}",
            "content-type": "application/json"}


# --- retention ---------------------------------------------------------------


def test_audit_purge_older_than_removes_only_aged_rows_for_the_tenant() -> None:
    log = InMemoryAuditLog()
    log.record("a", "x", 100, tenant_id="acme")
    log.record("a", "y", 500, tenant_id="acme")
    log.record("a", "z", 100, tenant_id="beta")  # another tenant, also old
    removed = log.purge_older_than("acme", before_at=200)
    assert removed == 1, "only acme's pre-200 row is purged"
    assert [e.action for e in log.events(tenant_id="acme")] == ["y"]
    assert len(log.events(tenant_id="beta")) == 1, "another tenant's rows are untouched"


def test_retention_sweep_is_gated_by_manage_data_retention() -> None:
    audit = InMemoryAuditLog()
    app, _ = _app(audit=audit)
    # owner holds MANAGE_DATA_RETENTION → the sweep runs (no ledger here, so 0 days,
    # nothing purged, but a 200 and a retention.swept audit event)
    r = app.handle(Request("POST", "/api/acme/retention", _h("owner@acme.com")))
    assert r.status == 200
    assert json.loads(r.body)["purged"] == 0
    assert any(e.action == "retention.swept" for e in audit.events(tenant_id="acme"))
    # a viewer cannot run it
    assert app.handle(Request("POST", "/api/acme/retention", _h("view@acme.com"))).status == 403


# --- erasure -----------------------------------------------------------------


def test_erase_is_owner_gated_purges_and_audits() -> None:
    audit = InMemoryAuditLog()
    app, _ = _app(audit=audit)
    # seed a decision so there is owner state to purge
    app.handle(Request("POST", "/api/acme/decisions", _h("owner@acme.com"), '{"label":"paid rent"}'))

    r = app.handle(Request("POST", "/api/acme/erase", _h("owner@acme.com")))
    assert r.status == 200
    erased = json.loads(r.body)["cleared"]
    assert erased["transactions"] == 2
    assert erased["decisions"] == 1
    assert erased["close_board"] is True
    assert erased["forecast_cache_cleared"] is True

    # the feed, close board, and decisions are gone now
    tx = json.loads(app.handle(Request("GET", "/api/acme/transactions", _h("owner@acme.com"))).body)
    assert tx["summary"]["total"] == 0
    dec = json.loads(app.handle(Request("GET", "/api/acme/decisions", _h("owner@acme.com"))).body)
    assert dec["decisions"] == []

    # a data.erased audit event was written for this tenant
    events = audit.events(tenant_id="acme")
    assert any(e.action == "data.erased" and e.actor == "owner@acme.com" for e in events)


def test_erase_is_idempotent() -> None:
    app, _ = _app(audit=InMemoryAuditLog())
    first = json.loads(app.handle(Request("POST", "/api/acme/erase", _h("owner@acme.com"))).body)
    assert first["cleared"]["transactions"] == 2
    second = json.loads(app.handle(Request("DELETE", "/api/acme/erase", _h("owner@acme.com"))).body)
    assert second["cleared"]["transactions"] == 0
    assert second["cleared"]["decisions"] == 0
    assert second["cleared"]["close_board"] is False


def test_erase_forbidden_for_viewer() -> None:
    app, _ = _app(audit=InMemoryAuditLog())
    assert app.handle(Request("POST", "/api/acme/erase", _h("view@acme.com"))).status == 403


def test_erase_without_audit_sink_still_succeeds() -> None:
    app, _ = _app(audit=None)
    assert app.handle(Request("POST", "/api/acme/erase", _h("owner@acme.com"))).status == 200


# --- audit-log viewer --------------------------------------------------------


def test_audit_json_most_recent_first_with_actor_filter() -> None:
    audit = InMemoryAuditLog()
    app, _ = _app(audit=audit)
    audit.record("owner@acme.com", "membership.set", NOW, tenant_id="acme", target="ben@acme.com")
    audit.record("ben@acme.com", "close.sealed", NOW + 1, tenant_id="acme", detail="2026-08")
    audit.record("owner@acme.com", "data.exported", NOW + 2, tenant_id="acme")
    # an event for another tenant must not leak in
    audit.record("x@other.com", "membership.set", NOW, tenant_id="other")

    r = app.handle(Request("GET", "/api/acme/audit", _h("owner@acme.com")))
    assert r.status == 200
    body = json.loads(r.body)
    actions = [e["action"] for e in body["events"]]
    assert actions == ["data.exported", "close.sealed", "membership.set"]  # most-recent-first
    assert all(e["seq"] for e in body["events"])

    # ?actor= filter
    filtered = json.loads(app.handle(Request(
        "GET", "/api/acme/audit?actor=owner@acme.com", _h("owner@acme.com"))).body)
    assert {e["actor"] for e in filtered["events"]} == {"owner@acme.com"}


def test_audit_json_501_without_sink() -> None:
    app, _ = _app(audit=None)
    assert app.handle(Request("GET", "/api/acme/audit", _h("owner@acme.com"))).status == 501


def test_audit_json_forbidden_for_viewer() -> None:
    app, _ = _app(audit=InMemoryAuditLog())
    assert app.handle(Request("GET", "/api/acme/audit", _h("view@acme.com"))).status == 403


def test_audit_page_renders_in_shell() -> None:
    audit = InMemoryAuditLog()
    app, _ = _app(audit=audit)
    audit.record("owner@acme.com", "membership.set", NOW, tenant_id="acme", target="ben@acme.com")
    r = app.handle(Request("GET", "/t/acme/audit", _h("owner@acme.com")))
    assert r.status == 200 and r.content_type.startswith("text/html")
    assert "Audit log" in r.body and "membership.set" in r.body
    assert 'class="rg-nav"' in r.body                       # wrapped in the shell
    assert "http://" not in r.body and "https://" not in r.body
    # viewer can't reach it
    assert app.handle(Request("GET", "/t/acme/audit", _h("view@acme.com"))).status == 403


# --- cached briefing (perf) --------------------------------------------------


def test_briefing_is_built_once_across_repeated_gets(monkeypatch: pytest.MonkeyPatch) -> None:
    import rgnr8_web.app as appmod

    real = appmod.build_briefing
    calls = {"n": 0}

    def counting(fc: Any) -> Any:
        calls["n"] += 1
        return real(fc)

    monkeypatch.setattr(appmod, "build_briefing", counting)

    app, _ = _app(audit=None)
    h = _h("owner@acme.com")
    for _ in range(3):
        assert app.handle(Request("GET", "/api/acme/today", h)).status == 200
    app.handle(Request("GET", "/app", h))
    assert calls["n"] == 1                                  # cached across every read

    # changing an assumption invalidates the forecast → the briefing rebuilds
    app.handle(Request("POST", "/api/acme/assumptions", h, '{"minimum_cash":"20000.00"}'))
    app.handle(Request("GET", "/api/acme/today", h))
    assert calls["n"] == 2                                  # recomputed exactly once more


def test_erase_response_is_honest_about_scope() -> None:
    # H2-3: the reset must not be presented as a full regulatory erasure.
    app, _ = _app()
    r = app.handle(Request("POST", "/api/acme/erase", _h("owner@acme.com")))
    body = json.loads(r.body)
    assert "NOT a full regulatory erasure" in body["scope"]
    retained = body["retained"]
    assert any("ledger" in x for x in retained)
    assert any("tax ID" in x for x in retained)
    assert any("audit" in x for x in retained)
