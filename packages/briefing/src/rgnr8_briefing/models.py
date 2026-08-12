"""Structured briefing objects.

Everything the owner sees is a `Fact` (or `Driver`) that carries `Evidence`
pointing back into the forecast — a projection field or a set of cash-flow
sequence numbers. Nothing renders unless it recomputes from the forecast (see
``validate.py``): that is the unsupported-number guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Literal

from rgnr8_forecast import Category, Direction, Money


class StatusLevel(str, Enum):
    STABLE = "STABLE"
    WATCH = "WATCH"
    AT_RISK = "AT_RISK"


EvidenceKind = Literal["field", "flows"]


@dataclass(frozen=True, slots=True)
class Evidence:
    """How a stated number is backed.

    - kind="field": ``field`` names a resolvable projection value
      (e.g. "trough_balance", "effective_floor", "cushion_at_trough").
    - kind="flows": ``flow_seqs`` are the CashFlow sequence numbers whose
      magnitudes sum to the stated amount.
    """

    kind: EvidenceKind
    field: str | None = None
    flow_seqs: tuple[int, ...] = ()
    note: str = ""


@dataclass(frozen=True, slots=True)
class Fact:
    key: str
    label: str
    evidence: Evidence
    amount: Money | None = None
    text: str | None = None
    confidence: int | None = None


@dataclass(frozen=True, slots=True)
class Driver:
    category: Category
    label: str
    direction: Direction
    amount: Money  # magnitude
    share_bps: int  # share of total inflows/outflows in basis points
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class WeekGlance:
    index: int
    start: date
    closing: Money
    status: StatusLevel
    confidence: int


@dataclass(frozen=True, slots=True)
class WeeklyBriefing:
    as_of: date
    currency: str
    period_label: str
    status: StatusLevel
    status_reason: str
    headline: str
    primary_action: str | None
    facts: tuple[Fact, ...]
    drivers: tuple[Driver, ...]
    week_glance: tuple[WeekGlance, ...]
    data_quality: tuple[str, ...]
    overall_confidence: int
    version_id: str


@dataclass(frozen=True, slots=True)
class Violation:
    """A number that did not reconcile to the forecast — must block publish."""

    where: str
    message: str
    stated: Money | None = None
    expected: Money | None = None
