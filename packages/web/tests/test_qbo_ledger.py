"""Pushing a connected QuickBooks company into the RGNR8 ledger.

The distinction these guard is the one that matters: open invoices and bills are
*decisions the business already made*, so they become real AR/AP documents; bank
movements are *claims that money moved*, so they queue for review rather than
being posted to a guessed account.
"""

import json
from typing import Any

from rgnr8_qbo import QboApiClient
from rgnr8_web import LedgerClient, LedgerResponse
from rgnr8_web.qbo_ledger import sync_qbo_to_ledger


class FakeHttp:
    """Answers QuickBooks queries from a canned map of entity → rows."""

    def __init__(self, entities: dict[str, list[dict[str, Any]]]) -> None:
        self.entities = entities
        self.queries: list[str] = []

    def get(self, url: str, headers: Any) -> Any:
        import urllib.parse
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("query", [""])[0]
        self.queries.append(query)
        entity = ""
        for name in ("Invoice", "Bill", "Purchase", "Deposit", "Account", "CompanyInfo"):
            if f"from {name}" in query:
                entity = name
                break

        class R:
            status = 200
            body = json.dumps({"QueryResponse": {entity: self.entities.get(entity, [])}})

        return R()


class FakeLedgerTransport:
    """A ledger that records what it was asked to create."""

    def __init__(self, existing: dict[str, list[dict[str, Any]]] | None = None,
                 fail: set[str] | None = None) -> None:
        self.existing = existing or {}
        self.fail = fail or set()
        self.calls: list[tuple[str, str, str]] = []

    def request(self, method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        self.calls.append((method, path, body))
        for marker in self.fail:
            if marker in path:
                return LedgerResponse(400, {"error": f"refused: {marker}"})
        if method == "GET" and path.endswith("/invoices"):
            return LedgerResponse(200, {"documents": self.existing.get("invoices", [])})
        if method == "GET" and path.endswith("/bills"):
            return LedgerResponse(200, {"documents": self.existing.get("bills", [])})
        if "/feed/" in path:
            payload = json.loads(body)
            n = len(payload["transactions"])
            return LedgerResponse(201, {
                "received": n, "added": n, "duplicates": 0,
                "auto_posted": 0, "pending": n,
            })
        return LedgerResponse(201, {"ok": True})

    def sent(self, needle: str) -> list[dict[str, Any]]:
        return [
            json.loads(c[2]) for c in self.calls
            if c[0] == "POST" and needle in c[1] and c[2]
        ]


INVOICES = [
    {"Id": "101", "DocNumber": "1042", "Balance": "1200.00", "TxnDate": "2026-08-01",
     "DueDate": "2026-08-31", "CustomerRef": {"name": "Halcyon LLC"}},
]
BILLS = [
    {"Id": "202", "Balance": "349.99", "TxnDate": "2026-08-04", "DueDate": "2026-08-19",
     "VendorRef": {"name": "Riverside Properties"}},
]
PURCHASES = [
    {"Id": "301", "TotalAmt": "89.00", "TxnDate": "2026-08-11",
     "PrivateNote": "NETFLIX.COM", "EntityRef": {"name": "Netflix"},
     "AccountRef": {"name": "Checking"}},
]
DEPOSITS = [
    {"Id": "401", "TotalAmt": "1200.00", "TxnDate": "2026-08-06",
     "PrivateNote": "ACH CREDIT HALCYON", "EntityRef": {"name": "Halcyon LLC"},
     "DepositToAccountRef": {"name": "Checking"}},
]


def _client(**entities: list[dict[str, Any]]) -> tuple[QboApiClient, FakeHttp]:
    http = FakeHttp({
        "Invoice": entities.get("invoices", INVOICES),
        "Bill": entities.get("bills", BILLS),
        "Purchase": entities.get("purchases", PURCHASES),
        "Deposit": entities.get("deposits", DEPOSITS),
    })
    return QboApiClient(http=http, api_base="https://x", realm_id="r", access_token="t"), http


# --- the client's new read ---------------------------------------------------

def test_purchases_come_back_negative_and_deposits_positive() -> None:
    client, _http = _client()
    txns = client.bank_transactions()
    by_id = {t.id: t for t in txns}
    assert by_id["qbo-purchase-301"].amount == "-89.00", "money out is negative"
    assert by_id["qbo-deposit-401"].amount == "1200.00", "money in is positive"
    assert by_id["qbo-purchase-301"].counterparty == "Netflix"
    # and they arrive in date order, oldest first
    assert [t.txn_date for t in txns] == ["2026-08-06", "2026-08-11"]


def test_a_since_date_narrows_the_query() -> None:
    client, http = _client()
    client.bank_transactions(since="2026-08-01")
    assert all("TxnDate >= '2026-08-01'" in q for q in http.queries)


# --- open items become real documents ----------------------------------------

def test_open_invoices_and_bills_become_ar_and_ap_documents() -> None:
    client, _http = _client()
    transport = FakeLedgerTransport()
    summary = sync_qbo_to_ledger(client, LedgerClient(transport), "acme")

    assert summary.invoices == 1 and summary.bills == 1
    invoice = transport.sent("/invoices")[0]
    assert invoice["id"] == "QBO-INV-101"
    assert invoice["party_id"] == "halcyon-llc"
    assert invoice["due_date"] == "2026-08-31"
    # "1200.00" reached the ledger as exact minor units, never a float
    assert invoice["lines"][0]["unit_amount_minor"] == "120000"

    bill = transport.sent("/bills")[0]
    assert bill["id"] == "QBO-BILL-202"
    assert bill["lines"][0]["unit_amount_minor"] == "34999"

    # the customer and vendor were created too
    assert transport.sent("/customers")[0]["name"] == "Halcyon LLC"
    assert transport.sent("/vendors")[0]["name"] == "Riverside Properties"


def test_a_resync_does_not_duplicate_documents() -> None:
    client, _http = _client()
    transport = FakeLedgerTransport(existing={
        "invoices": [{"id": "QBO-INV-101"}],
        "bills": [{"id": "QBO-BILL-202"}],
    })
    summary = sync_qbo_to_ledger(client, LedgerClient(transport), "acme")
    assert summary.invoices == 0 and summary.bills == 0
    assert summary.documents_skipped == 2
    assert not transport.sent("/invoices")


# --- bank movements queue, never post ----------------------------------------

def test_bank_movements_go_to_the_review_queue_not_the_books() -> None:
    client, _http = _client()
    transport = FakeLedgerTransport()
    summary = sync_qbo_to_ledger(client, LedgerClient(transport), "acme")

    delivered = transport.sent("/feed/")[0]
    assert delivered["source"] == "quickbooks"
    ids = sorted(t["id"] for t in delivered["transactions"])
    assert ids == ["qbo-deposit-401", "qbo-purchase-301"]
    assert delivered["transactions"][1]["amount_minor"] == "-8900"
    assert summary.bank_new == 2 and summary.pending_review == 2

    # nothing was posted as a journal entry
    assert not [c for c in transport.calls if c[1].endswith("/entries")]


def test_the_bank_account_the_feed_lands_on_is_configurable() -> None:
    client, _http = _client()
    transport = FakeLedgerTransport()
    sync_qbo_to_ledger(client, LedgerClient(transport), "acme", bank_code="1010")
    assert any("/feed/1010" in c[1] for c in transport.calls)


# --- failures are reported, never swallowed ----------------------------------

def test_a_refused_document_is_reported_and_the_rest_still_syncs() -> None:
    client, _http = _client()
    transport = FakeLedgerTransport(fail={"/bills"})
    summary = sync_qbo_to_ledger(client, LedgerClient(transport), "acme")
    assert summary.invoices == 1, "the invoice still went across"
    assert summary.bills == 0
    assert not summary.ok
    assert any("bill" in e.lower() for e in summary.errors)


def test_a_quickbooks_outage_is_reported_not_treated_as_empty() -> None:
    class Broken:
        def get(self, url: str, headers: Any) -> Any:
            raise ConnectionError("quickbooks unreachable")

    client = QboApiClient(http=Broken(), api_base="https://x", realm_id="r", access_token="t")
    transport = FakeLedgerTransport()
    summary = sync_qbo_to_ledger(client, LedgerClient(transport), "acme")
    assert not summary.ok
    assert len(summary.errors) == 3     # invoices, bills, bank
    assert summary.invoices == 0 and summary.bank_new == 0


def test_a_malformed_amount_is_reported_rather_than_rounded_away() -> None:
    client, _http = _client(invoices=[
        {"Id": "999", "Balance": "not-a-number", "TxnDate": "2026-08-01",
         "CustomerRef": {"name": "Odd Co"}},
    ])
    transport = FakeLedgerTransport()
    summary = sync_qbo_to_ledger(client, LedgerClient(transport), "acme")
    assert summary.invoices == 0
    assert any("not a valid amount" in e for e in summary.errors)


def test_the_summary_reads_in_plain_language() -> None:
    client, _http = _client()
    transport = FakeLedgerTransport()
    summary = sync_qbo_to_ledger(client, LedgerClient(transport), "acme")
    assert summary.describe() == (
        "1 invoices and 1 bills brought across; 2 new bank transactions "
        "waiting for review (2 in the queue)"
    )
