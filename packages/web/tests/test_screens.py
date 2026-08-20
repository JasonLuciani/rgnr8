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

SECRET = "screens-secret"
NOW = 1_760_000_000


def _inputs() -> ForecastInputs:
    return ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("90000.00")))


def _app(*, members: dict[str, Role] | None = None) -> WebApp:
    users = InMemoryUserDirectory()
    members = members or {"owner@acme.com": Role.OWNER, "book@acme.com": Role.BOOKKEEPER,
                          "cpa@acme.com": Role.ACCOUNTANT, "view@acme.com": Role.VIEWER}
    for email, role in members.items():
        users.upsert_user(User(email, email, email.split("@")[0]))
        users.set_membership(email, "acme", role)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users,
                 session_secret=SECRET, session_clock=lambda: NOW)
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
    assert "This week's briefing" in r.body and "Ask your CFO" in r.body
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
