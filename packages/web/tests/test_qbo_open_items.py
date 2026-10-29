"""QuickBooks' open invoices and bills during a parallel run (issues 11 and 12).

The mirrored books carry A/R and A/P balances with no documents behind them, so
Invoices and Collections showed $0 owed while A/R was $373.20 and QuickBooks
had $389.95 open, and the forecast said "no receivables provided". Each Refresh
now keeps a non-posting copy of QuickBooks' open items; these tests hold how it
is shown, how it ties out, and that the forecast counts it.
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import Any

from factory import app_with_two_tenants

from rgnr8_web import LedgerClient, LedgerResponse, Request, WebApp
from rgnr8_web.open_items import snapshot_from_quickbooks

OWNER = {"authorization": "Bearer tok-acme"}

SNAPSHOT = {
    "tenant": "acme", "as_of": "2026-10-29",
    "invoices": [
        {"qbo_id": "130", "number": "1037", "party": "Northwind", "date": "2026-10-01",
         "due_date": "2026-10-31", "open_minor": "28995"},
        {"qbo_id": "131", "number": "1038", "party": "Fabrikam", "date": "2026-10-15",
         "due_date": "2026-11-14", "open_minor": "10000"},
    ],
    "bills": [{"qbo_id": "77", "number": "", "party": "Copyshop", "date": "2026-10-10",
               "due_date": "2026-11-09", "open_minor": "4500"}],
    "invoice_total_minor": "38995", "bill_total_minor": "4500",
}


class _MirrorLedger:
    """A QuickBooks mirror as the ledger serves it: balances, no documents."""

    def __init__(self, *, documents: "list[dict[str, Any]] | None" = None,
                 snapshot: "dict[str, Any] | None" = None, ar_minor: int = 37320) -> None:
        self.documents = documents or []
        self.snapshot = snapshot if snapshot is not None else SNAPSHOT
        self.ar_minor = ar_minor
        self.saved: list[dict[str, Any]] = []

    def request(self, method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        route = path.split("?")[0]
        if route == "/t/acme/mirror/open-items":
            if method == "POST":
                self.saved.append(json.loads(body))
                return LedgerResponse(200, {})
            return LedgerResponse(200, self.snapshot)
        if route in ("/t/acme/invoices", "/t/acme/bills"):
            return LedgerResponse(200, {"documents": self.documents if route.endswith("invoices") else []})
        if route == "/t/acme/accounts":
            return LedgerResponse(200, {"accounts": [
                {"code": "10", "name": "Checking", "type": "ASSET", "subtype": "BANK"},
                {"code": "84", "name": "Accounts Receivable (A/R)", "type": "ASSET",
                 "subtype": "ACCOUNTS_RECEIVABLE"},
                {"code": "33", "name": "Accounts Payable (A/P)", "type": "LIABILITY",
                 "subtype": "ACCOUNTS_PAYABLE"},
            ]})
        if route == "/t/acme/trial-balance":
            return LedgerResponse(200, {"in_balance": True, "rows": [
                {"code": "10", "debit_minor": "500000", "credit_minor": "0"},
                {"code": "84", "debit_minor": str(self.ar_minor), "credit_minor": "0"},
                {"code": "33", "debit_minor": "0", "credit_minor": "4500"},
            ]})
        return LedgerResponse(404, {"error": "not found"})


def _app(ledger: _MirrorLedger) -> WebApp:
    app = app_with_two_tenants()
    app.set_ledger(LedgerClient(ledger))
    return app


def _get(app: WebApp, path: str) -> str:
    return app.handle(Request("GET", path, OWNER, "")).body


def test_invoices_show_quickbooks_open_items_and_how_they_tie_to_ar() -> None:
    page = _get(_app(_MirrorLedger()), "/t/acme/invoices")
    assert "Open in QuickBooks as of 2026-10-29" in page
    assert "1037" in page and "Northwind" in page and "$289.95" in page
    assert "Total open" in page and "$389.95" in page
    # $389.95 open in QuickBooks against $373.20 of A/R in the books.
    assert "Accounts Receivable in your books is $373.20, $16.75 more on QuickBooks' side" in page
    assert "Owed to you" not in page  # not the books' own (empty) document list


def test_when_they_match_it_says_so() -> None:
    page = _get(_app(_MirrorLedger(ar_minor=38995)), "/t/acme/invoices")
    assert "Ties out: QuickBooks' open invoices ($389.95) match Accounts Receivable" in page


def test_bills_tie_to_ap_as_an_amount_owed() -> None:
    page = _get(_app(_MirrorLedger()), "/t/acme/bills")
    assert "Copyshop" in page
    assert "Ties out: QuickBooks' open bills ($45.00) match Accounts Payable" in page


def test_the_books_own_documents_always_win() -> None:
    doc = {"id": "INV-9", "party_id": "c1", "date": "2026-11-02", "due_date": "2026-12-02",
           "total_minor": "5000", "open_minor": "5000", "status": "OPEN"}
    page = _get(_app(_MirrorLedger(documents=[doc])), "/t/acme/invoices")
    assert "Open in QuickBooks" not in page and "INV-9" in page


def test_collections_and_the_forecast_count_quickbooks_receivables() -> None:
    app = _app(_MirrorLedger())
    page = _get(app, "/t/acme/receivables")
    assert "of $389.95 total AR" in page and "$16.75 more on QuickBooks' side" in page
    assert page.index("Receivables &amp; collections") < page.index("$16.75 more")  # under the title
    fc = app._forecast(app._tenants["acme"])
    assert not any(n.startswith("no receivables provided") for n in fc.data_quality)
    invoices = app._effective_inputs(app._tenants["acme"]).invoices
    assert [i.provenance.source_system for i in invoices] == ["quickbooks", "quickbooks"]


def test_no_snapshot_means_the_books_alone() -> None:
    ledger = _MirrorLedger(snapshot={"tenant": "acme", "as_of": ""})
    page = _get(_app(ledger), "/t/acme/invoices")
    assert "Open in QuickBooks" not in page


def test_a_snapshot_is_what_quickbooks_has_open_in_minor_units() -> None:
    client = SimpleNamespace(
        open_invoices=lambda: [
            SimpleNamespace(id="130", doc_number="1037", customer="Northwind", balance="289.95",
                            txn_date="2026-10-01", due_date="2026-10-31"),
            SimpleNamespace(id="132", doc_number="1039", customer="Paid Co", balance="0",
                            txn_date="2026-10-01", due_date=""),
        ],
        open_bills=lambda: [
            SimpleNamespace(id="77", vendor="Copyshop", balance="1,045.5", txn_date="2026-10-10",
                            due_date=""),
        ],
    )
    snap = snapshot_from_quickbooks(client, date(2026, 10, 29))
    assert snap["as_of"] == "2026-10-29"
    assert snap["invoices"] == [{"qbo_id": "130", "number": "1037", "party": "Northwind",
                                 "date": "2026-10-01", "due_date": "2026-10-31", "open_minor": "28995"}]
    # no due date falls back to the bill date; "1,045.5" is 104550 cents
    assert snap["bills"][0]["open_minor"] == "104550" and snap["bills"][0]["due_date"] == "2026-10-10"


def test_storing_the_snapshot_never_fails_a_refresh() -> None:
    ledger = _MirrorLedger()
    app = _app(ledger)
    t = app._tenants["acme"]

    def broken() -> list[Any]:
        raise RuntimeError("QuickBooks went away")

    client = SimpleNamespace(open_invoices=broken, open_bills=lambda: [])
    assert "weren't updated" in app._store_open_items(t, client, date(2026, 10, 29))
    good = SimpleNamespace(open_invoices=lambda: [], open_bills=lambda: [])
    assert app._store_open_items(t, good, date(2026, 10, 29)) == ""
    assert ledger.saved == [{"as_of": "2026-10-29", "invoices": [], "bills": []}]
