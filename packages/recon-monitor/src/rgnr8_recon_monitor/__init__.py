"""RGNR8 reconciliation / trust monitor — the silent-divergence tripwire.

A cash system-of-record holds three views of the same number: the ledger's cash
balance, the bank feed's reported cash, and the opening balance the forecast was
seeded with. When they quietly drift apart — a dropped import, a mis-posted entry,
a stale seed — the owner is the last person who should discover it. This package is
the trust monitor that catches the gap first.

``check`` compares the three figures for one tenant at one injected ``as_of`` date,
computes the pairwise signed differences, and grades the worst gap against
caller-supplied tolerances into a frozen :class:`Divergence`
(``IN_SYNC`` / ``MINOR`` / ``MAJOR``) carrying a human-readable note that names the
diverging pair and the exact signed amount. ``trust_report`` folds a fleet of
divergences into a worst-first :class:`TrustReport` with per-severity counts for
the operator dashboard, and ``divergence_alert`` renders any out-of-tolerance
divergence as a payload the ops notification path can forward.

The shape is the house style: exact integer-minor-unit :class:`Money` throughout
(reused from ``rgnr8-forecast``), frozen dataclasses, an injected anchor date, and
no randomness — the same inputs always produce the same verdict, in tests and in
production. Detecting and surfacing drift is the whole job; auto-correcting it is
deliberately out of scope.
"""

from __future__ import annotations

from .alerting import divergence_alert
from .divergence import (
    Divergence,
    Severity,
    check,
    money_to_dict,
)
from .report import TrustReport, trust_report

__version__ = "0.1.0"

__all__ = [
    # divergence
    "Severity",
    "Divergence",
    "check",
    "money_to_dict",
    # report
    "TrustReport",
    "trust_report",
    # alerting
    "divergence_alert",
]
