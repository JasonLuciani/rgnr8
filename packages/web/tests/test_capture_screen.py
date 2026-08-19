"""Receipt capture → drafted bill.

Paste receipt text, get the vendor/date/total pulled out and a bill drafted; a
poor read is flagged for review, and confirming creates the vendor (if new) and
the bill through the ordinary AP path.
"""

import json
from datetime import date
from typing import Any
from urllib.parse import urlencode

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryAuditLog,
    InMemoryUserDirectory,
    JwtAuthenticator,
    LedgerClient,
    LedgerResponse,
    Request,
    Role,
    User,
    WebApp,
    sign_jwt,
)

SECRET = "cap-secret"
NOW = 1_760_000_000

RECEIPT = "HOME DEPOT #4512\nDate: 03/14/2026\n3/4in Plywood 2 @ 48.00 96.00\nTax 8.68\nTOTAL 117.18"


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


ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/vendors": (200, {"parties": []}),
    "POST /t/acme/vendors": (201, {"party": {"id": "home-depot-4512", "name": "HOME DEPOT #4512"}}),
    "POST /t/acme/bills": (201, {"document": {"id": "CAP-20260314-home-depot-4512"}}),
}


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("0.00"))
    )


def _app() -> tuple[WebApp, FakeTransport, InMemoryAuditLog]:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    audit = InMemoryAuditLog()
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users, audit=audit)
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("0.00")), token="unused")
    transport = FakeTransport(dict(ROUTES))
    app.set_ledger(LedgerClient(transport, token="svc-token"))
    return app, transport, audit


def _req(app: WebApp, path: str, method: str = "GET", form: dict[str, str] | None = None) -> Any:
    tok = sign_jwt({"sub": "u-owner", "tenant": "acme", "exp": NOW + 3600}, SECRET)
    headers = {"authorization": f"Bearer {tok}"}
    body = ""
    if method == "POST":
        headers["content-type"] = "application/x-www-form-urlencoded"
        body = urlencode(form or {})
    return app.handle(Request(method, path, headers, body))


def test_capture_page_renders_the_paste_form() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/capture")
    assert r.status == 200
    assert "Capture a receipt" in r.body
    assert "Receipt text" in r.body


def test_scanning_a_receipt_drafts_a_bill_with_the_total() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/capture", method="POST", form={"text": RECEIPT})
    assert r.status == 200
    assert "Drafted bill" in r.body
    assert "HOME DEPOT #4512" in r.body       # vendor pulled out
    assert "$117.18" in r.body                # total pulled out
    assert "high confidence" in r.body        # vendor + date + total + tax


def test_a_poor_read_is_flagged_for_review() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/capture", method="POST", form={"text": "blurry smudge"})
    assert r.status == 200
    assert "Please check this before saving" in r.body


def test_confirming_creates_the_vendor_and_the_bill() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/capture/bill", method="POST", form={
        "vendor": "HOME DEPOT #4512", "date": "2026-03-14", "amount": "117.18",
        "expense_account_code": "6400",
    })
    assert r.status in (302, 303)
    # a vendor party was created from the name, and a bill posted for the amount
    party_call = [c for c in transport.calls if c[0] == "POST" and c[1].endswith("/vendors")][0]
    assert json.loads(party_call[2])["name"] == "HOME DEPOT #4512"
    bill_call = [c for c in transport.calls if c[0] == "POST" and c[1].endswith("/bills")][0]
    bill = json.loads(bill_call[2])
    assert bill["party_id"] == "home-depot-4512"
    assert bill["lines"][0]["unit_amount_minor"] == "11718"
    assert bill["lines"][0]["account_code"] == "6400"


def test_confirming_requires_a_positive_amount() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/capture/bill", method="POST",
             form={"vendor": "X", "date": "2026-03-14", "amount": "0"})
    assert r.status in (302, 303)
    location = r.headers.get("Location", "") or r.headers.get("location", "")
    assert "positive" in location and "capture" in location
