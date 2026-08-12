"""Plan-change records — the audit shape for self-serve upgrades/downgrades.

`BillingService.change_plan` classifies a tier move as an upgrade, downgrade, or
lateral by comparing plan `base_price`, records a `PlanChange`, and (optionally)
fans it out to an injected `on_plan_change` seam so ops/analytics can react
without billing importing them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .plans import Plan, Tier


class PlanChangeKind(str, Enum):
    UPGRADE = "upgrade"      # moving to a higher-priced plan
    DOWNGRADE = "downgrade"  # moving to a lower-priced plan (guardrailed)
    LATERAL = "lateral"      # same base price (no up/down direction)


@dataclass(frozen=True, slots=True)
class PlanChange:
    """One tier transition on an account, with the direction already classified."""

    account_id: str
    from_tier: Tier
    to_tier: Tier
    kind: PlanChangeKind
    at: int  # epoch seconds


def classify_change(from_plan: Plan, to_plan: Plan) -> PlanChangeKind:
    """Direction of a tier move, by base price. `Money` is orderable within a
    currency, and every plan is priced in USD."""
    if to_plan.base_price > from_plan.base_price:
        return PlanChangeKind.UPGRADE
    if to_plan.base_price < from_plan.base_price:
        return PlanChangeKind.DOWNGRADE
    return PlanChangeKind.LATERAL
