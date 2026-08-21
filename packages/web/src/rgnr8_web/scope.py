"""The v1 production scope — which surfaces are ready to be trusted, and which
are provisional.

The external review's headline was that the domain engine is strong but
*everything at the trust boundaries* is immature. Rather than ship the whole
surface as if it were production, v1 is deliberately narrow: **cash clarity,
guided close, and the accountant handoff.** Those surfaces are production-grade;
the broader accounting features (AR/AP, jobs, estimates, payroll, …) still work
but are labelled **provisional** so an owner never mistakes an immature screen
for a system of record.

This module is the single source of truth for that line. The shell badges
provisional nav items and banners provisional screens; a deployment can also
hide provisional surfaces entirely via a feature flag.
"""

from __future__ import annotations

# Nav suffixes (see shell._NAV) that are production-grade in v1. The empty
# string is the Cash home. Everything NOT in this set is provisional.
#
# v1 = cash clarity + guided close + accountant handoff:
#   - cash clarity:       Cash, Ask, Briefing, Scenarios, Receivables, Health, Reports
#   - guided close:       Books (statements), Close, Package
#   - accountant handoff: Connect, Integrations
#   - account admin (infra, always production): Team, Settings, Audit
V1_PRODUCTION: frozenset[str] = frozenset({
    "",
    "ask",
    "briefing",
    "scenarios",
    "receivables",
    "health",
    "reports",
    "books",
    "close",
    "packages",
    "connect",
    "integrations",
    "team",
    "settings",
    "audit",
    # Graduated 2026-08-20 (C-1): the bank feed / review inbox. Reads through the
    # tenant-isolated ledger, categorize/accept is RBAC-gated and posts through the
    # authoritative ledger, and its lines carry provenance labels (imported → posted
    # → reconciled). "transactions" is the nav item; "inbox" is the review screen.
    "transactions",
    "inbox",
})


def is_provisional(suffix: str) -> bool:
    """True when a nav suffix is a provisional (not-yet-production) surface."""
    return suffix not in V1_PRODUCTION


# --- graduation gate (C-0) ---------------------------------------------------
#: The checklist a provisional surface must pass to be added to V1_PRODUCTION.
#: Graduating a surface = satisfying all of these, then moving its nav suffix(es)
#: into `V1_PRODUCTION` and recording the graduation in RGNR8-v1-scope.md.
GRADUATION_CHECKLIST: tuple[str, ...] = (
    "Inputs are validated and tenant-isolated (no cross-tenant read/write).",
    "Every figure carries a provenance label (forecast/imported/posted/reconciled/sealed).",
    "Writes flow through the authoritative controls — no silent in-memory state.",
    "An acceptance test proves the above for this surface.",
    "Graduation is recorded (date + who) in RGNR8-v1-scope.md.",
)


#: One-line, in-app disclosure shown on every provisional screen.
PROVISIONAL_NOTE = (
    "This is a provisional feature — it works, but it is not yet production-grade "
    "and should not be relied on for decisions or filings. For numbers you can act "
    "on, use Cash, the Close, and your accountant package."
)
