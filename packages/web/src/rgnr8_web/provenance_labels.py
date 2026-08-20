"""Number-provenance labels — telling the owner which figure is safe to act on.

A financial screen mixes numbers of very different trustworthiness: a *forecast*
of next week's cash is a guess; a *posted* journal figure is recorded fact; a
*reconciled* balance has been matched to the bank; a *sealed* figure lives in a
published, locked period and will not move. Presented in the same typeface they
invite the reader to trust the weakest number as much as the strongest.

This module gives every figure a small, consistent provenance badge and a legend
that explains the ladder, so an owner can tell at a glance what they are looking
at. The levels are ordered by trust; :func:`most_cautious` collapses a set of
figures to the weakest one so a summary never claims more certainty than its
inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from html import escape


class Provenance(Enum):
    """How a displayed number came to be, from least to most trustworthy."""

    FORECAST = "forecast"
    IMPORTED = "imported"
    POSTED = "posted"
    RECONCILED = "reconciled"
    SEALED = "sealed"


@dataclass(frozen=True, slots=True)
class _Meta:
    label: str
    rank: int
    #: A brand tone token driving the badge colour (falls back to a safe default).
    tone: str
    tooltip: str


# Ordered weakest → strongest. `rank` makes the ordering explicit and comparable.
_META: dict[Provenance, _Meta] = {
    Provenance.FORECAST: _Meta(
        "Forecast", 0, "var(--rg-risk,#b4462f)",
        "A projection from your assumptions — not yet a recorded fact.",
    ),
    Provenance.IMPORTED: _Meta(
        "Imported", 1, "var(--rg-warn,#8a6d00)",
        "Brought in from a bank feed or QuickBooks, but not yet posted to the ledger.",
    ),
    Provenance.POSTED: _Meta(
        "Posted", 2, "var(--rg-ink,#2b2b2b)",
        "Recorded in the ledger as a posted journal entry.",
    ),
    Provenance.RECONCILED: _Meta(
        "Reconciled", 3, "var(--rg-good,#1f6f43)",
        "Matched to a bank statement — the balance agrees with the bank.",
    ),
    Provenance.SEALED: _Meta(
        "Sealed", 4, "var(--rg-good,#1f6f43)",
        "In a published, locked period — this figure is final and will not move.",
    ),
}


def rank(level: Provenance) -> int:
    """Trust rank (higher = stronger). Useful for sorting or thresholds."""
    return _META[level].rank


def label(level: Provenance) -> str:
    """The human-readable name of a provenance level."""
    return _META[level].label


def most_cautious(levels: "list[Provenance] | tuple[Provenance, ...]") -> Provenance:
    """The weakest provenance in a set — a summary is only as trustworthy as its
    least-trustworthy input. Defaults to FORECAST for an empty set (assume the
    least certainty rather than the most)."""
    if not levels:
        return Provenance.FORECAST
    return min(levels, key=rank)


def badge(level: Provenance, *, title: bool = True) -> str:
    """A small inline provenance badge for placing next to a figure."""
    m = _META[level]
    tip = f' title="{escape(m.tooltip)}"' if title else ""
    return (
        f'<span class="rg-prov rg-prov-{level.value}"{tip} '
        f'style="display:inline-block;font:600 10px/1.4 var(--rg-sans);'
        f'letter-spacing:.04em;text-transform:uppercase;padding:1px 6px;'
        f'border-radius:999px;color:#fff;background:{m.tone};'
        f'vertical-align:middle;white-space:nowrap">{escape(m.label)}</span>'
    )


def legend(levels: "tuple[Provenance, ...]" = tuple(Provenance)) -> str:
    """A legend explaining the provenance ladder, weakest → strongest."""
    ordered = sorted(levels, key=rank)
    rows = "".join(
        f'<div style="display:flex;gap:8px;align-items:baseline;margin:2px 0">'
        f"{badge(lv, title=False)}"
        f'<span class="muted" style="font-size:12px">{escape(_META[lv].tooltip)}</span>'
        f"</div>"
        for lv in ordered
    )
    return (
        '<details class="rg-prov-legend" style="margin:8px 0">'
        '<summary style="cursor:pointer;font:600 12px/1.4 var(--rg-sans);color:var(--rg-muted,#666)">'
        "What do the labels mean?</summary>"
        f'<div style="margin:6px 0 0">{rows}</div>'
        "</details>"
    )
