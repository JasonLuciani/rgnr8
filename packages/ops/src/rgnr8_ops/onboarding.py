"""Onboarding metadata the operator captures per client: which chart-of-accounts
template to seed, and the cutover to RGNR8 as system of record.

The accounting engine that actually *builds* the chart and posts the opening
balances lives in the TypeScript core (`@rgnr8/ledger-kernel`
`buildChartForCategory` / `executeCutover`). This module is the Python
control-plane counterpart: it records the operator's choices so the TS core can
act on them, and tracks go-live status for the console. The category slugs here
are the cross-language contract — they match `ledger-kernel`'s `BusinessCategory`
enum values exactly, so a slug recorded here maps to a TS template one-to-one.
"""

from __future__ import annotations

from dataclasses import dataclass


# Slugs + human labels — MUST match ledger-kernel's BusinessCategory enum values.
BUSINESS_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("SERVICE_GENERAL", "General Service Business"),
    ("PROFESSIONAL_SERVICES", "Professional Services"),
    ("CONTRACTOR_TRADES", "Contractor & Trades"),
    ("RETAIL", "Retail"),
    ("ECOMMERCE", "E-commerce"),
    ("RESTAURANT", "Restaurant & Food Service"),
    ("REAL_ESTATE", "Real Estate"),
    ("HEALTHCARE_PRACTICE", "Healthcare Practice"),
    ("NONPROFIT", "Nonprofit"),
)

_CATEGORY_SLUGS = frozenset(slug for slug, _ in BUSINESS_CATEGORIES)

# Source systems we cut over from.
SOURCE_SYSTEMS = frozenset({"quickbooks", "xero", "other"})


def is_valid_category(slug: str) -> bool:
    return slug in _CATEGORY_SLUGS


def category_catalog() -> list[dict[str, str]]:
    return [{"slug": slug, "label": label} for slug, label in BUSINESS_CATEGORIES]


class OnboardingError(Exception):
    """An invalid onboarding choice (unknown category / source system)."""


@dataclass(frozen=True, slots=True)
class CutoverRecord:
    """A client's go-live: the moment RGNR8 became its system of record."""

    tenant_id: str
    source_system: str
    cutover_date: str  # ISO date
    marked_by: str
    marked_at: int  # epoch seconds (operator clock)
    opening_entry_id: str = ""  # set once the TS core posts the opening balances


class OnboardingRegistry:
    """Per-tenant onboarding state: chosen COA template + cutover status. A tiny
    in-memory store the operator surface reads/writes; production persists it."""

    def __init__(self) -> None:
        self._coa_category: dict[str, str] = {}
        self._cutover: dict[str, CutoverRecord] = {}

    # --- COA template choice --------------------------------------------------
    def set_coa_category(self, tenant_id: str, category: str) -> None:
        if not is_valid_category(category):
            raise OnboardingError(f"unknown COA category {category!r}")
        self._coa_category[tenant_id] = category

    def coa_category(self, tenant_id: str) -> str | None:
        return self._coa_category.get(tenant_id)

    # --- cutover / go-live ----------------------------------------------------
    def mark_cutover(
        self, tenant_id: str, source_system: str, cutover_date: str,
        *, marked_by: str, marked_at: int, opening_entry_id: str = "",
    ) -> CutoverRecord:
        if source_system not in SOURCE_SYSTEMS:
            raise OnboardingError(f"unknown source system {source_system!r}")
        rec = CutoverRecord(tenant_id, source_system, cutover_date, marked_by,
                            marked_at, opening_entry_id)
        self._cutover[tenant_id] = rec
        return rec

    def cutover(self, tenant_id: str) -> CutoverRecord | None:
        return self._cutover.get(tenant_id)

    def is_live(self, tenant_id: str) -> bool:
        """True once RGNR8 is the system of record for this tenant."""
        return tenant_id in self._cutover
