"""Plans, tiers, and entitlements.

Pricing tracks bundled human-review load, not features (the pricing thesis): the
three tiers differ mostly in how many **analyst minutes** and **client
businesses** they bundle, and in a couple of capability flags (multi-tenant for an
accounting firm, an external accountant seat, API access). A `Plan` is the static
definition; an account carries a `Tier`, and the plan's numbers drive both
entitlement checks and metered overage on the invoice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rgnr8_forecast import Money


class Tier(str, Enum):
    SELF_SERVE = "self_serve"    # Tier 1 — software-only
    ASSISTED = "assisted"        # Tier 2 — assisted review
    CO_DELIVERY = "co_delivery"  # Tier 3 — accountant co-delivery (firm)


class Feature(str, Enum):
    CASH = "cash"
    BRIEFING = "briefing"
    REGISTER = "register"
    CLOSE = "close"
    PACKAGE = "package"
    ASK_CFO = "ask_cfo"
    MULTI_TENANT = "multi_tenant"          # one account owns many businesses
    ACCOUNTANT_SEAT = "accountant_seat"    # external CPA co-delivery
    API_ACCESS = "api_access"              # partner/API keys + webhooks out
    PRIORITY_SUPPORT = "priority_support"


# every paid tier includes the full owner product; tiers differ on bundled human
# review + multi-business/API capability, not on the core screens.
_CORE = frozenset({
    Feature.CASH, Feature.BRIEFING, Feature.REGISTER,
    Feature.CLOSE, Feature.PACKAGE, Feature.ASK_CFO,
})


def _usd(s: str) -> Money:
    return Money.from_decimal(s)


@dataclass(frozen=True, slots=True)
class Plan:
    tier: Tier
    name: str
    base_price: Money             # per billing period (monthly)
    included_tenants: int         # businesses bundled in the base price
    included_seats: int           # user seats bundled
    included_analyst_minutes: int  # bundled human-review minutes per period
    analyst_minute_rate: Money    # overage price per analyst-minute beyond included
    extra_tenant_rate: Money      # price per business beyond included_tenants
    features: frozenset[Feature]

    def has(self, feature: Feature) -> bool:
        return feature in self.features


PLANS: dict[Tier, Plan] = {
    Tier.SELF_SERVE: Plan(
        tier=Tier.SELF_SERVE, name="Self-serve",
        base_price=_usd("199.00"), included_tenants=1, included_seats=3,
        included_analyst_minutes=0, analyst_minute_rate=_usd("3.00"),
        extra_tenant_rate=_usd("149.00"), features=_CORE,
    ),
    Tier.ASSISTED: Plan(
        tier=Tier.ASSISTED, name="Assisted",
        base_price=_usd("499.00"), included_tenants=1, included_seats=5,
        included_analyst_minutes=120, analyst_minute_rate=_usd("2.50"),
        extra_tenant_rate=_usd("199.00"), features=_CORE | {Feature.PRIORITY_SUPPORT},
    ),
    Tier.CO_DELIVERY: Plan(
        tier=Tier.CO_DELIVERY, name="Co-delivery",
        base_price=_usd("999.00"), included_tenants=5, included_seats=10,
        included_analyst_minutes=600, analyst_minute_rate=_usd("2.00"),
        extra_tenant_rate=_usd("149.00"),
        features=_CORE | {Feature.MULTI_TENANT, Feature.ACCOUNTANT_SEAT,
                          Feature.API_ACCESS, Feature.PRIORITY_SUPPORT},
    ),
}


def plan_for(tier: Tier) -> Plan:
    return PLANS[tier]
