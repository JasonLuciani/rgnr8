"""In-shell owner screens: cash, briefing, close (advance + seal gate), packages."""

from collections.abc import Mapping
from datetime import date

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    CloseBoard,
    CloseTask,
    InMemoryUserDirectory,
    JwtAuthenticator,
    Request,
    Role,
    User,
    WebApp,
    sign_jwt,
)
from rgnr8_web.ledger_client import LedgerClient, LedgerResponse
from rgnr8_web.screens import default_close_board

SECRET = "screens-secret"
NOW = 1_760_000_000


def _inputs() -> ForecastInputs:
    return ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("90000.00")))


def _app(*, members: dict[str, Role] | None = None, audit: object | None = None) -> WebApp:
    users = InMemoryUserDirectory()
    members = members or {"owner@acme.com": Role.OWNER, "book@acme.com": Role.BOOKKEEPER,
                          "cpa@acme.com": Role.ACCOUNTANT, "view@acme.com": Role.VIEWER}
    for email, role in members.items():
        users.upsert_user(User(email, email, email.split("@")[0]))
        users.set_membership(email, "acme", role)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users,
                 session_secret=SECRET, session_clock=lambda: NOW, audit=audit)
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    return app


def _h(sub: str) -> dict[str, str]:
    return {"authorization": f"Bearer {sign_jwt({'sub': sub, 'tenant': 'acme', 'exp': NOW + 3600}, SECRET)}"}


def test_cash_page_is_in_shell() -> None:
    r = _app().handle(Request("GET", "/t/acme", _h("owner@acme.com")))
    assert r.status == 200 and r.content_type.startswith("text/html")
    assert "Cash outlook" in r.body and "<svg" in r.body      # chart + shell chrome
    assert 'class="rg-nav"' in r.body                          # wrapped in the shell nav
    assert "http://" not in r.body and "https://" not in r.body


def test_briefing_page_renders_in_shell() -> None:
    r = _app().handle(Request("GET", "/t/acme/briefing", _h("owner@acme.com")))
    assert r.status == 200
    assert "<h1>Weekly briefing</h1>" in r.body and "Ask your CFO" in r.body
    assert "13-week glance" in r.body
    assert "http://" not in r.body and "https://" not in r.body


def test_close_page_owner_can_seal_controls_visible() -> None:
    r = _app().handle(Request("GET", "/t/acme/close", _h("owner@acme.com")))
    assert r.status == 200 and "Month-end close" in r.body
    assert "Mark done" in r.body            # owner has MANAGE_CLOSE
    assert "Seal &amp; publish package" not in r.body  # not complete yet → no seal button


def test_close_page_bookkeeper_cannot_seal() -> None:
    app = _app()
    # complete every task so the seal gate is the only thing standing
    board = CloseBoard("2026-08", tuple(
        CloseTask(k, k, "done") for k in ("a", "b")), sealed=False)
    app.add_close("acme", board)
    r = app.handle(Request("GET", "/t/acme/close", _h("book@acme.com")))
    assert r.status == 200
    assert "Mark done" not in r.body  # all done already
    # ready-to-seal, but bookkeeper lacks PUBLISH_CLOSE → publish button withheld
    assert "Seal &amp; publish package" not in r.body
    assert "must publish" in r.body


def test_close_advance_then_publish_gate() -> None:
    app = _app()
    board = CloseBoard("2026-08", (CloseTask("only", "The one task", "open"),))
    app.add_close("acme", board)
    hb = {**_h("book@acme.com"), "content-type": "application/json"}
    # bookkeeper advances the task
    r = app.handle(Request("POST", "/api/acme/close", hb, '{"id":"only","status":"done"}'))
    assert r.status == 200
    import json
    assert json.loads(r.body)["complete"] is True
    # bookkeeper may NOT seal (no PUBLISH_CLOSE)
    assert app.handle(Request("POST", "/api/acme/close/publish", hb, "{}")).status == 403
    # owner seals it
    ho = {**_h("owner@acme.com"), "content-type": "application/json"}
    sealed = app.handle(Request("POST", "/api/acme/close/publish", ho, "{}"))
    assert sealed.status == 200 and json.loads(sealed.body)["sealed"] is True


