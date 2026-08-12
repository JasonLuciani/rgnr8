"""Canonical product-event catalog — the acquisition→activation funnel.

Product analytics only works if everyone emits the *same* event names. Free-text
strings drift ("signup" vs "sign_up" vs "SignupComplete") and the funnel silently
splinters, so the catalog is a closed `EventName` str-Enum: the only events the
tracker will emit, and the only labels the funnel report reasons about. The values
are snake_case and stable — they are the wire contract with the downstream
analytics store, so renaming one is a breaking change, not a refactor.

The events trace the whole top-of-funnel journey — from the first anonymous
`signup_started` through billing (`checkout_*`), account/tenant provisioning
(`account_activated`, `tenant_onboarded`, `connector_connected`), first real value
(`first_forecast_viewed`, `briefing_delivered`, `cfo_question_asked`,
`close_sealed`) — plus the one churn signal we care about at this stage
(`subscription_canceled`).

A `ProductEvent` is the frozen record that carries one occurrence: *what* happened
(`name`), *when* (`at`, an injected epoch-seconds reading — never the wall clock),
*who* (`distinct_id`, the user/account identity the funnel groups by), the tenancy
it happened under (`tenant_id`, `account_id`), and a `props` bag of event-specific
detail. Frozen so a recorded event can't be mutated after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class EventName(str, Enum):
    """The closed set of product events RGNR8 emits, in funnel order."""

    SIGNUP_STARTED = "signup_started"
    SIGNUP_COMPLETED = "signup_completed"
    EMAIL_VERIFIED = "email_verified"
    CHECKOUT_STARTED = "checkout_started"
    CHECKOUT_COMPLETED = "checkout_completed"
    ACCOUNT_ACTIVATED = "account_activated"
    TENANT_ONBOARDED = "tenant_onboarded"
    CONNECTOR_CONNECTED = "connector_connected"
    FIRST_FORECAST_VIEWED = "first_forecast_viewed"
    BRIEFING_DELIVERED = "briefing_delivered"
    CFO_QUESTION_ASKED = "cfo_question_asked"
    CLOSE_SEALED = "close_sealed"
    SUBSCRIPTION_CANCELED = "subscription_canceled"

    def __str__(self) -> str:
        # Render as the bare wire value ("signup_started"), not "EventName.X".
        return self.value


@dataclass(frozen=True)
class ProductEvent:
    """One recorded product-event occurrence — immutable once captured.

    `name` is the catalog label; `at` is an injected epoch-seconds timestamp;
    `distinct_id` is the identity the funnel groups by (user or account);
    `tenant_id`/`account_id` scope it to the tenancy; `props` carries any
    event-specific detail. `props` defaults to an empty dict via a factory so
    every instance owns its own mapping.
    """

    name: EventName
    at: float
    distinct_id: str
    tenant_id: str = ""
    account_id: str = ""
    props: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Flat, JSON-serializable view for a capture-API payload.

        The event value is emitted as its bare string so the payload is stable
        regardless of the Enum member's repr.
        """
        return {
            "event": str(self.name),
            "timestamp": self.at,
            "distinct_id": self.distinct_id,
            "tenant_id": self.tenant_id,
            "account_id": self.account_id,
            "properties": dict(self.props),
        }
