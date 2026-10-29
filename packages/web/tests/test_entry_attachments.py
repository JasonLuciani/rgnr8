"""A receipt on any posted entry.

Bills, invoices and bank lines already carried a paperclip. A journal entry,
the thing every other screen eventually becomes, did not: the general ledger,
the report you open when a number looks wrong, had no way to show what was
behind a line. These tests hold that it now does, and that the Ledger's own
entry form can take the file in the same step as posting.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from factory import app_with_two_tenants
from test_attachments import multipart_body

from rgnr8_web import LedgerClient, LedgerResponse, Request, WebApp
from rgnr8_web.reporting_screens import render_general_ledger

GL = {
    "total_debit_minor": "5000", "total_credit_minor": "5000",
    "accounts": [{
        "code": "1000", "name": "Business Checking", "opening_minor": "0",
        "total_debit_minor": "5000", "total_credit_minor": "0", "closing_minor": "5000",
        "rows": [
            {"date": "2026-10-05", "memo": "Fee", "entry_id": "acme:1",
             "debit_minor": "5000", "credit_minor": "0", "balance_minor": "5000"},
            {"date": "2026-10-06", "memo": "Other", "entry_id": "acme:2",
             "debit_minor": "0", "credit_minor": "0", "balance_minor": "5000"},
        ],
    }],
}


class _Ledger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def request(self, method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        route = path.split("?")[0]
        self.calls.append((method, route, body))
        if route == "/t/acme/settings":
            return LedgerResponse(200, {"settings": {"book_of_record": "rgnr8"}})
        if route == "/t/acme/entries" and method == "POST":
            return LedgerResponse(201, {"entry": {"id": "acme:7"}})
        if route == "/t/acme/attachments" and method == "POST":
            return LedgerResponse(201, {"attachment": {"id": "att-1"}})
        if route == "/t/acme/attachments/counts":
            return LedgerResponse(200, {"counts": {"acme:1": 1}})
        if route == "/t/acme/gl":
            return LedgerResponse(200, GL)
        return LedgerResponse(404, {"error": "not found"})


def _app() -> tuple[WebApp, _Ledger]:
    app = app_with_two_tenants()
    ledger = _Ledger()
    app.set_ledger(LedgerClient(ledger))
    return app, ledger


def _call(app: WebApp, method: str, path: str, body: str = "", ct: str = "") -> Any:
    headers = {"authorization": "Bearer tok-acme"}
    if ct:
        headers["content-type"] = ct
    return app.handle(Request(method, path, headers, body))


ENTRY = {"date": "2026-10-05", "memo": "Fee", "code1": "6400", "debit1": "50.00",
         "code2": "1000", "credit2": "50.00"}
PDF = b"%PDF-1.4\n" + bytes(range(256))


def test_a_file_sent_with_a_journal_entry_lands_on_that_entry() -> None:
    app, ledger = _app()
    body = multipart_body("B", [("file", "invoice.pdf", "application/pdf", PDF)], ENTRY)
    r = _call(app, "POST", "/t/acme/books/entries", body, "multipart/form-data; boundary=B")
    assert r.status == 302
    assert "posted=" in r.headers["Location"] and "err=" not in r.headers["Location"]
    posts = [(rt, b) for m, rt, b in ledger.calls if m == "POST"]
    assert [rt for rt, _ in posts] == ["/t/acme/entries", "/t/acme/attachments"]
    entry = json.loads(posts[0][1])
    assert [ln["side"] for ln in entry["lines"]] == ["DEBIT", "CREDIT"]
    saved = json.loads(posts[1][1])
    assert (saved["subject_kind"], saved["subject_id"]) == ("entry", "acme:7")
    assert base64.b64decode(saved["content_base64"]) == PDF


def test_the_plain_form_still_posts_without_a_file() -> None:
    app, ledger = _app()
    r = _call(app, "POST", "/t/acme/books/entries", urlencode(ENTRY),
              "application/x-www-form-urlencoded")
    assert r.status == 302
    assert [rt for m, rt, _ in ledger.calls if m == "POST"] == ["/t/acme/entries"]


def test_a_refused_file_leaves_the_entry_posted_and_says_where_to_attach_it() -> None:
    app, ledger = _app()
    real = ledger.request

    def refuse(method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        if method == "POST" and path == "/t/acme/attachments":
            return LedgerResponse(400, {"error": "upload rejected by content scan"})
        return real(method, path, body, headers)

    ledger.request = refuse  # type: ignore[method-assign]
    body = multipart_body("B", [("file", "x.pdf", "application/pdf", PDF)], ENTRY)
    r = _call(app, "POST", "/t/acme/books/entries", body, "multipart/form-data; boundary=B")
    q = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["Location"]).query).items()}
    assert q["posted"] == "Entry posted"
    assert "content scan" in q["err"] and "paperclip" in q["err"]


def test_the_ledger_form_takes_a_file() -> None:
    from rgnr8_web.books_screens import render_books_home

    html = render_books_home("acme", {"rows": []}, {"accounts": [
        {"code": "1000", "name": "Checking", "type": "ASSET", "subtype": "BANK"}]},
        can_post=True)
    form = html.split('action="/t/acme/books/entries"', 1)[1].split("</form>", 1)[0]
    assert 'type="file" name="file"' in form
    assert 'enctype="multipart/form-data"' in html.split('action="/t/acme/books/entries"', 1)[0][-200:]


def test_every_general_ledger_row_has_a_paperclip() -> None:
    app, _ledger = _app()
    page = _call(app, "GET", "/t/acme/books/gl").body
    assert 'href="/t/acme/files/entry/acme:1"' in page
    assert 'href="/t/acme/files/entry/acme:2"' in page
    # Filled where there is evidence, hollow where there isn't.
    assert "📎 1" in page
    assert "No receipt attached yet" in page


def test_a_row_without_an_entry_gets_no_paperclip() -> None:
    data = {"accounts": [{**GL["accounts"][0], "rows": [{"date": "2026-10-05", "memo": "x",
                                                         "debit_minor": "1", "credit_minor": "0",
                                                         "balance_minor": "1"}]}]}
    assert "/files/entry/" not in render_general_ledger("acme", data)
