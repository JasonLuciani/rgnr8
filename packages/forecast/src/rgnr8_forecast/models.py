"""Domain inputs, configuration, and the resolved cash-flow record.

Design principles (shared with the accounting kernel):
- Money is integer minor units — never float.
- No wall clock: every date is an explicit input, so a forecast is reproducible.
- Every projected flow keeps provenance back to the record that produced it, so
  any number in the forecast can be drilled to its source (the product's
  progressive-disclosure and answer-validator requirements).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .dates import Recurrence
from .enums import Category, Confidence, Direction, InvoiceStatus
from .money import Money


# --- provenance --------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a projected flow came from."""

    source_system: str
    source_type: str
    source_id: str
    source_version: str = "1"


# --- opening position --------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CashPosition:
    """Verified cash as of the anchor date.

    ``available`` is spendable posted cash. ``restricted`` is cash that exists
    but cannot be used (held for taxes, collateral); it raises the effective
    minimum-cash floor rather than being spent.
    """

    as_of: date
    available: Money
    restricted: Money = field(default=Money(0))
    verified: bool = True


# --- receivables / payables --------------------------------------------------
@dataclass(frozen=True, slots=True)
class Invoice:
    """An open customer invoice (AR). Only ``open_amount`` remains to be paid."""

    id: str
    customer_id: str
    issue_date: date
    due_date: date
    open_amount: Money
    status: InvoiceStatus = InvoiceStatus.OPEN
    provenance: Provenance | None = None


@dataclass(frozen=True, slots=True)
class PaymentObservation:
    """A historical (due_date, paid_date) pair used to learn payment timing."""

    due_date: date
    paid_date: date

    @property
    def days_late(self) -> int:
        return (self.paid_date - self.due_date).days


@dataclass(frozen=True, slots=True)
class CustomerHistory:
    customer_id: str
    observations: tuple[PaymentObservation, ...] = ()
    # An explicit override wins over learned behavior (customer-specific policy).
    override_days_late: int | None = None


@dataclass(frozen=True, slots=True)
class Bill:
    """An open vendor bill (AP). Paid on ``scheduled_date`` if set, else ``due_date``."""

    id: str
    vendor_id: str
    due_date: date
    amount: Money
    scheduled_date: date | None = None
    provenance: Provenance | None = None


# --- scheduled / recurring commitments --------------------------------------
@dataclass(frozen=True, slots=True)
class RecurringItem:
    """A recurring inflow or outflow (rent, subscriptions, retainers)."""

    label: str
    category: Category
    direction: Direction
    amount: Money
    recurrence: Recurrence
    provenance: Provenance | None = None


@dataclass(frozen=True, slots=True)
class PayrollSchedule:
    """Payroll timing. Net pay leaves on payday; payroll taxes are remitted
    ``tax_remit_lag_days`` later (gross/net/liability timing handled explicitly)."""

    label: str
    recurrence: Recurrence
    net_pay: Money
    payroll_taxes: Money = field(default=Money(0))
    tax_remit_lag_days: int = 3
    provenance: Provenance | None = None


@dataclass(frozen=True, slots=True)
class DebtInstrument:
    """A loan/lease. Each payment is a single cash outflow; the principal/
    interest split is retained as metadata for reporting, not double-counted."""

    label: str
    recurrence: Recurrence
    payment: Money
    principal_portion: Money | None = None
    interest_portion: Money | None = None
    provenance: Provenance | None = None


# --- one-off items -----------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OneTimeItem:
    """A dated one-off flow: a planned purchase, distribution, tax payment,
    or a pending bank transaction. ``confidence`` marks how firm it is."""

    label: str
    category: Category
    direction: Direction
    amount: Money
    on_date: date
    confidence: Confidence = Confidence.PLANNED
    provenance: Provenance | None = None


@dataclass(frozen=True, slots=True)
class PipelineOpportunity:
    """Unbilled/uncertain future revenue. Excluded from BASE; in UPSIDE it is
    included at its probability-weighted amount (deterministic, no sampling)."""

    label: str
    customer_id: str
    expected_date: date
    amount: Money
    probability_bps: int  # 0..10000 (basis points)
    provenance: Provenance | None = None


# --- the full input bundle ---------------------------------------------------
@dataclass(frozen=True, slots=True)
class ForecastInputs:
    opening: CashPosition
    currency: str = "USD"
    invoices: tuple[Invoice, ...] = ()
    customer_histories: tuple[CustomerHistory, ...] = ()
    bills: tuple[Bill, ...] = ()
    recurring: tuple[RecurringItem, ...] = ()
    payroll: tuple[PayrollSchedule, ...] = ()
    debt: tuple[DebtInstrument, ...] = ()
    one_time: tuple[OneTimeItem, ...] = ()
    pipeline: tuple[PipelineOpportunity, ...] = ()

    def history_for(self, customer_id: str) -> CustomerHistory | None:
        for h in self.customer_histories:
            if h.customer_id == customer_id:
                return h
        return None


# --- scenario assumptions ----------------------------------------------------
@dataclass(frozen=True, slots=True)
class ScenarioAssumptions:
    """Explicit, named deltas that define a scenario. Base/downside/upside run
    through the SAME engine and differ only by this object (per the blueprint)."""

    # Extra days added to every predicted AR payment (stress on collections).
    ar_extra_delay_days: int = 0
    # Invoices older than this many days past due are treated as bad debt...
    bad_debt_age_days: int | None = None
    # ...and written down by this fraction (basis points, 10000 = 100%).
    bad_debt_bps: int = 0
    # Refund/chargeback reserve as bps of projected customer receipts.
    refund_reserve_bps: int = 0
    # Include pipeline opportunities at probability weighting (UPSIDE only).
    include_pipeline: bool = False
    # Collect AR on the due date, ignoring predicted lateness (optimistic).
    ar_on_time: bool = False


def base_assumptions() -> ScenarioAssumptions:
    return ScenarioAssumptions()


def downside_assumptions() -> ScenarioAssumptions:
    return ScenarioAssumptions(
        ar_extra_delay_days=14,
        bad_debt_age_days=90,
        bad_debt_bps=2500,  # write down 25% of >90d-past-due AR
        refund_reserve_bps=150,  # 1.5% refund/chargeback reserve
    )


def upside_assumptions() -> ScenarioAssumptions:
    return ScenarioAssumptions(include_pipeline=True, ar_on_time=True)


# --- configuration -----------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ForecastConfig:
    currency: str = "USD"
    horizon_weeks: int = 13
    minimum_cash: Money = field(default=Money(0))
    # Fallback AR delay when a customer has no usable history.
    default_payment_delay_days: int = 5
    # Minimum observations before learned timing is trusted over the default.
    min_history_observations: int = 3
    # Version identifiers that participate in reproducibility fingerprinting.
    engine_version: str = "0.1.0"
    assumption_version: str = "2026-08-a"
    mapping_version: str = "map-1"
    model_version: str = "timing-1"
    timezone: str = "America/Denver"
    rounding_policy: str = "half-even-minor-unit"
