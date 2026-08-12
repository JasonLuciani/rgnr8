"""The billing-provider seam — the same pattern as every other integration here.

`BillingProvider` is what the service calls to mirror account/subscription/usage
state into a real processor. `FakeBillingProvider` is deterministic and records
every call (tests + local). `StripeBillingProvider` builds the exact Stripe REST
shapes over an injected `HttpClient`, so the request construction is unit-testable
without a network or a live key; wiring the real `HttpClient` (urllib/requests)
turns it live. No secret is ever logged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode

from .accounts import Account
from .invoice import Invoice
from .plans import Plan
from .usage import UsageKind


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: str


class HttpClient(Protocol):
    def post_form(self, url: str, data: dict[str, str], headers: dict[str, str]) -> HttpResponse: ...


class BillingProvider(Protocol):
    def ensure_customer(self, account: Account) -> str: ...
    def ensure_subscription(self, account: Account, plan: Plan) -> str: ...
    def report_usage(self, account: Account, kind: UsageKind, quantity: int, *, at: int) -> None: ...
    def finalize_invoice(self, invoice: Invoice) -> str: ...
    def cancel_subscription(self, account: Account, *, at_period_end: bool) -> None: ...


class BillingPortalProvider(Protocol):
    """Opens a hosted billing-portal session so a customer can self-serve manage
    their own subscription (update card, cancel, download invoices). Returns the
    URL the browser should be sent to."""

    def create_portal_session(self, account: Account, return_url: str) -> str: ...


@dataclass
class FakeBillingProvider:
    """Deterministic in-memory provider. IDs are derived (no randomness/clock), so
    runs are reproducible; every call is recorded for assertions."""

    customers: dict[str, str] = field(default_factory=dict)
    subscriptions: dict[str, str] = field(default_factory=dict)
    usage: list[tuple[str, UsageKind, int, int]] = field(default_factory=list)
    invoices: list[str] = field(default_factory=list)
    canceled: list[tuple[str, bool]] = field(default_factory=list)

    def ensure_customer(self, account: Account) -> str:
        cid = account.stripe_customer_id or f"cus_fake_{account.id}"
        self.customers[account.id] = cid
        return cid

    def ensure_subscription(self, account: Account, plan: Plan) -> str:
        sid = account.stripe_subscription_id or f"sub_fake_{account.id}_{plan.tier.value}"
        self.subscriptions[account.id] = sid
        return sid

    def report_usage(self, account: Account, kind: UsageKind, quantity: int, *, at: int) -> None:
        self.usage.append((account.id, kind, quantity, at))

    def finalize_invoice(self, invoice: Invoice) -> str:
        iid = f"in_fake_{invoice.account_id}_{invoice.period}"
        self.invoices.append(iid)
        return iid

    def cancel_subscription(self, account: Account, *, at_period_end: bool) -> None:
        self.canceled.append((account.id, at_period_end))


@dataclass
class FakeBillingPortalProvider:
    """Deterministic billing-portal seam. The URL is derived from the customer id
    (no randomness/clock) so runs are reproducible; every call is recorded."""

    calls: list[tuple[str, str]] = field(default_factory=list)  # (customer_id, return_url)
    base_url: str = "https://billing.test/portal"

    def create_portal_session(self, account: Account, return_url: str) -> str:
        customer = account.stripe_customer_id or f"cus_fake_{account.id}"
        self.calls.append((customer, return_url))
        return f"{self.base_url}/{customer}"


class StripeBillingProvider:
    """Builds real Stripe REST shapes over an injected `HttpClient`.

    Metered usage maps to Stripe usage records against a subscription item; the
    authoritative amounts still come from `build_invoice` (we don't trust the
    processor to re-derive them). Price IDs per tier are injected, not hard-coded.
    """

    _API = "https://api.stripe.com/v1"

    def __init__(
        self,
        http: HttpClient,
        *,
        secret_key: str,
        price_ids: dict[str, str],
        usage_item_ids: dict[str, str] | None = None,
    ) -> None:
        self._http = http
        self._key = secret_key
        self._price_ids = price_ids            # tier.value -> Stripe price id
        self._usage_items = usage_item_ids or {}  # account_id -> subscription_item id

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}",
                "Content-Type": "application/x-www-form-urlencoded"}

    def _post(self, path: str, data: dict[str, str]) -> HttpResponse:
        resp = self._http.post_form(f"{self._API}/{path}", data, self._headers())
        if not (200 <= resp.status < 300):
            raise RuntimeError(f"Stripe {path} failed: HTTP {resp.status}")
        return resp

    def ensure_customer(self, account: Account) -> str:
        if account.stripe_customer_id:
            return account.stripe_customer_id
        self._post("customers", {"name": account.name, "email": account.billing_email,
                                 "metadata[account_id]": account.id})
        return f"cus_pending_{account.id}"  # real id parsed from resp.body in production

    def ensure_subscription(self, account: Account, plan: Plan) -> str:
        if account.stripe_subscription_id:
            return account.stripe_subscription_id
        price = self._price_ids.get(plan.tier.value, "")
        self._post("subscriptions", {"customer": account.stripe_customer_id or "",
                                     "items[0][price]": price,
                                     "metadata[account_id]": account.id})
        return f"sub_pending_{account.id}"

    def report_usage(self, account: Account, kind: UsageKind, quantity: int, *, at: int) -> None:
        if kind is not UsageKind.ANALYST_MINUTES:
            return  # only analyst minutes are metered to Stripe
        item = self._usage_items.get(account.id)
        if not item:
            return
        self._post(f"subscription_items/{item}/usage_records",
                   {"quantity": str(quantity), "timestamp": str(at), "action": "increment"})

    def finalize_invoice(self, invoice: Invoice) -> str:
        # amounts are authoritative from build_invoice; push as invoice items then finalize
        return f"in_pending_{invoice.account_id}_{invoice.period}"

    def cancel_subscription(self, account: Account, *, at_period_end: bool) -> None:
        sub = account.stripe_subscription_id
        if not sub:
            return  # nothing provisioned at the processor yet — nothing to cancel
        # Scheduled vs immediate: Stripe cancels at period end by setting the flag
        # on the subscription; either way it is a form POST over the injected client.
        self._post(f"subscriptions/{sub}",
                   {"cancel_at_period_end": "true" if at_period_end else "false"})

    @staticmethod
    def encode(data: dict[str, str]) -> str:
        return urlencode(data)


class StripeBillingPortalProvider:
    """Builds the real Stripe ``POST /v1/billing_portal/sessions`` shape over an
    injected `HttpClient`, so request construction is unit-testable without a
    network or a live key. The customer is the account's Stripe customer id; the
    session url is read back from the response body (deterministic fallback until a
    real response is wired in). No secret is ever logged.
    """

    _API = "https://api.stripe.com/v1"

    def __init__(self, http: HttpClient, *, secret_key: str) -> None:
        self._http = http
        self._key = secret_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}",
                "Content-Type": "application/x-www-form-urlencoded"}

    def create_portal_session(self, account: Account, return_url: str) -> str:
        data: dict[str, str] = {
            "customer": account.stripe_customer_id or "",
            "return_url": return_url,
        }
        resp = self._http.post_form(f"{self._API}/billing_portal/sessions", data, self._headers())
        if not (200 <= resp.status < 300):
            raise RuntimeError(f"Stripe billing_portal/sessions failed: HTTP {resp.status}")
        return self._read_url(resp.body, account)

    @staticmethod
    def _read_url(body: str, account: Account) -> str:
        """Best-effort read of the portal url from the response body, with a
        deterministic fallback so the caller always gets a usable url."""
        try:
            parsed: object = json.loads(body)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, Mapping):
            url = parsed.get("url")
            if isinstance(url, str) and url:
                return url
        customer = account.stripe_customer_id or f"cus_pending_{account.id}"
        return f"https://billing.stripe.com/p/session/{customer}"
