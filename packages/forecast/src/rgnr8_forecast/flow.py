"""The resolved cash-flow record — the atomic, explainable unit the engine sums."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .enums import Category, Confidence, Direction, Scenario
from .models import Provenance
from .money import Money


@dataclass(frozen=True, slots=True)
class CashFlow:
    """A single dated movement of cash, with the reasoning that produced it.

    ``amount`` is always positive; ``direction`` carries the sign. Every flow
    keeps a plain-language ``basis`` and (where applicable) ``source`` provenance
    so any figure in the forecast can be traced and explained.
    """

    seq: int
    on_date: date
    direction: Direction
    amount: Money
    category: Category
    confidence: Confidence
    basis: str
    scenario: Scenario = Scenario.BASE
    source: Provenance | None = None
    origin_id: str | None = None

    @property
    def signed_minor(self) -> int:
        """Effect on cash in minor units (positive for inflow, negative for outflow)."""
        return self.amount.minor_units if self.direction is Direction.INFLOW else -self.amount.minor_units

    def signed_money(self) -> Money:
        return self.amount if self.direction is Direction.INFLOW else -self.amount