def test_publish_rejected_until_complete() -> None:
    app = _app()
    app.add_close("acme", CloseBoard("2026-08", (
        CloseTask("a", "A", "done"), CloseTask("b", "B", "open"))))
    ho = {**_h("owner@acme.com"), "content-type": "application/json"}
    r = app.handle(Request("POST", "/api/acme/close/publish", ho, "{}"))
    assert r.status == 409  # a task is still open


class _FakeLedgerTransport:
    """Records the last request and replays a scripted response."""

    def __init__(self, status: int, body: dict[str, object]) -> None:
        self.status = status
        self.body = body
        self.calls: list[tuple[str, str, str]] = []

    def request(
        self, method: str, path: str, body: str, headers: "Mapping[str, str]",
    ) -> "LedgerResponse":
        self.calls.append((method, path, body))
        return LedgerResponse(self.status, self.body)


def test_publish_delegates_to_the_authoritative_ledger_when_wired() -> None:
    import json
    app = _app()
    app.add_close("acme", CloseBoard("2026-08", (CloseTask("only", "The one task", "done"),)))
    fake = _FakeLedgerTransport(200, {"period": "2026-08", "status": "PUBLISHED"})
    app.set_ledger(LedgerClient(fake))
    ho = {**_h("owner@acme.com"), "content-type": "application/json"}

    sealed = app.handle(Request("POST", "/api/acme/close/publish", ho, "{}"))
    assert sealed.status == 200 and json.loads(sealed.body)["sealed"] is True
    # It actually called the ledger's authoritative publish endpoint...
    assert len(fake.calls) == 1
    method, path, payload = fake.calls[0]
    assert method == "POST" and path == "/t/acme/close/publish"
    body = json.loads(payload)
    assert body["from"] == "2026-08-01" and body["to"] == "2026-08-31"
    assert body["published_by"] == "owner@acme.com"        # SoD identity threaded through
    assert body["period"] == "2026-08"


def test_publish_threads_the_preparer_for_separation_of_duties() -> None:
    import json
    app = _app()
    app.add_close("acme", CloseBoard("2026-08", (CloseTask("only", "The one task", "open"),)))
    fake = _FakeLedgerTransport(200, {"period": "2026-08", "status": "PUBLISHED"})
    app.set_ledger(LedgerClient(fake))
    # The bookkeeper prepares the close (advances the task)...
    hb = {**_h("book@acme.com"), "content-type": "application/json"}
    app.handle(Request("POST", "/api/acme/close", hb, '{"id":"only","status":"done"}'))
    # ...the owner publishes it.
    ho = {**_h("owner@acme.com"), "content-type": "application/json"}
    app.handle(Request("POST", "/api/acme/close/publish", ho, "{}"))

    body = json.loads(fake.calls[-1][2])
    # The authoritative publish is told BOTH identities, so the ledger can enforce
    # that the preparer is not also the publisher.
    assert body["prepared_by"] == "book@acme.com"
    assert body["published_by"] == "owner@acme.com"


def test_publish_surfaces_the_ledger_gate_rejection() -> None:
    import json
    app = _app()
    app.add_close("acme", CloseBoard("2026-08", (CloseTask("only", "The one task", "done"),)))
    # The authoritative ledger refuses (e.g. trial balance doesn't tie).
    fake = _FakeLedgerTransport(409, {"error": "close is blocked: Trial balance is in balance"})
    app.set_ledger(LedgerClient(fake))
    ho = {**_h("owner@acme.com"), "content-type": "application/json"}

    r = app.handle(Request("POST", "/api/acme/close/publish", ho, "{}"))
    assert r.status == 409
    assert "blocked" in json.loads(r.body)["error"]
    # The local board is NOT sealed when the authoritative layer refuses.
    state = json.loads(app.handle(Request("GET", "/api/acme/close",
                       {**_h("owner@acme.com")})).body)
    assert state["sealed"] is False


def test_viewer_cannot_reach_close() -> None:
    app = _app()
    # viewer lacks MANAGE_CLOSE → the close route is closed to them
    assert app.handle(Request("GET", "/t/acme/close", _h("view@acme.com"))).status == 403
    assert app.handle(Request("POST", "/api/acme/close", _h("view@acme.com"), '{"id":"x","status":"done"}')).status == 403


def test_packages_page_unconfigured_message() -> None:
    r = _app().handle(Request("GET", "/t/acme/packages", _h("owner@acme.com")))
    assert r.status == 200 and "Financial packages" in r.body
    assert "aren't configured" in r.body  # no package reader wired in this app


