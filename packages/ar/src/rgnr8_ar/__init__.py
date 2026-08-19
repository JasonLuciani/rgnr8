"""RGNR8 AR / collections — turn open invoices into action.

The #1 SMB cash lever is getting paid faster. This package takes the same open
invoices the forecast engine consumes and produces the collections surface around
them: an aging view, a prioritized chase list, drafted reminder nudges, and a
point-in-time AR report.

Built on ``rgnr8-forecast``: it reuses that package's exact ``Money`` and
``Invoice`` / ``CustomerHistory`` shapes rather than re-declaring them, so AR and
the forecast agree on receivables by construction.

Everything is deterministic — the anchor date is an injected ``as_of`` (never the
wall clock), there is no randomness, and money is exact integer minor units:

  * :mod:`.aging`  — ``AgingBucket``, ``bucket_for``, ``AgingSummary``.
  * :mod:`.report` — ``ARReport`` and ``ar_report`` (totals, overdue, DSO estimate).
  * :mod:`.chase`  — ``ChaseItem`` and ``chase_list`` (amount x days-overdue x risk).
  * :mod:`.nudge`  — ``CollectionNudge`` and ``draft_nudge`` (tone escalates by age).

Non-goal: sending email. Nudges are drafts only; delivery is a downstream seam.
"""

from __future__ import annotations

from .aging import (
    BUCKET_ORDER,
    AgingBucket,
    AgingSummary,
    bucket_for,
    days_overdue,
    summarize_aging,
)
from .chase import (
    NEUTRAL_RISK_WEIGHT,
    ChaseItem,
    chase_list,
    risk_weight,
    typical_days_late,
)
from .nudge import CollectionNudge, NudgeTone, draft_nudge, tone_for
from .report import ARReport, OpenInvoice, ar_report

__version__ = "0.1.0"

__all__ = [
    # aging
    "AgingBucket",
    "BUCKET_ORDER",
    "bucket_for",
    "days_overdue",
    "AgingSummary",
    "summarize_aging",
    # report
    "OpenInvoice",
    "ARReport",
    "ar_report",
    # chase
    "ChaseItem",
    "chase_list",
    "risk_weight",
    "typical_days_late",
    "NEUTRAL_RISK_WEIGHT",
    # nudge
    "CollectionNudge",
    "NudgeTone",
    "draft_nudge",
    "tone_for",
]
