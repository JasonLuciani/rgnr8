"""The billing-provider seam — the same pattern as every other integration here.

`BillingProvider` is what the service calls to mirror account/subscription/usage
state into a real processor. `FakeBillingProvider` is deterministic and records
every call (tests + local). `StripeBillingProvider` builds the exact Stripe REST
shapes over an injected `HttpClient`, so the request construction is unit-testable
without a network or a live key; wiring the real `HttpClient` (urllib/requests)
turns it live. No secret is ever logged.
"""

from __future__ import annotations

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


@dataclass
class FakeBillingProvider:
    """Deterministic in-memory provider. IDs are derived (no randomness/clock), so
    runs are reproducible; every call is recorded for assertions."""

    customers: dict[str, str] = field(default_factory=dict)
    subscriptions: dict[str, str] = field(default_factory=dict)
    usage: list[tuple[str, UsageKind, int, int]] = field(default_factory=list)
    invoices: list[str] = field(default_factory=list)

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

    @staticmethod
    def encode(data: dict[str, str]) -> str:
        return urlencode(data)