# --- the close checklist never reports a step done that nothing proves -------
def test_default_close_board_starts_with_nothing_done() -> None:
    board = default_close_board("2026-08")
    assert board.done == 0 and not board.complete
    feeds = next(t for t in board.tasks if t.key == "feeds")
    assert feeds.status != "done"
    assert "Not verified" in feeds.note


def test_tenant_with_no_feeds_sees_feeds_not_done() -> None:
    import json
    app = _app()  # no add_close(): the tenant runs on the default board
    state = json.loads(app.handle(Request("GET", "/api/acme/close", _h("owner@acme.com"))).body)
    feeds = next(t for t in state["tasks"] if t["key"] == "feeds")
    assert feeds["status"] == "open"
    assert state["done"] == 0 and state["complete"] is False
    r = app.handle(Request("GET", "/t/acme/close", _h("owner@acme.com")))
    assert r.status == 200
    assert "Not verified" in r.body
    assert "0/5" in r.body


def test_default_board_cannot_be_sealed_while_feeds_unverified() -> None:
    import json
    app = _app()
    ho = {**_h("owner@acme.com"), "content-type": "application/json"}
    r = app.handle(Request("POST", "/api/acme/close/publish", ho, "{}"))
    assert r.status == 409
    assert "every close task must be done" in json.loads(r.body)["error"]


# --- gate override: blocked close, role-gated override, recorded -------------
import json as _json  # noqa: E402

from rgnr8_web.audit import InMemoryAuditLog  # noqa: E402
from rgnr8_web.rbac import Permission, role_permissions  # noqa: E402

_OVERRIDE_REASON = "August bank statement arrives Monday; balance confirmed with the bank"


class _GateLedger:
    """Stands in for the ledger's close gate: a blocked close is refused with the
    failed checks; an override is accepted only for overridable checks, and the
    trial balance is refused even when an override is asked for."""

    def __init__(self, failed: list[dict[str, object]]) -> None:
        self.failed = failed
        self.calls: list[tuple[str, str, str]] = []

    def request(self, method: str, path: str, body: str, headers: "Mapping[str, str]") -> "LedgerResponse":
        self.calls.append((method, path, body))
        data = _json.loads(body)
        checks = [{"id": c["id"], "label": c["label"], "detail": c["detail"], "overridable": c["overridable"]}
                  for c in self.failed]
        if data.get("override") is True:
            if any(not c["overridable"] for c in self.failed):
                return LedgerResponse(409, {"error": "close is blocked: Trial balance is in balance "
                                                     "(cannot be overridden)", "checks": checks})
            return LedgerResponse(200, {"period": "2026-08", "status": "PUBLISHED", "override": {
                "by": data["published_by"], "at": "2026-09-01T17:00:00Z",
                "reason": data["override_reason"],
                "checks": [{"id": c["id"], "label": c["label"], "detail": c["detail"]} for c in self.failed]}})
        return LedgerResponse(409, {"error": "close is blocked: " + "; ".join(c["label"] for c in self.failed),
                                    "checks": checks})


_EMPTY_RECON = {"id": "bank_reconciliations", "label": "All bank/card accounts reconciled",
                "detail": "no bank/card reconciliations supplied for this period", "overridable": True,
                "passed": False}
_TRIAL = {"id": "trial_balance", "label": "Trial balance is in balance", "detail": "trial balance does not tie",
          "overridable": False, "passed": False}


def _gate_app(failed: list[dict[str, object]], audit: object | None = None) -> tuple[WebApp, _GateLedger]:
    app = _app(audit=audit)
    app.add_close("acme", CloseBoard("2026-08", (CloseTask("only", "The one task", "done"),)))
    ledger = _GateLedger(failed)
    app.set_ledger(LedgerClient(ledger))
    return app, ledger


def _post(app: WebApp, who: str, body: dict[str, object]) -> object:
    return app.handle(Request("POST", "/api/acme/close/publish",
                              {**_h(who), "content-type": "application/json"}, _json.dumps(body)))


def test_override_permission_is_held_by_owner_and_controller_only() -> None:
    p = Permission.OVERRIDE_CLOSE_GATE
    assert p in role_permissions(Role.OWNER) and p in role_permissions(Role.CONTROLLER)
    for role in (Role.BOOKKEEPER, Role.ACCOUNTANT, Role.VIEWER):
        assert p not in role_permissions(role), role


