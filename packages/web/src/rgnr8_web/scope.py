"""The v1 production scope — which surfaces are ready to be trusted, and which
are provisional.

The external review's headline was that the domain engine is strong but
*everything at the trust boundaries* is immature. So v1 started deliberately
narrow — **cash clarity, guided close, and the accountant handoff** — and the
broader accounting features (AR/AP, jobs, estimates, payroll, …) were labelled
**provisional** so an owner never mistook an immature screen for a system of
record. Track C then graduated each of those surfaces through an explicit gate
(see `GRADUATION_CHECKLIST` and `RGNR8-v1-scope.md`) as its inputs, provenance
labels, and authoritative write path were verified with tests. As of 2026-08-21
all core surfaces have graduated.

This module remains the single source of truth for that line: the shell badges
any provisional nav item and banners any provisional screen, and a deployment can
hide provisional surfaces entirely via a feature flag — so a *future* surface can
ship provisional and graduate the same way, even though no current surface is.
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
    # Graduated 2026-08-21 (C-2…C-7): the remaining core surfaces. Each reads
    # through the tenant-isolated ledger, gates writes on RBAC + POST_JOURNAL so
    # every change posts through the authoritative ledger (no in-memory state), and
    # labels its figures with provenance. See RGNR8-v1-scope.md for the per-surface
    # graduation record.
    "invoices",    # C-2 · AR
    "bills",       # C-3 · AP
    "capture",     # C-4 · receipt capture (imported → posted)
    "payroll",     # C-5 · payroll runs (posted → sealed)
    "jobs",        # C-6 · job costing (posted → reconciled)
    "estimates",   # C-6 · estimates (forecast → posted)
    "pipeline",    # C-6 · sales pipeline (forecast)
    "debt",        # C-7 · loans (posted → reconciled)
    "assets",      # C-7 · fixed assets (posted)
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
