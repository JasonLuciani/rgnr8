"""The :class:`DataContext` — everything a report might read, in one frozen bag.

A report engine that composes many data sources needs a single, uniform place to
read them from. ``DataContext`` is that place: it carries every optional input a
section builder could want (a forecast result, open invoices, register
transactions, a usage summary, reconciliation figures, the cross-language
``financial-statements/1`` contract, a budget, and scenario inputs) plus the
report frame (period label, anchor date, currency).

Every data field is optional. A section whose data is absent degrades to a
graceful "not available" block rather than failing — so the same context can feed
a cash-only report and a full board pack, and each renders exactly the sections it
has data for. The context is a frozen dataclass and holds only immutable / read
shapes, so building a report never mutates its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Optional

from rgnr8_billing import Account, UsageSummary
from rgnr8_forecast import (
    CustomerHistory,
    ForecastConfig,
    ForecastInputs,
    ForecastResult,
    Invoice,
    Money,
)
from rgnr8_scenario import Scenario as ScenarioAdjustments


@dataclass(frozen=True, slots=True)
class Transaction:
    """A single bank-register movement. ``amount_minor`` is signed: positive for
    money in, negative for money out. This is the light register shape the
    transaction/budget sections read; the forecast engine's richer ``CashFlow``
    lives elsewhere."""

    on_date: date
    description: str
    category: str
    amount_minor: int
    currency: str = "USD"

    @property
    def money(self) -> Money:
        """The signed amount as :class:`Money`."""
        return Money(self.amount_minor, self.currency)

    @property
    def is_inflow(self) -> bool:
        return self.amount_minor >= 0


@dataclass(frozen=True, slots=True)
class ReconFigures:
    """The three cash figures the trust monitor reconciles, plus tolerances.

    Feeds :func:`rgnr8_recon_monitor.check` directly; all five must share a
    currency."""

    tenant_id: str
    ledger_cash: Money
    bank_cash: Money
    forecast_opening: Money
    minor_tolerance: Money
    major_tolerance: Money


@dataclass(frozen=True, slots=True)
class DataContext:
    """Everything a report might read. Every data source is optional; missing data
    makes its section degrade gracefully."""

    # --- report frame --------------------------------------------------------
    period: str = ""
    as_of: date = date(1970, 1, 1)
    currency: str = "USD"

    # --- forecast / cash -----------------------------------------------------
    forecast: Optional[ForecastResult] = None

    # --- accounts receivable -------------------------------------------------
    invoices: tuple[Invoice, ...] = ()
    histories: Mapping[str, CustomerHistory] = field(default_factory=dict)
    avg_daily_sales: Optional[Money] = None

    # --- register ------------------------------------------------------------
    transactions: tuple[Transaction, ...] = ()

    # --- billing / usage -----------------------------------------------------
    usage: Optional[UsageSummary] = None
    account: Optional[Account] = None

    # --- reconciliation / trust ---------------------------------------------
    recon: Optional[ReconFigures] = None

    # --- financial statements (financial-statements/1 contract) --------------
    financial_statements: Optional[Mapping[str, object]] = None

    # --- budget vs actual ----------------------------------------------------
    budget: Optional[Mapping[str, Money]] = None

    # --- scenario / what-if --------------------------------------------------
    forecast_inputs: Optional[ForecastInputs] = None
    forecast_config: Optional[ForecastConfig] = None
    scenario: Optional[ScenarioAdjustments] = None
