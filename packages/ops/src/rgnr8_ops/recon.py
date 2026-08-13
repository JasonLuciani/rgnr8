"""Per-client trust figures the operator attaches, feeding the recon monitor.

Cash-at-risk is only trustworthy if the three views of cash agree: the ledger's
balance, the bank feed's reported cash, and the opening balance the forecast was
seeded with. `TenantRecon` is the small, operator-set input that carries those
three figures (plus the tolerances) for one client — the trust analogue of
`TenantOpsStatus`. `build_ops_report` folds it through `rgnr8_recon_monitor.check`
into a graded `Divergence` so the operator console can show, per client, whether
ledger / bank / forecast are in sync or silently drifting, with the exact amount
and severity.

Everything is exact integer-minor-unit `Money` and pure — the ``as_of`` anchor is
injected by the report, there is no clock read here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from rgnr8_forecast import Money
from rgnr8_recon_monitor import Divergence, check

# Default grading tolerances (in the figures' own currency): agree within $1.00 →
# IN_SYNC, within $100.00 → MINOR, beyond → MAJOR. Operators can override per set.
DEFAULT_MINOR_TOLERANCE_MINOR = 100      # $1.00
DEFAULT_MAJOR_TOLERANCE_MINOR = 10_000   # $100.00


@dataclass(frozen=True, slots=True)
class TenantRecon:
    """The three cash figures (+ tolerances) the operator attaches for one client."""

    ledger_cash: Money
    bank_cash: Money
    forecast_opening: Money
    minor_tolerance: Money
    major_tolerance: Money

    def divergence(self, tenant_id: str, as_of: date) -> Divergence:
        """Grade this client's three figures into a `Divergence` at ``as_of``."""
        return check(
            tenant_id,
            as_of,
            self.ledger_cash,
            self.bank_cash,
            self.forecast_opening,
            minor_tolerance=self.minor_tolerance,
            major_tolerance=self.major_tolerance,
        )


def make_recon(
    ledger_cash: Money,
    bank_cash: Money,
    forecast_opening: Money,
    *,
    minor_tolerance: Money | None = None,
    major_tolerance: Money | None = None,
) -> TenantRecon:
    """Build a `TenantRecon`, defaulting the grading tolerances into the ledger
    figure's currency so the whole set shares one currency (as `check` requires)."""
    ccy = ledger_cash.currency
    minor = minor_tolerance if minor_tolerance is not None else Money(DEFAULT_MINOR_TOLERANCE_MINOR, ccy)
    major = major_tolerance if major_tolerance is not None else Money(DEFAULT_MAJOR_TOLERANCE_MINOR, ccy)
    return TenantRecon(
        ledger_cash=ledger_cash,
        bank_cash=bank_cash,
        forecast_opening=forecast_opening,
        minor_tolerance=minor,
        major_tolerance=major,
    )