def test_blocked_close_shows_failed_checks_and_override_form_to_a_holder() -> None:
    app, _ = _gate_app([_EMPTY_RECON])
    r = _post(app, "owner@acme.com", {})
    assert r.status == 409 and _json.loads(r.body)["checks"][0]["overridable"] is True
    page = app.handle(Request("GET", "/t/acme/close", _h("owner@acme.com"))).body
    assert "Close is blocked" in page and "All bank/card accounts reconciled" in page
    assert "Override and close" in page and 'id="overrideReason"' in page


def test_blocked_close_tells_a_user_without_the_permission_who_can_override() -> None:
    app, _ = _gate_app([_EMPTY_RECON])
    assert _post(app, "cpa@acme.com", {}).status == 409  # accountant may publish, not override
    page = app.handle(Request("GET", "/t/acme/close", _h("cpa@acme.com"))).body
    assert "Close is blocked" in page and "owner or controller" in page
    assert "Override and close" not in page and 'id="overrideReason"' not in page


def test_override_with_reason_seals_and_records_who_when_reason_and_checks() -> None:
    log = InMemoryAuditLog()
    app, ledger = _gate_app([_EMPTY_RECON], audit=log)
    r = _post(app, "owner@acme.com", {"override": True, "override_reason": _OVERRIDE_REASON})
    assert r.status == 200, r.body
    assert _json.loads(r.body)["override"]["checks"] == ["All bank/card accounts reconciled"]
    # The ledger was asked to record the override with the reason.
    sent = _json.loads(ledger.calls[-1][2])
    assert sent["override"] is True and sent["override_reason"] == _OVERRIDE_REASON
    # The sealed period shows who overrode, and why.
    page = app.handle(Request("GET", "/t/acme/close", _h("owner@acme.com"))).body
    assert f"Overridden by owner@acme.com: {_OVERRIDE_REASON}" in page
    assert "Close is blocked" not in page
    # And the audit log holds the who/when/why/what.
    (ev,) = [e for e in log.events(tenant_id="acme") if e.action == "close.gate_overridden"]
    assert ev.actor == "owner@acme.com" and ev.at == NOW and ev.target == "2026-08"
    assert _OVERRIDE_REASON in ev.detail and "All bank/card accounts reconciled" in ev.detail


def test_override_without_a_reason_is_refused_before_the_ledger() -> None:
    app, ledger = _gate_app([_EMPTY_RECON])
    r = _post(app, "owner@acme.com", {"override": True, "override_reason": "   "})
    assert r.status == 400
    assert ledger.calls == []


def test_override_posted_directly_by_a_user_without_the_permission_is_403() -> None:
    app, ledger = _gate_app([_EMPTY_RECON])
    # the accountant can publish a clean close, but may not override a failed one
    for who in ("cpa@acme.com", "book@acme.com", "view@acme.com"):
        r = _post(app, who, {"override": True, "override_reason": _OVERRIDE_REASON})
        assert r.status == 403, who
    assert ledger.calls == []  # never reached the ledger
    state = _json.loads(app.handle(Request("GET", "/api/acme/close", _h("owner@acme.com"))).body)
    assert state["sealed"] is False


def test_unbalanced_trial_balance_cannot_be_overridden_by_anyone() -> None:
    log = InMemoryAuditLog()
    app, _ = _gate_app([_TRIAL, _EMPTY_RECON], audit=log)
    r = _post(app, "owner@acme.com", {"override": True, "override_reason": _OVERRIDE_REASON})
    assert r.status == 409 and "cannot be overridden" in _json.loads(r.body)["error"]
    page = app.handle(Request("GET", "/t/acme/close", _h("owner@acme.com"))).body
    assert "cannot be overridden" in page and "Override and close" not in page
    assert log.events(tenant_id="acme") == []
    state = _json.loads(app.handle(Request("GET", "/api/acme/close", _h("owner@acme.com"))).body)
    assert state["sealed"] is False


def test_override_needs_the_ledger_it_overrides() -> None:
    app = _app()
    app.add_close("acme", CloseBoard("2026-08", (CloseTask("only", "The one task", "done"),)))
    r = _post(app, "owner@acme.com", {"override": True, "override_reason": _OVERRIDE_REASON})
    assert r.status == 400 and "ledger" in _json.loads(r.body)["error"]
