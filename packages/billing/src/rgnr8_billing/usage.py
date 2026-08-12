"""Usage metering — the events that drive metered billing.

The pricing thesis is that we bill bundled human-review load, so the meter that
matters most is **analyst minutes**. We also record briefings sent, API calls,
and active tenants, so the operator can see load per account and so overage rolls
up cleanly at period close. Events are append-only and stamped with the period
they belong to; a `UsageSummary` folds a period's events into the totals the
invoice needs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum


class UsageKind(str, Enum):
    ANALYST_MINUTES = "analyst_minutes"  # human review time — the metered driver
    BRIEFING_SENT = "briefing_sent"
    API_CALL = "api_call"
    ACTIVE_TENANT = "active_tenant"


@dataclass(frozen=True, slots=True)
class UsageEvent:
    account_id: str
    kind: UsageKind
    quantity: int
    period: str          # "YYYY-MM" the usage is billed in
    tenant_id: str = ""  # which business incurred it (blank = account-level)
    at: int = 0          # epoch seconds
    note: str = ""


@dataclass(frozen=True, slots=True)
class UsageSummary:
    account_id: str
    period: str
    analyst_minutes: int = 0
    briefings_sent: int = 0
    api_calls: int = 0
    active_tenants: int = 0

    @staticmethod
    def summarize(account_id: str, period: str, events: Iterable[UsageEvent]) -> "UsageSummary":
        totals = {k: 0 for k in UsageKind}
        for e in events:
            if e.account_id != account_id or e.period != period:
                continue
            totals[e.kind] += e.quantity
        return UsageSummary(
            account_id=account_id,
            period=period,
            analyst_minutes=totals[UsageKind.ANALYST_MINUTES],
            briefings_sent=totals[UsageKind.BRIEFING_SENT],
            api_calls=totals[UsageKind.API_CALL],
            active_tenants=totals[UsageKind.ACTIVE_TENANT],
        )
