"""Parallel run: while QuickBooks is the book of record, nothing is entered here.

During the parallel run every create form could post to the ledger, and one
posting stopped every later Refresh (the mirror refuses a journal holding
anything it didn't copy). The ledger now refuses those postings itself; these
tests hold the web app's side: no form offers to post, a posting request is
answered here with the reason, and nobody but the mirror may claim to be
QuickBooks.
"""

from __future__ import annotations

import json
import re
from typing import Any

from factory import app_with_two_tenants

from rgnr8_web import LedgerClient, LedgerResponse, Request, WebApp
from rgnr8_web.parallel_run import (
    PARALLEL_RUN_MESSAGE,
    is_posting_path,
    notice_html,
    posting_forms_to_notice,
)


# Just enough of a ledger for the books screen to render its entry form.
_READS: dict[str, tuple[int, dict[str, Any]]] = {
    "/t/acme/accounts": (200, {"tenant": "acme", "accounts": [
        {"code": "1000", "name": "Business Checking", "type": "ASSET", "subtype": "BANK"},
        {"code": "4000", "name": "Services Income", "type": "REVENUE", "subtype": "INCOME"},
    ]}),
    "/t/acme/trial-balance": (200, {
        "tenant": "acme", "currency": "USD", "in_balance": True,
        "total_debit_minor": "0", "total_credit_minor": "0", "rows": [],
    }),
}


class _Ledger:
    """A ledger whose book of record can be flipped; records every call."""

    def __init__(self, book: str = "quickbooks") -> None:
        self.book = book
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        self.calls.append((method, path.split("?")[0]))
        if path == "/t/acme/settings":
            return LedgerResponse(200, {"tenant": "acme", "settings": {
                "book_of_record": self.book,
                "mirror_realm_id": "9341453432535209" if self.book == "quickbooks" else "",
            }})
        if method == "POST":
            return LedgerResponse(201, {"tenant": "acme", "entry": {"id": "acme:1"}})
        return LedgerResponse(*_READS.get(path.split("?")[0], (404, {"error": "not found"})))

    def posts(self) -> list[str]:
        return [p for m, p in self.calls if m == "POST"]


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _app(book: str = "quickbooks") -> tuple[WebApp, _Ledger, _Clock]:
    app = app_with_two_tenants()
    clock = _Clock()
    app._monotonic = clock  # the cache's clock; the constructor arg in production
    ledger = _Ledger(book)
    app.set_ledger(LedgerClient(ledger))
    return app, ledger, clock


def _call(app: WebApp, method: str, path: str, body: str = "") -> Any:
    headers = {"authorization": "Bearer tok-acme"}
    if method == "POST":
        headers["content-type"] = ("application/json" if path.startswith("/api/")
                                   else "application/x-www-form-urlencoded")
    return app.handle(Request(method, path, headers, body))


ENTRY_FORM = "date=2026-10-05&memo=Fee&code1=1000&debit1=50.00&code2=4000&credit2=50.00"


