"""Receipt capture → drafted bill.

Paste receipt text, get the vendor/date/total pulled out and a bill drafted; a
poor read is flagged for review, and confirming creates the vendor (if new) and
the bill through the ordinary AP path.
"""

import base64
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
    assert "paste the receipt text" in r.body
    # A photo or PDF can be uploaded as well as text pasted.
    assert 'enctype="multipart/form-data"' in r.body
    assert 'type="file" name="file" accept="image/*,application/pdf"' in r.body


def test_scanning_a_receipt_drafts_a_bill_with_the_total() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/capture", method="POST", form={"text": RECEIPT})
    assert r.status == 200
    assert "Drafted from the receipt" in r.body
    assert "HOME DEPOT #4512" in r.body       # vendor pulled out
    assert "$117.18" in r.body                # total pulled out
    assert "high confidence" in r.body        # vendor + date + total + tax


def test_the_draft_offers_both_outcomes_in_plain_words() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/capture", method="POST", form={"text": RECEIPT})
    assert "Record as expense (already paid)" in r.body
    assert "Create bill (pay later)" in r.body
    # The expense path opens the expense form pre-filled (a GET: nothing posts).
    assert 'formmethod="get" formaction="/t/acme/expenses"' in r.body
    assert 'action="/t/acme/capture/bill"' in r.body


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


# --- a photo or PDF of the receipt -------------------------------------------

PHOTO = b"\xff\xd8\xff\xe0" + bytes(range(256))  # a JPEG header, then non-UTF-8 bytes


def _upload(app: WebApp, filename: str = "till.jpg", content_type: str = "image/jpeg",
            content: bytes = PHOTO, text: str = "") -> Any:
    from test_attachments import multipart_body

    tok = sign_jwt({"sub": "u-owner", "tenant": "acme", "exp": NOW + 3600}, SECRET)
    body = multipart_body("B", [("file", filename, content_type, content)], {"text": text})
    return app.handle(Request("POST", "/t/acme/capture", {
        "authorization": f"Bearer {tok}",
        "content-type": "multipart/form-data; boundary=B",
    }, body))


def _kept_app() -> tuple[WebApp, FakeTransport, InMemoryAuditLog]:
    app, transport, audit = _app()
    transport.routes["POST /t/acme/attachments"] = (201, {"attachment": {
        "id": "att-receipt-rcpt-1", "subject_kind": "receipt", "subject_id": "rcpt-1",
        "filename": "till.jpg"}})
    transport.routes["GET /t/acme/attachments"] = (200, {"attachments": []})
    return app, transport, audit


def test_an_uploaded_receipt_is_kept_even_when_it_cant_be_read() -> None:
    app, transport, _a = _kept_app()
    r = _upload(app)
    assert r.status == 200
    [kept] = [json.loads(c[2]) for c in transport.calls
              if c[0] == "POST" and c[1] == "/t/acme/attachments"]
    assert kept["subject_kind"] == "receipt"
    assert kept["filename"] == "till.jpg"
    assert base64.b64decode(kept["content_base64"]) == PHOTO, "byte for byte"
    # No reader wired: say so, and let the person type it.
    assert "We kept till.jpg" in r.body
    assert "Type the vendor, date and total" in r.body
    assert 'name="receipt" value="att-receipt-rcpt-1"' in r.body
    # Downloaded, never shown in the page's own origin.
    assert 'href="/t/acme/files/att-receipt-rcpt-1/download"' in r.body
    assert "<img" not in r.body.split("Drafted from the receipt", 1)[1]


def test_a_wired_reader_fills_the_draft_from_the_photo() -> None:
    from rgnr8_ocr import StubOcrClient

    app, _t, _a = _kept_app()
    app.set_ocr(StubOcrClient({PHOTO: RECEIPT}))
    r = _upload(app)
    assert "HOME DEPOT #4512" in r.body and "$117.18" in r.body
    assert "We kept" not in r.body


def test_only_a_photo_or_a_pdf_is_taken_as_a_receipt() -> None:
    app, transport, _a = _kept_app()
    r = _upload(app, filename="notes.html", content_type="text/html", content=b"<script>")
    assert "photo (JPEG, PNG, HEIC, …) or a PDF" in r.body
    assert not [c for c in transport.calls if c[0] == "POST"]
    # A phone photo the browser couldn't label still counts, by its extension.
    from rgnr8_web.capture_screens import is_receipt_file

    assert is_receipt_file("IMG_0042.HEIC", "application/octet-stream")
    assert is_receipt_file("scan.pdf", "application/pdf")
    assert not is_receipt_file("evil.svg.html", "application/octet-stream")


def test_a_kept_receipt_is_filed_onto_the_bill_made_from_it() -> None:
    app, transport, _a = _kept_app()
    transport.routes["POST /t/acme/attachments/att-receipt-rcpt-1/file"] = (
        200, {"attachment": {"id": "att-receipt-rcpt-1"}})
    r = _req(app, "/t/acme/capture/bill", method="POST", form={
        "vendor": "HOME DEPOT #4512", "date": "2026-03-14", "amount": "117.18",
        "receipt": "att-receipt-rcpt-1",
    })
    assert "/t/acme/bills?ok=" in r.headers["Location"]
    [filed] = [json.loads(c[2]) for c in transport.calls if c[1].endswith("/file")]
    assert filed == {"subject_kind": "bill", "subject_id": "CAP-20260314-home-depot-4512"}


def test_receipts_kept_and_not_recorded_are_listed_and_can_be_reopened() -> None:
    app, transport, _a = _kept_app()
    transport.routes["GET /t/acme/attachments"] = (200, {"attachments": [{
        "id": "att-receipt-rcpt-9", "subject_kind": "receipt", "subject_id": "rcpt-9",
        "filename": "lunch.pdf", "uploaded_at": "2026-10-01T10:00:00Z"}]})
    page = _req(app, "/t/acme/capture").body
    assert "Kept, not recorded yet" in page and "lunch.pdf" in page
    assert 'href="/t/acme/capture?receipt=att-receipt-rcpt-9"' in page
    reopened = _req(app, "/t/acme/capture?receipt=att-receipt-rcpt-9").body
    assert 'name="receipt" value="att-receipt-rcpt-9"' in reopened
    assert "Record as expense (already paid)" in reopened
