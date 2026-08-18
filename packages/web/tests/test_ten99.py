"""1099 contractor tracking on the owner-facing screens.

The screen's job is not to render a total. It is to make three things
impossible to miss: who you owe a form to and can't file for, who is about to
join that list, and what money left the bank to a contractor without going
through a bill.
"""

import json
from datetime import date
from typing import Any

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryAuditLog, InMemoryUserDirectory, JwtAuthenticator, LedgerClient,
    LedgerResponse, Request, Role, User, WebApp, sign_jwt,
)

SECRET = "ten99-secret"
NOW = 1_760_000_000        # 2025-10-09; the tenant's as-of date is 2026-08-31


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


REPORT = {
    "contract": "form-1099/1", "year": 2025, "currency": "USD",
    "threshold_minor": "60000",
    "rows": [
        {"vendor_id": "dana", "vendor_name": "Dana Ruiz Design", "tax_id": "",
         "amount_minor": "400000", "needs_w9": True},
        {"vendor_id": "kai", "vendor_name": "Kai Studio", "tax_id": "98-7654321",
         "amount_minor": "250000", "needs_w9": False},
    ],
    "total_minor": "650000",
    "missing_tax_id": ["dana"],
    "below_threshold": [
        {"vendor_id": "sam", "vendor_name": "Sam Okonkwo",
         "amount_minor": "54000", "short_by_minor": "6000"},
    ],
    "possibly_missing": [
        {"vendor_name": "Dana Ruiz Design", "date": "2025-09-04",
         "amount_minor": "-120000", "description": "TRANSFER TO DANA"},
    ],
}

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/1099/2025": (200, REPORT),
    "GET /t/acme/bills": (200, {"documents": [], "open_total_minor": "0"}),
    "GET /t/acme/vendors": (200, {"parties": []}),
    "POST /t/acme/vendors": (201, {"party": {"id": "dana"}}),
    "GET /t/acme/attachments/counts": (200, {"counts": {}}),
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


# --- the report --------------------------------------------------------------

def test_the_year_defaults_to_the_one_just_ended() -> None:
    """1099s are filed in January for last year, so "this year" is the wrong default."""
    app, transport, _a = _app()
    r = _req(app, "/t/acme/books/1099")
    assert r.status == 200
    assert any("/1099/2025" in c[1] for c in transport.calls)


def test_a_missing_w9_leads_the_page_with_why_it_matters_now() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/1099?year=2025")
    assert "You owe a 1099 to Dana Ruiz Design and have no tax id on file" in r.body
    assert "collected in January it is a search for someone who has moved on" in r.body
    assert "W-9 needed" in r.body


def test_amounts_are_shown_even_when_the_form_cannot_be_filed() -> None:
    """The amount is what makes a missing W-9 urgent; hiding it makes the
    warning read as bureaucracy."""
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/1099?year=2025")
    assert "$4,000.00" in r.body      # Dana, no tax id
    assert "$2,500.00" in r.body      # Kai, filable
    assert "$6,500.00" in r.body      # the total


def test_everything_in_order_says_so_rather_than_warning() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/1099/2025"] = (200, dict(
        REPORT, missing_tax_id=[], possibly_missing=[],
        rows=[REPORT["rows"][1]],  # type: ignore[index]
    ))
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/1099?year=2025")
    assert "1 form(s) to file for 2025, all with a tax id" in r.body
    assert "no tax id on file" not in r.body


def test_nobody_over_the_threshold_says_so() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/1099/2025"] = (200, dict(
        REPORT, rows=[], total_minor="0", missing_tax_id=[], possibly_missing=[],
    ))
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/1099?year=2025")
    assert "Nobody was paid enough to need a form this year" in r.body


# --- the two warnings that earn their place ---------------------------------

def test_below_threshold_shows_how_far_short_and_why_it_is_listed() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/1099?year=2025")
    assert "Tracked, but under the threshold" in r.body
    assert "Sam Okonkwo" in r.body
    assert "$540.00" in r.body and "$60.00" in r.body
    assert "one more invoice moves them into the list above" in r.body


def test_unbilled_bank_payments_are_a_prompt_not_a_number() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/1099?year=2025")
    assert "Paid from the bank, never billed" in r.body
    assert "TRANSFER TO DANA" in r.body and "-$1,200.00" in r.body
    assert "not counted above" in r.body or "not\ncounted above" in r.body
    assert "guessing produces a total you cannot defend" in r.body


def test_no_warnings_means_those_sections_are_absent() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/1099/2025"] = (200, dict(
        REPORT, below_threshold=[], possibly_missing=[],
    ))
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/1099?year=2025")
    assert "Tracked, but under the threshold" not in r.body
    assert "Paid from the bank, never billed" not in r.body


def test_a_service_outage_is_reported() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/1099/2025"] = (503, {"error": "connection refused"})
    app, _t, _a = _app(routes)
    assert "1099 report unavailable" in _req(app, "/t/acme/books/1099?year=2025").body


# --- flagging a contractor ---------------------------------------------------

def test_the_vendor_form_asks_about_1099_status_and_says_why() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/bills")
    assert "1099 contractor?" in r.body
    assert 'name="tax_id"' in r.body
    assert "Flag them now even without the tax id" in r.body


def test_a_customer_is_never_asked_about_1099_status() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/invoices"] = (200, {"documents": [], "open_total_minor": "0"})
    routes["GET /t/acme/customers"] = (200, {"parties": []})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/invoices")
    assert "1099 contractor?" not in r.body


def test_flagging_a_vendor_sends_the_flag_and_the_tax_id() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/vendors", method="POST",
         body="name=Dana+Ruiz+Design&is_1099=1&tax_id=12-3456789")
    sent = json.loads([c for c in transport.calls if c[0] == "POST"][0][2])
    assert sent["is_1099"] is True
    assert sent["tax_id"] == "12-3456789"


def test_a_contractor_can_be_flagged_before_the_w9_arrives() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/vendors", method="POST", body="name=Dana&is_1099=1")
    sent = json.loads([c for c in transport.calls if c[0] == "POST"][0][2])
    assert sent["is_1099"] is True
    assert "tax_id" not in sent


def test_the_bills_screen_links_to_the_1099_report() -> None:
    app, _t, _a = _app()
    assert "/t/acme/books/1099" in _req(app, "/t/acme/bills").body


def test_a_viewer_can_read_the_report() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/1099?year=2025", sub="u-view")
    assert r.status == 200
    assert "Dana Ruiz Design" in r.body
