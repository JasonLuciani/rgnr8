"""Classify silent divergence between the three cash figures at an anchor date.

The trust monitor compares, for a single tenant at a single ``as_of`` date:

* ``ledger_cash`` — the cash balance the double-entry ledger believes it holds,
* ``bank_cash`` — the cash the bank feed reports,
* ``forecast_opening`` — the opening balance the forecast was seeded with.

In a system-of-record these three must agree; a quiet gap between any pair is the
early signature of a dropped import, a mis-posted entry, or a stale forecast seed.
``check`` computes the pairwise signed differences, picks the worst gap, grades it
against caller-supplied tolerances, and returns an immutable :class:`Divergence`
with a human-readable note. Everything is exact integer-minor-unit money math and
pure — the ``as_of`` date is injected, there is no clock read and no randomness, so
the same inputs always produce the same verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from rgnr8_forecast import CurrencyMismatch, Money


class Severity(Enum):
    """How badly the three cash figures disagree, worst last."""

    IN_SYNC = "in_sync"
    MINOR = "minor"
    MAJOR = "major"


def money_to_dict(amount: Money) -> dict[str, object]:
    """Serialize a :class:`Money` to a JSON-safe dict for the dashboard."""
    return {
        "minor_units": amount.minor_units,
        "currency": amount.currency,
        "display": str(amount),
    }


def _require_same_currency(reference: Money, *others: Money) -> None:
    """Reject any figure whose currency differs from ``reference``."""
    for other in others:
        if other.currency != reference.currency:
            raise CurrencyMismatch(f"{reference.currency} vs {other.currency}")


@dataclass(frozen=True, slots=True)
class Divergence:
    """The graded reconciliation verdict for one tenant at one anchor date.

    ``ledger_vs_bank`` and ``bank_vs_forecast`` are signed differences (a positive
    value means the first named figure is the larger). ``worst_gap`` is the
    magnitude (always non-negative) of whichever pair diverges most; it is the
    number ``severity`` is graded from and the fleet report sorts on.
    """

    tenant_id: str
    as_of: date
    ledger_cash: Money
    bank_cash: Money
    forecast_opening: Money
    ledger_vs_bank: Money
    bank_vs_forecast: Money
    worst_gap: Money
    severity: Severity
    note: str

    def to_dict(self) -> dict[str, object]:
        """A JSON-safe view of this divergence for the operator dashboard."""
        return {
            "tenant_id": self.tenant_id,
            "as_of": self.as_of.isoformat(),
            "ledger_cash": money_to_dict(self.ledger_cash),
            "bank_cash": money_to_dict(self.bank_cash),
            "forecast_opening": money_to_dict(self.forecast_opening),
            "ledger_vs_bank": money_to_dict(self.ledger_vs_bank),
            "bank_vs_forecast": money_to_dict(self.bank_vs_forecast),
            "worst_gap": money_to_dict(self.worst_gap),
            "severity": self.severity.value,
            "note": self.note,
        }


def _pair_note(first: str, second: str, diff: Money) -> str:
    """Describe a signed pair difference in words (``first - second``)."""
    magnitude = diff.abs()
    if diff.is_zero:
        return f"{first} and {second} agree"
    ahead, behind = (first, second) if diff.is_positive else (second, first)
    return f"{ahead} exceeds {behind} by {magnitude}"


def check(
    tenant_id: str,
    as_of: date,
    ledger_cash: Money,
    bank_cash: Money,
    forecast_opening: Money,
    *,
    minor_tolerance: Money,
    major_tolerance: Money,
) -> Divergence:
    """Grade the three cash figures into a :class:`Divergence`.

    Computes the pairwise signed diffs ``ledger - bank`` and ``bank - forecast``,
    takes the larger magnitude as ``worst_gap``, and grades it:
    ``IN_SYNC`` when ``worst_gap <= minor_tolerance``, ``MINOR`` when
    ``worst_gap <= major_tolerance``, otherwise ``MAJOR``. All figures and both
    tolerances must share one currency; a mismatch raises ``CurrencyMismatch``.
    """
    _require_same_currency(
        ledger_cash,
        bank_cash,
        forecast_opening,
        minor_tolerance,
        major_tolerance,
    )

    ledger_vs_bank = ledger_cash - bank_cash
    bank_vs_forecast = bank_cash - forecast_opening

    # Pick the worst-diverging pair; ledger-vs-bank wins ties for determinism.
    if bank_vs_forecast.abs().minor_units > ledger_vs_bank.abs().minor_units:
        worst_pair = _pair_note("bank", "forecast opening", bank_vs_forecast)
        worst_gap = bank_vs_forecast.abs()
    else:
        worst_pair = _pair_note("ledger", "bank", ledger_vs_bank)
        worst_gap = ledger_vs_bank.abs()

    worst = worst_gap.minor_units
    if worst <= minor_tolerance.minor_units:
        severity = Severity.IN_SYNC
    elif worst <= major_tolerance.minor_units:
        severity = Severity.MINOR
    else:
        severity = Severity.MAJOR

    if severity is Severity.IN_SYNC:
        note = (
            f"ledger, bank, and forecast opening agree within tolerance "
            f"(worst gap {worst_gap})"
        )
    else:
        note = f"{severity.value} divergence: {worst_pair} (worst gap {worst_gap})"

    return Divergence(
        tenant_id=tenant_id,
        as_of=as_of,
        ledger_cash=ledger_cash,
        bank_cash=bank_cash,
        forecast_opening=forecast_opening,
        ledger_vs_bank=ledger_vs_bank,
        bank_vs_forecast=bank_vs_forecast,
        worst_gap=worst_gap,
        severity=severity,
        note=note,
    )
