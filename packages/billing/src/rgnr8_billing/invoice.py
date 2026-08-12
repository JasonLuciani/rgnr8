"""Invoice construction — turn a plan + a period's usage into billable lines.

Deterministic and exact: every amount is integer minor units (never float), the
base subscription is one line, and metered overage (analyst minutes beyond the
bundle, businesses beyond the included count) adds further lines. This is the
authoritative computation; the provider seam then mirrors it into Stripe.
"""

from __future__ import annotations

from dataclasses import dataclass

from rgnr8_forecast import Money

from .accounts import Account
from .plans import Plan
from .usage import UsageSummary


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    description: str
    quantity: int
    unit_amount: Money
    amount: Money


@dataclass(frozen=True, slots=True)
class Invoice:
    account_id: str
    period: str
    currency: str
    lines: tuple[InvoiceLine, ...]
    subtotal: Money
    total: Money

    @property
    def line_count(self) -> int:
        return len(self.lines)


def build_invoice(
    account: Account,
    plan: Plan,
    usage: UsageSummary,
    *,
    tenant_count: int | None = None,
) -> Invoice:
    """Build the period invoice: base subscription + analyst-minute overage +
    extra-business overage. `tenant_count` defaults to the account's current
    businesses."""
    currency = plan.base_price.currency
    tenants = tenant_count if tenant_count is not None else account.tenant_count
    lines: list[InvoiceLine] = [
        InvoiceLine(f"{plan.name} plan — {account.name}", 1, plan.base_price, plan.base_price),
    ]

    over_minutes = max(0, usage.analyst_minutes - plan.included_analyst_minutes)
    if over_minutes > 0:
        amt = plan.analyst_minute_rate.scale_by(over_minutes, 1)
        lines.append(InvoiceLine(
            f"Analyst minutes over {plan.included_analyst_minutes} bundled",
            over_minutes, plan.analyst_minute_rate, amt))

    over_tenants = max(0, tenants - plan.included_tenants)
    if over_tenants > 0:
        amt = plan.extra_tenant_rate.scale_by(over_tenants, 1)
        lines.append(InvoiceLine(
            f"Additional businesses over {plan.included_tenants} included",
            over_tenants, plan.extra_tenant_rate, amt))

    subtotal = Money(0, currency)
    for ln in lines:
        subtotal = subtotal + ln.amount
    return Invoice(account.id, usage.period, currency, tuple(lines), subtotal, subtotal)
