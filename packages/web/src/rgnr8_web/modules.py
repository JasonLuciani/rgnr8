"""Optional modules: on only for the businesses that use them.

Every business saw every screen: estimates, job costing, a sales pipeline,
loans, fixed assets and webhooks, twenty-two nav items for a service business
that uses a handful. Each of these is now a per-business switch in Settings,
off until someone turns it on. Turning one off only takes it out of the
sidebar: its pages and its data stay where they were.
"""

from __future__ import annotations

# nav suffix -> (label, what it is for, as the Settings page says it)
OPTIONAL_MODULES: dict[str, tuple[str, str]] = {
    "estimates": ("Estimates", "Quotes you send before the work, turned into invoices when won."),
    "jobs": ("Projects", "Job costing: budgets, time and materials, progress billing."),
    "pipeline": ("Sales pipeline", "Deals you're chasing, weighted into the cash outlook."),
    "debt": ("Loans", "Loans and lines of credit, with their payment schedules."),
    "assets": ("Fixed assets", "Equipment and vehicles, with depreciation."),
    "integrations": ("Integrations", "Send events to other systems (webhooks)."),
}


def capability(module: str) -> str:
    """How an enabled module appears in the sidebar's set of capabilities."""
    return f"module:{module}"


def enabled(saved: "list[str] | None") -> frozenset[str]:
    """The modules a business has switched on; unknown names are ignored."""
    return frozenset(m for m in (saved or []) if m in OPTIONAL_MODULES)
