"""Sales tax, credits and refunds on the invoicing screens.

The web layer's job here is narrow and exacting: turn "8.25" into 82500 parts
per million without ever touching a float, offer a credit OR a refund but never
both, and let the ledger's refusals reach the owner in words that say what they
should have done instead.
"""

import json
from datetime import date
from typing import Any

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryAuditLog, InMemoryUserDirectory, JwtAuthenticator, LedgerClient,
    LedgerResponse, Request, Role, User, WebApp, sign_jwt,
)

SECRET = "tax-secret"
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


PARTIES = {"parties": [{"id": "halcyon", "name": "Halcyon LLC", "termsDays": 30}]}

OPEN_TAXED = {
    "id": "INV-1", "kind": "invoice", "party_id": "halcyon",
    "date": "2026-08-01", "due_date": "2026-08-31",
    "net_minor": "100000", "tax_minor": "8250", "tax_rate_ppm": 82500,
    "total_minor": "108250", "open_minor": "108250", "status": "OPEN",
    "memo": "August work", "lines": [],
}
COLLECTED = dict(OPEN_TAXED, id="INV-2", open_minor="0", status="PAID",
                 tax_minor="0", tax_rate_ppm=0, net_minor="100000",
                 total_minor="100000")

DOCS = {
    "documents": [OPEN_TAXED, COLLECTED],
    "open_total_minor": "108250", "overdue_total_minor": "0",
}

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/invoices": (200, DOCS),
    "GET /t/acme/customers": (200, PARTIES),
    "POST /t/acme/invoices": (201, {"document": OPEN_TAXED}),
    "POST /t/acme/invoices/INV-1/credits": (200, {
        "document": dict(OPEN_TAXED, open_minor="83250", status="PARTIAL"),
        "entry_id": "acme:9", "applied_minor": "25000", "kind": "credit",
    }),
    "POST /t/acme/invoices/INV-2/refunds": (200, {
        "document": COLLECTED, "entry_id": "acme:10",
        "applied_minor": "30000", "kind": "refund",
    }),
}


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("3000.00"))
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
                   ForecastConfig(minimum_cash=Money.from_decimal("1000.00")), token="unused")
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


# --- sales tax ---------------------------------------------------------------

def test_a_percentage_becomes_exact_parts_per_million() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/invoices", method="POST",
         body="id=INV-9&party_id=halcyon&date=2026-08-01&tax_rate=8.25"
              "&amount1=1000.00&code1=4100")
    sent = _sent(transport, "/invoices")
    assert sent["tax_rate_ppm"] == 82500, "8.25% is 82500 ppm exactly"


def test_odd_but_legal_rates_survive_the_conversion() -> None:
    app, transport, _a = _app()
    for typed, ppm in [("7", 70000), ("6.5", 65000), ("8.8875", 88875), ("0", 0)]:
        transport.calls.clear()
        _req(app, "/t/acme/invoices", method="POST",
             body=f"id=X&party_id=halcyon&date=2026-08-01&tax_rate={typed}"
                  "&amount1=100.00&code1=4100")
        sent = _sent(transport, "/invoices")
        assert sent.get("tax_rate_ppm", 0) == ppm, f"{typed}% should be {ppm} ppm"


def test_a_nonsense_rate_never_reaches_the_service() -> None:
    app, transport, _a = _app()
    for typed in ["eight", "-1", "8.123456"]:
        transport.calls.clear()
        r = _req(app, "/t/acme/invoices", method="POST",
                 body=f"id=X&party_id=halcyon&date=2026-08-01&tax_rate={typed}"
                      "&amount1=100.00&code1=4100")
        assert "err=" in str(r.headers.get("Location", "")), typed
        assert not [c for c in transport.calls if c[0] == "POST"]


def test_the_list_shows_tax_split_out_from_the_net() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/invoices")
    assert r.status == 200
    assert "$1,000.00 + $82.50 sales tax (8.25%)" in r.body
    assert "$1,082.50" in r.body           # what they actually owe


def test_the_invoice_form_explains_that_tax_is_not_income() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/invoices")
    assert "Sales tax rate %" in r.body
    assert "the state&#x27;s money" in r.body or "the state's money" in r.body


def test_bills_are_not_offered_a_sales_tax_rate() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/bills"] = (200, {"documents": [], "open_total_minor": "0"})
    routes["GET /t/acme/vendors"] = (200, {"parties": [{"id": "v", "name": "V"}]})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/bills")
    assert "Sales tax rate %" not in r.body


# --- credits and refunds -----------------------------------------------------

def test_an_open_invoice_is_offered_a_credit_and_not_a_refund() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/invoices")
    assert "/t/acme/invoices/INV-1/credits" in r.body
    assert "/t/acme/invoices/INV-1/refunds" not in r.body


def test_a_collected_invoice_is_offered_a_refund_and_not_a_credit() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/invoices")
    assert "/t/acme/invoices/INV-2/refunds" in r.body
    assert "/t/acme/invoices/INV-2/credits" not in r.body


def test_crediting_sends_the_amount_and_reports_what_was_applied() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/invoices/INV-1/credits", method="POST",
             body="amount=250.00&memo=Overbilled")
    assert r.status == 302
    sent = _sent(transport, "/credits")
    assert sent["amount_minor"] == "25000"
    assert sent["memo"] == "Overbilled"
    assert "Credited%20250.00%20against%20INV-1" in str(r.headers.get("Location", ""))
    assert any(e.action == "arap.credit" for e in audit.events(tenant_id="acme"))


def test_a_blank_amount_credits_the_whole_balance() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/invoices/INV-1/credits", method="POST", body="amount=")
    sent = _sent(transport, "/credits")
    assert "amount_minor" not in sent, "the ledger defaults it to the full open balance"


def test_refunding_sends_the_amount_and_names_it_a_refund() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/invoices/INV-2/refunds", method="POST", body="amount=300.00")
    assert r.status == 302
    assert _sent(transport, "/refunds")["amount_minor"] == "30000"
    assert "Refunded%20300.00" in str(r.headers.get("Location", ""))
    assert any(e.action == "arap.refund" for e in audit.events(tenant_id="acme"))


def test_the_ledgers_refusal_tells_the_owner_what_they_wanted_instead() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/invoices/INV-1/credits"] = (400, {
        "error": "invoice INV-1 is already settled — money that has been paid "
                 "comes back as a refund, not a credit",
    })
    app, _t, audit = _app(routes)
    r = _req(app, "/t/acme/invoices/INV-1/credits", method="POST", body="amount=100.00")
    assert "refund%2C%20not%20a%20credit" in str(r.headers.get("Location", ""))
    assert not [e for e in audit.events(tenant_id="acme") if e.action == "arap.credit"]


def test_a_malformed_amount_never_reaches_the_service() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/invoices/INV-1/credits", method="POST", body="amount=lots")
    assert "not%20a%20valid%20amount" in str(r.headers.get("Location", ""))
    assert not [c for c in transport.calls if c[0] == "POST"]


def test_a_viewer_can_see_the_documents_but_adjust_nothing() -> None:
    app, transport, _a = _app()
    page = _req(app, "/t/acme/invoices", sub="u-view")
    assert page.status == 200
    assert "$1,082.50" in page.body
    assert "/credits" not in page.body and "/refunds" not in page.body
    for path in ["/t/acme/invoices/INV-1/credits", "/t/acme/invoices/INV-2/refunds"]:
        assert _req(app, path, sub="u-view", method="POST", body="amount=1").status == 403
    assert not [c for c in transport.calls if c[0] == "POST"]
