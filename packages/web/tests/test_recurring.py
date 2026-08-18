"""Recurring transactions on the owner-facing screens.

The design being defended is that nothing posts unattended, and the screen says
so. A rule that fires on its own keeps paying rent after the lease ends; the
whole value of a due list is that a person sees it before it becomes a journal
entry.
"""

import json
from datetime import date
from typing import Any

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryAuditLog, InMemoryUserDirectory, JwtAuthenticator, LedgerClient,
    LedgerResponse, Request, Role, User, WebApp, sign_jwt,
)

SECRET = "recur-secret"
NOW = 1_760_000_000


class FakeTransport:
    def __init__(self, routes: dict[str, tuple[int, dict[str, Any]]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, str]] = []

    def request(self, method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        self.calls.append((method, path, body))
        status, payload = self.routes.get(
            f"{method} {path.split('?')[0]}", (404, {"error": "not found"})
        )
        return LedgerResponse(status, payload)


ACCOUNTS = {
    "accounts": [
        {"code": "1000", "name": "Business Checking", "type": "ASSET", "subtype": "BANK"},
        {"code": "6300", "name": "Rent & Lease", "type": "EXPENSE"},
    ],
}

TEMPLATES = {
    "recurring": [
        {"id": "rent", "name": "Studio rent", "frequency": "MONTHLY", "interval": 1,
         "start_date": "2026-06-01", "end_date": "", "memo": "Riverside Properties",
         "active": True, "last_posted": "2026-07-01",
         "lines": [
             {"account_code": "6300", "side": "DEBIT", "amount_minor": "350000",
              "memo": "", "dimensions": {}},
             {"account_code": "1000", "side": "CREDIT", "amount_minor": "350000",
              "memo": "", "dimensions": {}},
         ]},
        {"id": "audit", "name": "Annual audit", "frequency": "YEARLY", "interval": 1,
         "start_date": "2026-01-15", "end_date": "2028-01-15", "memo": "",
         "active": False, "last_posted": "", "lines": []},
        {"id": "quarterly", "name": "Insurance", "frequency": "MONTHLY", "interval": 3,
         "start_date": "2026-01-01", "end_date": "", "memo": "",
         "active": True, "last_posted": "", "lines": []},
    ],
}

DUE = {
    "as_of": "2026-08-31",
    "due": [
        {"id": "rent", "name": "Studio rent", "date": "2026-08-01",
         "memo": "Riverside Properties", "amount_minor": "350000"},
    ],
}

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/accounts": (200, ACCOUNTS),
    "GET /t/acme/recurring": (200, TEMPLATES),
    "GET /t/acme/recurring/due": (200, DUE),
    "POST /t/acme/recurring": (201, {"recurring": TEMPLATES["recurring"][0]}),
    "POST /t/acme/recurring/run": (200, {
        "posted": 1, "skipped": 0,
        "entries": [{"id": "rent", "date": "2026-08-01", "entry_id": "acme:9"}],
        "failures": [],
    }),
    "DELETE /t/acme/recurring/rent": (200, {"removed": "rent"}),
}


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("0.00"))
    )


def _app(
    routes: dict[str, tuple[int, dict[str, Any]]] | None = None,
) -> tuple[WebApp, FakeTransport, InMemoryAuditLog]:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    users.upsert_user(User("u-view", "view@acme.com", "Viewer"))
    users.set_membership("u-view", "acme", Role.VIEWER)
    audit = InMemoryAuditLog()
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW),
                 users=users, audit=audit)
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("0.00")), token="unused")
    transport = FakeTransport(routes if routes is not None else dict(DEFAULT_ROUTES))
    app.set_ledger(LedgerClient(transport, token="svc-token"))
    return app, transport, audit


def _req(app: WebApp, path: str, sub: str = "u-owner",
         method: str = "GET", body: str = "") -> Any:
    tok = sign_jwt({"sub": sub, "tenant": "acme", "exp": NOW + 3600}, SECRET)
    headers = {"authorization": f"Bearer {tok}"}
    if method == "POST":
        headers["content-type"] = "application/x-www-form-urlencoded"
    return app.handle(Request(method, path, headers, body))


def _sent(transport: FakeTransport, needle: str) -> dict[str, Any]:
    return json.loads([c for c in transport.calls if c[0] == "POST" and needle in c[1]][0][2])


# --- what's due --------------------------------------------------------------

def test_the_screen_leads_with_what_is_due_and_says_nothing_posts_alone() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/recurring?as_of=2026-08-31")
    assert r.status == 200
    assert "1 due as of 2026-08-31" in r.body
    assert "Studio rent" in r.body and "$3,500.00" in r.body
    assert "Nothing posts on its own" in r.body
    assert "Post all 1" in r.body


def test_nothing_due_says_so_rather_than_showing_an_empty_table() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/recurring/due"] = (200, {"as_of": "2026-08-31", "due": []})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/recurring?as_of=2026-08-31")
    assert "Nothing due" in r.body
    assert "up to date as of 2026-08-31" in r.body
    assert "Post all" not in r.body