def _parts(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


# --- which routes post ----------------------------------------------------------

POSTING = [
    "/t/acme/invoices", "/t/acme/bills", "/t/acme/expenses",
    "/t/acme/invoices/INV-1/payments", "/t/acme/bills/B-1/credits", "/t/acme/invoices/INV-1/refunds",
    "/t/acme/books/entries", "/t/acme/books/wip", "/t/acme/books/recurring/run",
    "/t/acme/payroll/remit", "/t/acme/payroll/R-1/post", "/t/acme/payroll/R-1/void",
    "/t/acme/debt/loans", "/t/acme/debt/payments", "/t/acme/debt/draws",
    "/t/acme/assets", "/t/acme/assets/A-1/depreciate", "/t/acme/assets/A-1/usage",
    "/t/acme/assets/A-1/dispose",
    "/t/acme/capture/bill",
    "/t/acme/inventory/receipts", "/t/acme/inventory/issues", "/t/acme/inventory/counts",
    "/t/acme/work-orders/WO-1/entries",
    "/t/acme/sales-orders/SO-1/invoice", "/t/acme/estimates/E-1/invoice",
    "/t/acme/purchase-orders/PO-1/receipts", "/t/acme/purchase-orders/PO-1/bill",
    "/t/acme/jobs/J-1/bill/progress", "/t/acme/jobs/J-1/deposits",
    "/t/acme/jobs/J-1/deposits/apply", "/t/acme/jobs/J-1/retainage/release",
    "/t/acme/inbox/bulk-accept", "/t/acme/inbox/T-1/accept", "/t/acme/inbox/T-1/match",
    "/t/acme/inbox/T-1/exclude", "/t/acme/inbox/T-1/undo",
    "/api/acme/entries", "/api/acme/ingest",
]

NOT_POSTING = [
    # Refresh and the go-live live on Connections; settings are never a posting.
    "/t/acme/connect/qbo/sync", "/t/acme/connect/qbo/migrate", "/t/acme/settings",
    # Records that never touch the journal.
    "/t/acme/customers", "/t/acme/inventory/items", "/t/acme/payroll", "/t/acme/payroll/employees",
    "/t/acme/jobs", "/t/acme/jobs/J-1/budget", "/t/acme/estimates", "/t/acme/estimates/E-1/accept",
    "/t/acme/sales-orders", "/t/acme/purchase-orders", "/t/acme/work-orders",
    "/t/acme/work-orders/WO-1/complete", "/t/acme/inbox/rules", "/t/acme/integrations",
    "/api/acme/transactions",
    # Reading a receipt keeps the file and drafts; attaching evidence posts nothing.
    "/t/acme/capture", "/t/acme/files/entry/acme:1", "/t/acme/files/receipt/rcpt-1",
]


def test_every_posting_route_is_named_and_nothing_else() -> None:
    for path in POSTING:
        assert is_posting_path(_parts(path), "POST"), path
        assert not is_posting_path(_parts(path), "GET"), path
    for path in NOT_POSTING:
        assert not is_posting_path(_parts(path), "POST"), path


# --- requests -------------------------------------------------------------------


def test_a_posting_form_is_answered_here_with_the_reason_and_never_reaches_the_ledger() -> None:
    app, ledger, _ = _app("quickbooks")
    r = _call(app, "POST", "/t/acme/invoices", "id=INV-1&party_id=cust-1&date=2026-10-01")
    assert r.status == 409
    assert "Add this in QuickBooks" in r.body
    assert 'href="/t/acme/connect"' in r.body  # Refresh is one click away
    assert ledger.posts() == []


def test_an_api_posting_gets_the_ledgers_409_shape() -> None:
    app, ledger, _ = _app("quickbooks")
    for path in ("/api/acme/entries", "/api/acme/ingest"):
        r = _call(app, "POST", path, json.dumps({"date": "2026-10-05", "lines": [], "transactions": []}))
        assert r.status == 409, path
        assert json.loads(r.body) == {"error": PARALLEL_RUN_MESSAGE, "code": "book_of_record"}
    assert ledger.posts() == []


def test_with_rgnr8_as_the_book_of_record_postings_go_through() -> None:
    app, ledger, _ = _app("rgnr8")
    r = _call(app, "POST", "/t/acme/books/entries", ENTRY_FORM)
    assert r.status == 302
    assert ledger.posts() == ["/t/acme/entries"]


def test_refresh_is_never_refused() -> None:
    # Connect isn't configured in this app (501), but the point is it's not a 409.
    app, _, _ = _app("quickbooks")
    assert _call(app, "POST", "/t/acme/connect/qbo/sync").status != 409


def test_switching_back_to_rgnr8_opens_posting_up_within_a_minute() -> None:
    app, ledger, clock = _app("quickbooks")
    assert _call(app, "POST", "/t/acme/books/entries", ENTRY_FORM).status == 409
    ledger.book = "rgnr8"  # cutover
    assert _call(app, "POST", "/t/acme/books/entries", ENTRY_FORM).status == 409  # cached
    clock.t = 61.0
    assert _call(app, "POST", "/t/acme/books/entries", ENTRY_FORM).status != 409


def test_nobody_may_ingest_as_quickbooks() -> None:
    app, ledger, _ = _app("rgnr8")
    r = _call(app, "POST", "/api/acme/ingest", json.dumps({"source": "QuickBooks", "transactions": []}))
    assert r.status == 400
    assert "reserved for the QuickBooks mirror" in json.loads(r.body)["error"]
    assert ledger.posts() == []


# --- screens --------------------------------------------------------------------

_POSTING_FORM = re.compile(r'<form\b[^>]*\baction="([^"]*)"')


def _posting_forms(html: str) -> list[str]:
    return [a for a in _POSTING_FORM.findall(html) if is_posting_path(_parts(a.split("?")[0]), "POST")]


def test_forms_that_would_post_become_the_notice_and_the_rest_stay() -> None:
    html = (
        '<h1>Inventory</h1>'
        '<form method="post" action="/t/acme/inventory/items" class="grid"><input name="sku"></form>'
        '<form method="post" action="/t/acme/inventory/receipts" class="grid">'
        '<input name="qty"><button>Receive</button></form>'
        '<form method="post" action="/t/acme/jobs/J-1/bill/progress" class="grid"><button>Bill</button></form>'
    )
    out = posting_forms_to_notice(html, "acme")
    assert _posting_forms(out) == []
    assert 'action="/t/acme/inventory/items"' in out
    assert out.count(notice_html("acme")) == 2
    assert "Receive" not in out and "<h1>Inventory</h1>" in out


def test_no_screen_offers_a_posting_form_during_a_parallel_run() -> None:
    screens = ["/t/acme", "/t/acme/invoices", "/t/acme/bills", "/t/acme/expenses",
               "/t/acme/books", "/t/acme/payroll",
               "/t/acme/debt", "/t/acme/assets", "/t/acme/capture", "/t/acme/inventory",
               "/t/acme/inbox", "/t/acme/books/wip"]
    rgnr8, _, _ = _app("rgnr8")
    offered = {s for s in screens if _posting_forms(_call(rgnr8, "GET", s).body)}
    # The check below means something only if these screens do offer forms normally.
    assert offered, "no screen rendered a posting form against the fake ledger"

    qbo, _, _ = _app("quickbooks")
    for screen in screens:
        page = _call(qbo, "GET", screen).body
        assert _posting_forms(page) == [], screen
    for screen in offered:
        assert "data-parallel-run" in _call(qbo, "GET", screen).body, screen


# --- a locked form says which form it is --------------------------------------


def test_a_form_that_names_itself_is_replaced_by_a_notice_naming_it() -> None:
    from rgnr8_web.parallel_run import form_label

    html = (f'<form method="post" action="/t/acme/books/entries"'
            f'{form_label("Journal entry", "+ New → Journal entry")}><button>Post</button></form>'
            '<form method="post" action="/t/acme/books/wip"><button>Post</button></form>')
    out = posting_forms_to_notice(html, "acme")
    assert _posting_forms(out) == []
    assert ("<strong>Journal entry</strong>: this form opens here after the cutover, when "
            "RGNR8 becomes the book of record. Until then, add it in QuickBooks "
            "(+ New → Journal entry), then") in out
    assert 'href="/t/acme/connect">press Refresh</a>' in out
    # A form that doesn't name itself still gets today's notice.
    assert notice_html("acme") in out


def test_a_name_is_escaped_on_the_way_in_and_out() -> None:
    from rgnr8_web.parallel_run import form_label

    html = (f'<form method="post" action="/t/acme/bills"{form_label("<b>&</b>")}>'
            "</form>")
    out = posting_forms_to_notice(html, "acme")
    assert "<strong>&lt;b&gt;&amp;&lt;/b&gt;</strong>" in out and "<b>" not in out


def test_the_main_forms_name_themselves_during_a_parallel_run() -> None:
    from rgnr8_web.arap_screens import render_documents

    qbo, _, _ = _app("quickbooks")
    books = _call(qbo, "GET", "/t/acme/books").body
    assert "<strong>Journal entry</strong>" in books and "+ New → Journal entry" in books

    doc = {"id": "D-1", "party_id": "p1", "date": "2026-10-01", "due_date": "2026-10-31",
           "total_minor": "5000", "open_minor": "5000", "lines": []}
    for kind, names in (("invoices", ("Invoice", "Receive payment")),
                        ("bills", ("Bill", "Pay bill"))):
        html = render_documents("acme", kind, {"documents": [doc]},
                                {"parties": [{"id": "p1", "name": "Pat"}]},
                                today="2026-10-05", can_post=True)
        out = posting_forms_to_notice(html, "acme")
        assert _posting_forms(out) == [], kind
        for name in names:
            assert f"<strong>{name}</strong>" in out, (kind, name)


def test_a_receipt_is_still_kept_during_a_parallel_run_and_its_draft_says_what_it_is() -> None:
    from test_attachments import multipart_body

    app, ledger, _ = _app("quickbooks")
    body = multipart_body("B", [("file", "till.jpg", "image/jpeg", b"\xff\xd8\xff")], {})
    r = app.handle(Request("POST", "/t/acme/capture", {
        "authorization": "Bearer tok-acme",
        "content-type": "multipart/form-data; boundary=B"}, body))
    assert r.status == 200
    assert ledger.posts() == ["/t/acme/attachments"], "the file is kept; nothing posts"
    assert _posting_forms(r.body) == []
    assert "<strong>Receipt: expense or bill</strong>" in r.body
