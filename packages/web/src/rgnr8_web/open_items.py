"""QuickBooks' open invoices and bills, as RGNR8 shows them during a parallel run.

The mirrored books carry A/R and A/P balances but no documents behind them, so
Invoices and Collections showed $0 owed and the forecast saw no receivables.
Each Refresh stores QuickBooks' open items in the ledger as a non-posting
snapshot (see the ledger's openItems.ts); this module takes it there and reads
it back, and says how it ties to the books' A/R and A/P balances.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from rgnr8_forecast import Bill, Invoice, Money, Provenance

QUICKBOOKS = "quickbooks"


def _minor(decimal_dollars: str) -> int:
    """QuickBooks' decimal-dollar string to minor units, exactly (no float)."""
    raw = (decimal_dollars or "0").strip().replace(",", "")
    neg = raw.startswith("-")
    whole, _, frac = raw.lstrip("-").partition(".")
    cents = int(whole or "0") * 100 + int((frac + "00")[:2] or "0")
    return -cents if neg else cents


def snapshot_from_quickbooks(client: Any, as_of: date) -> dict[str, object]:
    """What QuickBooks has open right now, in the shape the ledger stores."""
    invoices = [
        {"qbo_id": i.id, "number": i.doc_number, "party": i.customer, "date": i.txn_date,
         "due_date": i.due_date or i.txn_date, "open_minor": str(_minor(i.balance))}
        for i in client.open_invoices() if _minor(i.balance) > 0
    ]
    bills = [
        {"qbo_id": b.id, "number": "", "party": b.vendor, "date": b.txn_date,
         "due_date": b.due_date or b.txn_date, "open_minor": str(_minor(b.balance))}
        for b in client.open_bills() if _minor(b.balance) > 0
    ]
    return {"as_of": as_of.isoformat(), "invoices": invoices, "bills": bills}


@dataclass(frozen=True, slots=True)
class OpenItems:
    as_of: str
    invoices: tuple[dict[str, Any], ...]
    bills: tuple[dict[str, Any], ...]
    invoice_total: Money
    bill_total: Money

    @property
    def empty(self) -> bool:
        return not self.invoices and not self.bills


def read_open_items(ledger: Any, tenant: str, currency: str = "USD") -> "OpenItems | None":
    """The stored snapshot, or None when there is none (or the ledger can't say:
    an older ledger without the route, or one that is down; both mean "no
    QuickBooks copy to show", never an error on the page)."""
    if not hasattr(ledger, "open_items"):
        return None
    res = ledger.open_items(tenant)
    if not res.ok or not res.body.get("as_of"):
        return None
    body = res.body

    def rows(key: str) -> tuple[dict[str, Any], ...]:
        raw = body.get(key)
        return tuple(r for r in raw if isinstance(r, dict)) if isinstance(raw, list) else ()

    def total(key: str) -> Money:
        try:
            return Money(int(str(body.get(key) or "0")), currency)
        except ValueError:
            return Money(0, currency)

    return OpenItems(
        as_of=str(body["as_of"]),
        invoices=rows("invoices"),
        bills=rows("bills"),
        invoice_total=total("invoice_total_minor"),
        bill_total=total("bill_total_minor"),
    )


def _day(raw: object, fallback: date) -> date:
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return fallback


def as_forecast_items(
    items: OpenItems, as_of: date, currency: str
) -> tuple[tuple[Invoice, ...], tuple[Bill, ...]]:
    """The snapshot as forecast facts, labelled as coming from QuickBooks."""
    invoices = tuple(Invoice(
        id=str(r.get("number") or r.get("qbo_id")),
        customer_id=str(r.get("party") or "customer"),
        issue_date=_day(r.get("date"), as_of),
        due_date=_day(r.get("due_date"), as_of),
        open_amount=Money(int(str(r.get("open_minor") or "0")), currency),
        provenance=Provenance(QUICKBOOKS, "invoice", str(r.get("qbo_id"))),
    ) for r in items.invoices)
    bills = tuple(Bill(
        id=str(r.get("qbo_id")),
        vendor_id=str(r.get("party") or "vendor"),
        due_date=_day(r.get("due_date"), as_of),
        amount=Money(int(str(r.get("open_minor") or "0")), currency),
        provenance=Provenance(QUICKBOOKS, "bill", str(r.get("qbo_id"))),
    ) for r in items.bills)
    return invoices, bills
