"""Aggregate per-tenant divergences into a fleet-wide trust report.

``trust_report`` takes the divergences produced across a fleet at one anchor date
and orders them worst-first — ``MAJOR`` before ``MINOR`` before ``IN_SYNC``, and
within a severity band by descending ``worst_gap`` — so the operator dashboard
leads with the tenant most likely to be silently broken. It also tallies a count
per severity. The result is immutable and has a JSON-safe ``to_dict`` for the
dashboard seam. Pure and deterministic: sorting is total (severity rank, then gap
magnitude), so the same set of divergences always renders in the same order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence

from .divergence import Divergence, Severity

# Worst-first ordering: MAJOR leads, IN_SYNC trails.
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.MAJOR: 0,
    Severity.MINOR: 1,
    Severity.IN_SYNC: 2,
}


@dataclass(frozen=True, slots=True)
class TrustReport:
    """A fleet trust snapshot: rows worst-first plus a count per severity."""

    as_of: date | None
    rows: tuple[Divergence, ...]
    counts: dict[Severity, int]

    def to_dict(self) -> dict[str, object]:
        """A JSON-safe view of the whole report for the operator dashboard."""
        return {
            "as_of": self.as_of.isoformat() if self.as_of is not None else None,
            "counts": {sev.value: self.counts[sev] for sev in Severity},
            "rows": [row.to_dict() for row in self.rows],
        }


def trust_report(divergences: Iterable[Divergence]) -> TrustReport:
    """Sort divergences worst-first and tally counts by severity.

    The anchor date is taken as the latest ``as_of`` among the rows (they are
    expected to share one), or ``None`` when there are no divergences.
    """
    rows: Sequence[Divergence] = list(divergences)

    ordered = tuple(
        sorted(
            rows,
            key=lambda d: (_SEVERITY_RANK[d.severity], -d.worst_gap.minor_units),
        )
    )

    counts: dict[Severity, int] = {sev: 0 for sev in Severity}
    for d in rows:
        counts[d.severity] += 1

    as_of = max((d.as_of for d in rows), default=None)

    return TrustReport(as_of=as_of, rows=ordered, counts=counts)
