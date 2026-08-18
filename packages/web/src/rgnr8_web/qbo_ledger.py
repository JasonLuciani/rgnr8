"""Push a connected QuickBooks company into the RGNR8 **ledger**.

Its sibling, :mod:`qbo_sync`, turns a QuickBooks company into forecast inputs —
a projection of cash. This module does the thing that makes RGNR8 a replacement
rather than an overlay: it moves the actual bookkeeping across.

Two flows, and the difference between them matters:

* **Open invoices and bills become real AR/AP documents.** They are what the
  business is owed and owes; they belong in the ledger as open items with
  control-account tie-out, not as forecast rows.
* **Bank movements land in the review inbox, not the general ledger.** A
  QuickBooks Purchase says money left the account; it does not say what for in
  terms of *this* chart of accounts. Auto-posting them to a default expense
  account would produce books that balance and mean nothing. So they queue for
  review, where rules and history pre-fill them and a human confirms.

Everything is idempotent by QuickBooks id, because re-syncing an overlapping
window is the normal case, not an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rgnr8_qbo import QboApiClient

from .ledger_client import LedgerClient


def _minor(decimal_dollars: str) -> str:
    """Parse a QuickBooks decimal-dollar string into integer minor units.

    Exact: QuickBooks sends "1234.56", and it never becomes a float on the way
    to a ledger that stores integers.
    """
    text = (decimal_dollars or "0").strip().replace(",", "").replace("$", "")
    negative = text.startswith("-")
    if negative:
        text = text[1:]
    if not text:
        return "0"
    whole, _, frac = text.partition(".")
    if not whole.isdigit() or (frac and not frac.isdigit()):
        raise ValueError(f"not a valid amount: {decimal_dollars!r}")
    if len(frac) > 2:
        frac = frac[:2]          # QuickBooks occasionally sends more precision
    minor = int(whole or "0") * 100 + int((frac or "0").ljust(2, "0"))
    return str(-minor if negative else minor)


def _slug(text: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in text)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


@dataclass
class LedgerSyncSummary:
    """What a sync actually moved — and what it deliberately did not post."""

    customers: int = 0
    vendors: int = 0
    invoices: int = 0
    bills: int = 0
    documents_skipped: int = 0
    bank_delivered: int = 0
    bank_new: int = 0
    bank_duplicates: int = 0
    pending_review: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def describe(self) -> str:
        return (
            f"{self.invoices} invoices and {self.bills} bills brought across; "
            f"{self.bank_new} new bank transactions waiting for review "
            f"({self.pending_review} in the queue)"
        )


def sync_qbo_to_ledger(
    client: QboApiClient,
    ledger: LedgerClient,
    tenant: str,
    *,
    bank_code: str = "1000",
    income_code: str = "4100",
    expense_code: str = "6400",
    since: str = "",
) -> LedgerSyncSummary:
    """Bring a connected company's open items and bank activity into the ledger.

    Nothing here posts an expense or income entry on its own judgement. The
    invoices and bills carry their own accounts because the business already
    decided them; the bank movements queue for review because it hasn't.
    """
    summary = LedgerSyncSummary()

    # --- open receivables ---------------------------------------------------
    try:
        invoices = client.open_invoices()
    except Exception as exc:                      # a live API failure
        summary.errors.append(f"could not read invoices: {exc}")
        invoices = []

    existing = _existing_ids(ledger, tenant, "invoices")
    for inv in invoices:
        doc_id = f"QBO-INV-{inv.id}"
        if doc_id in existing:
            summary.documents_skipped += 1
            continue
        party_id = _slug(inv.customer) or f"qbo-customer-{inv.id}"
        res = ledger.create_party(tenant, "customers", party_id, inv.customer or party_id)
        if res.ok:
            summary.customers += 1
        try:
            amount = _minor(inv.balance)
        except ValueError as exc:
            summary.errors.append(f"invoice {inv.doc_number or inv.id}: {exc}")
            continue
        created = ledger.create_document(
            tenant, "invoices", doc_id, party_id, inv.txn_date,
            [{
                "description": "Brought across from QuickBooks",
                "unit_amount_minor": amount,
                "account_code": income_code,
            }],
            memo=f"QuickBooks invoice {inv.doc_number or inv.id}",
            due_date=inv.due_date,
        )
        if created.ok:
            summary.invoices += 1
        else:
            summary.errors.append(f"invoice {doc_id}: {created.error()}")

    # --- open payables ------------------------------------------------------
    try:
        bills = client.open_bills()
    except Exception as exc:
        summary.errors.append(f"could not read bills: {exc}")
        bills = []

    existing = _existing_ids(ledger, tenant, "bills")
    for bill in bills:
        doc_id = f"QBO-BILL-{bill.id}"
        if doc_id in existing:
            summary.documents_skipped += 1
            continue
        party_id = _slug(bill.vendor) or f"qbo-vendor-{bill.id}"
        res = ledger.create_party(tenant, "vendors", party_id, bill.vendor or party_id)
        if res.ok:
            summary.vendors += 1
        try:
            amount = _minor(bill.balance)
        except ValueError as exc:
            summary.errors.append(f"bill {bill.id}: {exc}")
            continue
        created = ledger.create_document(
            tenant, "bills", doc_id, party_id, bill.txn_date,
            [{
                "description": "Brought across from QuickBooks",
                "unit_amount_minor": amount,
                "account_code": expense_code,
            }],
            memo=f"QuickBooks bill {bill.id}",
            due_date=bill.due_date,
        )
        if created.ok:
            summary.bills += 1
        else:
            summary.errors.append(f"bill {doc_id}: {created.error()}")

    # --- bank activity → the review queue, never straight to the books ------
    try:
        movements = client.bank_transactions(since=since)
    except Exception as exc:
        summary.errors.append(f"could not read bank transactions: {exc}")
        movements = []

    payload: list[dict[str, object]] = []
    for txn in movements:
        try:
            amount = _minor(txn.amount)
        except ValueError as exc:
            summary.errors.append(f"{txn.id}: {exc}")
            continue
        if amount == "0":
            continue
        payload.append({
            "id": txn.id,
            "date": txn.txn_date,
            "amount_minor": amount,
            "description": txn.description or f"{txn.kind} {txn.id}",
            "counterparty": txn.counterparty,
        })

    if payload:
        delivered = ledger.feed_deliver(tenant, bank_code, payload, source="quickbooks")
        if delivered.ok:
            summary.bank_delivered = len(payload)
            summary.bank_new = _int(delivered.body.get("added"))
            summary.bank_duplicates = _int(delivered.body.get("duplicates"))
            summary.pending_review = _int(delivered.body.get("pending"))
        else:
            summary.errors.append(f"bank transactions: {delivered.error()}")

    return summary


def _existing_ids(ledger: LedgerClient, tenant: str, kind: str) -> set[str]:
    """What is already in the ledger, so a re-sync doesn't duplicate it."""
    res = ledger.documents(tenant, kind)
    if not res.ok:
        return set()
    docs = res.body.get("documents")
    if not isinstance(docs, (list, tuple)):
        return set()
    out: set[str] = set()
    for d in docs:
        if isinstance(d, dict):
            out.add(str(d.get("id")))
    return out


def _int(v: object) -> int:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0
