"""Turn a connected QuickBooks company into RGNR8 forecast inputs.

Once a tenant is connected, this pulls the live figures from QuickBooks (via
:class:`rgnr8_qbo.QboApiClient`) and maps them into the platform's
:class:`~rgnr8_forecast.ForecastInputs`, so the cash outlook, briefing, and reports
reflect the real books rather than placeholder numbers:

* **bank account balances** → opening cash (``CashPosition.available``),
* **open invoices** (AR) → :class:`~rgnr8_forecast.Invoice` inflows,
* **open bills** (AP) → dated :class:`~rgnr8_forecast.OneTimeItem` outflows.

Amounts arrive from QBO as decimal-dollar strings and are parsed with the shared
:class:`~rgnr8_forecast.Money` (exact minor units). Dates fall back gracefully
(due date → txn date → the anchor date) so a record with a missing field still
lands somewhere sensible on the timeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from rgnr8_forecast import (
    CashPosition,
    Category,
    Confidence,
    Direction,
    ForecastInputs,
    Invoice,
    Money,
    OneTimeItem,
)
from rgnr8_qbo import QboApiClient


@dataclass(frozen=True, slots=True)
class QboSyncSummary:
    """A human-readable summary of what a sync pulled (for the UI + audit)."""

    company: str
    cash: Money
    bank_accounts: int
    invoice_count: int
    ar_total: Money
    bill_count: int
    ap_total: Money


def _money(s: str) -> Money:
    try:
        return Money.from_decimal(s or "0")
    except (ValueError, ArithmeticError):
        return Money.zero()


def _date(fallback: date, *candidates: str) -> date:
    for c in candidates:
        if c:
            try:
                return date.fromisoformat(c)
            except ValueError:
                continue
    return fallback


def build_inputs_from_qbo(
    client: QboApiClient, *, as_of: date, currency: str = "USD"
) -> tuple[ForecastInputs, QboSyncSummary]:
    """Pull the connected company and build :class:`ForecastInputs` + a summary."""
    company = client.company_info()
    banks = client.bank_accounts()
    invoices = client.open_invoices()
    bills = client.open_bills()

    cash = Money.zero(currency)
    for b in banks:
        cash = cash + _money(b.current_balance)

    inv_models: list[Invoice] = []
    ar_total = Money.zero(currency)
    for inv in invoices:
        amount = _money(inv.balance)
        ar_total = ar_total + amount
        issue = _date(as_of, inv.txn_date)
        due = _date(issue, inv.due_date, inv.txn_date)
        inv_models.append(
            Invoice(
                id=inv.doc_number or inv.id,
                customer_id=inv.customer or "customer",
                issue_date=issue,
                due_date=due,
                open_amount=amount,
            )
        )

    bill_items: list[OneTimeItem] = []
    ap_total = Money.zero(currency)
    for bill in bills:
        amount = _money(bill.balance)
        ap_total = ap_total + amount
        on = _date(as_of, bill.due_date, bill.txn_date)
        bill_items.append(
            OneTimeItem(
                label=bill.vendor or "Bill",
                category=Category.VENDOR_PAYMENT,
                direction=Direction.OUTFLOW,
                amount=amount,
                on_date=on,
                confidence=Confidence.RECORDED,
            )
        )

    inputs = ForecastInputs(
        opening=CashPosition(as_of=as_of, available=cash, verified=True),
        currency=currency,
        invoices=tuple(inv_models),
        one_time=tuple(bill_items),
    )
    summary = QboSyncSummary(
        company=company.name,
        cash=cash,
        bank_accounts=len(banks),
        invoice_count=len(inv_models),
        ar_total=ar_total,
        bill_count=len(bill_items),
        ap_total=ap_total,
    )
    return inputs, summary
