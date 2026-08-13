"""AR report — aging summary, total AR, overdue total, and a DSO estimate.

Everything is computed from open invoices as of an injected anchor date. DSO
(days sales outstanding) is only populated when the caller supplies an average
daily sales figure — there is no wall-clock or revenue lookup baked in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from rgnr8_forecast import Invoice, Money

from .aging import AgingSummary, summarize_aging

# Open invoices are exactly the forecast engine's invoice shape; alias it so AR
# callers can name the concept without importing from the forecast package.
OpenInvoice = Invoice


@dataclass(frozen=True, slots=True)
class ARReport:
    """A point-in-time accounts-receivable snapshot.

    ``dso`` is ``total_ar / avg_daily_sales`` in days, or ``None`` when no average
    daily sales figure was supplied (or it was non-positive).
    """

    as_of: date
    aging: AgingSummary
    total_ar: Money
    overdue_total: Money
    dso: float | None


def ar_report(
    invoices: Iterable[Invoice],
    as_of: date,
    *,
    avg_daily_sales: Money | None = None,
    currency: str = "USD",
) -> ARReport:
    """Build an :class:`ARReport` from open invoices as of ``as_of``.

    DSO is populated only when ``avg_daily_sales`` is provided and positive; it is
    computed in the same currency's minor units, so the ratio is unit-free days.
    """
    aging = summarize_aging(invoices, as_of, currency=currency)

    dso: float | None = None
    if avg_daily_sales is not None:
        if avg_daily_sales.currency != aging.grand_total.currency:
            raise ValueError(
                "avg_daily_sales currency "
                f"{avg_daily_sales.currency!r} does not match AR currency "
                f"{aging.grand_total.currency!r}"
            )
        if avg_daily_sales.is_positive:
            dso = aging.grand_total.minor_units / avg_daily_sales.minor_units

    return ARReport(
        as_of=as_of,
        aging=aging,
        total_ar=aging.grand_total,
        overdue_total=aging.overdue_total,
        dso=dso,
    )