def test_schedules_read_as_english_not_as_a_cron_string() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/recurring")
    assert "every month from 2026-06-01" in r.body
    assert "every 3 months from 2026-01-01" in r.body
    assert "every year from 2026-01-15 until 2028-01-15" in r.body


def test_a_paused_template_is_shown_as_paused() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/recurring")
    assert "paused" in r.body
    assert "Annual audit" in r.body


def test_last_posted_is_visible_including_never() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/recurring")
    assert "2026-07-01" in r.body
    assert "never" in r.body


# --- memorizing --------------------------------------------------------------

def test_memorizing_parses_amounts_exactly_and_sends_the_schedule() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/books/recurring", method="POST",
             body="name=Studio+rent&frequency=MONTHLY&interval=1"
                  "&start_date=2026-06-01&memo=Riverside"
                  "&code1=6300&debit1=3,500.00&code2=1000&credit2=3500.00")
    assert r.status == 302
    sent = _sent(transport, "/recurring")
    assert sent["name"] == "Studio rent"
    assert sent["frequency"] == "MONTHLY"
    assert sent["start_date"] == "2026-06-01"
    assert sent["lines"] == [
        {"account_code": "6300", "side": "DEBIT", "amount_minor": "350000"},
        {"account_code": "1000", "side": "CREDIT", "amount_minor": "350000"},
    ]
    assert any(e.action == "recurring.saved" for e in audit.events(tenant_id="acme"))


def test_a_line_with_both_a_debit_and_a_credit_never_reaches_the_service() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/books/recurring", method="POST",
             body="name=X&start_date=2026-06-01&code1=6300&debit1=10&credit1=10")
    assert "not%20both" in str(r.headers.get("Location", ""))
    assert not [c for c in transport.calls if c[0] == "POST"]


def test_the_services_refusal_reaches_the_owner() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/recurring"] = (
        400, {"error": "this template doesn't balance — debits 350000 vs credits 340000"}
    )
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/recurring", method="POST",
             body="name=X&start_date=2026-06-01&code1=6300&debit1=3500&code2=1000&credit2=3400")
    assert "doesn%27t%20balance" in str(r.headers.get("Location", ""))


def test_the_form_explains_why_an_end_date_matters() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/recurring")
    assert "rent that stops with the lease and rent that posts forever" in r.body


# --- running -----------------------------------------------------------------

def test_running_reports_what_posted() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/books/recurring/run", method="POST", body="as_of=2026-08-31")
    assert r.status == 200
    assert "Posted 1" in r.body
    assert _sent(transport, "/recurring/run")["as_of"] == "2026-08-31"
    assert any(e.action == "recurring.run" for e in audit.events(tenant_id="acme"))


def test_a_failure_is_shown_with_its_reason_and_said_to_be_still_waiting() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/recurring/run"] = (200, {
        "posted": 2, "skipped": 1, "entries": [],
        "failures": [{"id": "rent", "date": "2026-07-01",
                      "error": "period 2026-07 is closed"}],
    })
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/recurring/run", method="POST", body="as_of=2026-08-31")
    assert "Posted 2" in r.body
    assert "1 couldn&#x27;t post and are still waiting" in r.body or \
           "1 couldn't post and are still waiting" in r.body
    assert "period 2026-07 is closed" in r.body
    assert "still listed as due, so nothing is lost" in r.body


def test_a_template_can_be_removed() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/books/recurring/rent/delete", method="POST")
    assert r.status == 302
    assert any(c[0] == "DELETE" for c in transport.calls)
    assert any(e.action == "recurring.removed" for e in audit.events(tenant_id="acme"))


# --- permissions -------------------------------------------------------------

def test_a_viewer_can_see_what_is_due_but_post_nothing() -> None:
    app, transport, _a = _app()
    page = _req(app, "/t/acme/books/recurring", sub="u-view")
    assert page.status == 200
    assert "Studio rent" in page.body
    assert "Post all" not in page.body
    assert "Memorize a transaction" not in page.body

    for path, body in [
        ("/t/acme/books/recurring", "name=X&start_date=2026-06-01"),
        ("/t/acme/books/recurring/run", "as_of=2026-08-31"),
        ("/t/acme/books/recurring/rent/delete", ""),
    ]:
        assert _req(app, path, sub="u-view", method="POST", body=body).status == 403
    assert not [c for c in transport.calls if c[0] in ("POST", "DELETE")]


def test_books_home_links_to_recurring() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/trial-balance"] = (200, {
        "currency": "USD", "in_balance": True, "rows": [],
        "total_debit_minor": "0", "total_credit_minor": "0",
    })
    routes["GET /t/acme/dimensions"] = (200, {"dimensions": []})
    app, _t, _a = _app(routes)
    assert "/t/acme/books/recurring" in _req(app, "/t/acme/books").body
