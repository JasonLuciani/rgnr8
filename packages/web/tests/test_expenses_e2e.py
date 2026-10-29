"""END-TO-END: an expense and its receipt, through the REAL ledger service.

The unit tests prove what the web app sends; this proves the ledger takes it.
A photo is kept on Receipts, recorded as an expense paid by card, and the books
show the card owed and the supplies spent, with the same photo now attached to
that entry. Skipped (not silently passed) if the service hasn't been built.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from test_attachments import multipart_body
from test_books_e2e import (  # noqa: F401 - ledger_service is a fixture
    NOW,
    SECRET,
    _app,
    _req,
    _seed,
    _service_get,
    _signed,
    ledger_service,
)

from rgnr8_web import Request, sign_jwt

PHOTO = b"\xff\xd8\xff\xe0" + bytes(range(256))


def _multipart(app: Any, tenant: str, path: str, body: str) -> Any:
    tok = sign_jwt({"sub": f"u-{tenant}", "tenant": tenant, "exp": NOW + 3600}, SECRET)
    return app.handle(Request("POST", path, {
        "authorization": f"Bearer {tok}", "content-type": "multipart/form-data; boundary=B",
    }, body))


def test_a_kept_receipt_becomes_a_card_expense_with_the_photo_on_its_entry(
    ledger_service: str,
) -> None:
    base = ledger_service
    app = _app(base, ["harbor"])
    _seed(base, "harbor", "SERVICE_GENERAL")

    # Keep the photo. No reader is wired, so the draft asks for the figures.
    draft = _multipart(app, "harbor", "/t/harbor/capture", multipart_body(
        "B", [("file", "till.jpg", "image/jpeg", PHOTO)], {"text": ""}))
    assert draft.status == 200
    assert "We kept till.jpg" in draft.body
    kept = _service_get(base, "/t/harbor/attachments?subject_kind=receipt")["attachments"]
    assert [a["filename"] for a in kept] == ["till.jpg"]
    receipt = kept[0]["id"]

    # "Record as expense" opens the expense form with the receipt riding along.
    form = _req(app, "harbor", "/t/harbor/expenses?" + urlencode({
        "vendor": "Harbor Hardware", "date": "2026-08-12", "amount": "117.18",
        "expense_account_code": "6400", "receipt": receipt, "receipt_name": "till.jpg"}))
    assert form.status == 200 and f'name="receipt" value="{receipt}"' in form.body
    token = form.body.split('name="token" value="', 1)[1].split('"', 1)[0]

    posted = _req(app, "harbor", "/t/harbor/expenses", "POST", urlencode({
        "token": token, "date": "2026-08-12", "vendor": "Harbor Hardware", "paid_from": "2100",
        "code1": "6400", "desc1": "Shelving", "amount1": "117.18", "total": "117.18",
        "receipt": receipt}))
    q = {k: v[0] for k, v in parse_qs(urlsplit(posted.headers["Location"]).query).items()}
    assert "Expense recorded" in q.get("done", ""), q
    assert "err" not in q, q

    # The books: supplies up, the card owed, in balance.
    signed = _signed(base, "harbor")
    assert signed["6400"] == 11718
    assert signed["2100"] == -11718

    # Posting the same form again is the same expense (the ledger replays the key).
    _req(app, "harbor", "/t/harbor/expenses", "POST", urlencode({
        "token": token, "date": "2026-08-12", "vendor": "Harbor Hardware", "paid_from": "2100",
        "code1": "6400", "desc1": "Shelving", "amount1": "117.18"}))
    assert _signed(base, "harbor")["6400"] == 11718

    # The photo moved onto the entry, byte for byte, and no longer waits.
    [entry] = [e for e in _service_get(base, "/t/harbor/entries")["entries"]
               if e["source"] == "expense"]
    assert entry["idempotency_key"].startswith("exp:harbor-hardware:")
    on_entry = _service_get(
        base, f"/t/harbor/attachments?subject_kind=entry&subject_id={entry['id']}")
    assert [a["id"] for a in on_entry["attachments"]] == [receipt]
    content = _service_get(base, f"/t/harbor/attachments/{receipt}/content")
    assert base64.b64decode(content["content_base64"]) == PHOTO
    assert _service_get(base, "/t/harbor/attachments?subject_kind=receipt")["attachments"] == []

    # The list and the general ledger both show it, with its paperclip filled.
    listed = _req(app, "harbor", "/t/harbor/expenses").body
    assert "Harbor Hardware" in listed and "$117.18" in listed and "📎 1" in listed
    gl = _req(app, "harbor", "/t/harbor/books/gl").body
    assert f'href="/t/harbor/files/entry/{entry["id"]}"' in gl


def test_a_journal_entry_posted_with_a_file_carries_it(ledger_service: str) -> None:
    base = ledger_service
    app = _app(base, ["quay"])
    _seed(base, "quay", "SERVICE_GENERAL")
    r = _multipart(app, "quay", "/t/quay/books/entries", multipart_body(
        "B", [("file", "lease.pdf", "application/pdf", b"%PDF-1.4 lease")],
        {"date": "2026-08-10", "memo": "Rent", "code1": "6300", "debit1": "2000.00",
         "code2": "1000", "credit2": "2000.00"}))
    assert "posted=" in r.headers["Location"] and "err=" not in r.headers["Location"]
    [entry] = _service_get(base, "/t/quay/entries")["entries"]
    files = _service_get(base, f"/t/quay/attachments?subject_kind=entry&subject_id={entry['id']}")
    assert [a["filename"] for a in files["attachments"]] == ["lease.pdf"]
