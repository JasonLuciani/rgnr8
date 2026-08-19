"""Payments (Stripe) — a pay link on an invoice, and a received payment posted to AR.

`PaymentProvider` is the seam: create a hosted payment link for an invoice, and
parse an inbound webhook into a `PaymentEvent`. `StripePaymentProvider` builds
the Checkout-session request over the shared HTTP client; it is shape-correct and
needs a secret key + a real client to go live. `to_ar_payment` maps a received
payment into the AR payment path, keyed by the provider event id so a re-delivered
webhook records the payment once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .http import HttpClient


@dataclass(frozen=True)
class PaymentEvent:
    event_id: str
    invoice_ref: str
    amount_minor: int
    currency: str
    created: str  # YYYY-MM-DD


class PaymentProvider(Protocol):
    def create_payment_link(
        self, invoice_ref: str, amount_minor: int, currency: str
    ) -> str: ...

    def parse_webhook(self, payload: dict[str, object]) -> PaymentEvent: ...


class StripePaymentProvider:
    """Shape-correct Stripe Checkout adapter. Needs a secret key and a real HTTP
    client to go live; the request-building and webhook-parsing are here."""

    def __init__(
        self, http: HttpClient, secret_key: str, *, base_url: str = "https://api.stripe.com"
    ) -> None:
        self._http = http
        self._key = secret_key
        self._base = base_url.rstrip("/")

    def create_payment_link(self, invoice_ref: str, amount_minor: int, currency: str) -> str:
        body: dict[str, object] = {
            "mode": "payment",
            "client_reference_id": invoice_ref,
            "line_items[0][price_data][currency]": currency.lower(),
            "line_items[0][price_data][unit_amount]": amount_minor,
            "line_items[0][price_data][product_data][name]": f"Invoice {invoice_ref}",
            "line_items[0][quantity]": 1,
        }
        resp = self._http.post_json(
            f"{self._base}/v1/checkout/sessions",
            body,
            {"Authorization": f"Bearer {self._key}"},
        )
        return str(resp.get("url", ""))

    def parse_webhook(self, payload: dict[str, object]) -> PaymentEvent:
        obj = payload.get("data", {})
        obj = obj.get("object", {}) if isinstance(obj, dict) else {}
        obj = obj if isinstance(obj, dict) else {}
        return PaymentEvent(
            event_id=str(payload.get("id", "")),
            invoice_ref=str(obj.get("client_reference_id", "")),
            amount_minor=int(obj.get("amount_total", 0) or 0),
            currency=str(obj.get("currency", "usd")).upper(),
            created=str(obj.get("created_date", "")),
        )


class FakePaymentProvider:
    def __init__(self, link: str = "https://pay.example/abc") -> None:
        self._link = link
        self.created: list[tuple[str, int, str]] = []

    def create_payment_link(self, invoice_ref: str, amount_minor: int, currency: str) -> str:
        self.created.append((invoice_ref, amount_minor, currency))
        return self._link

    def parse_webhook(self, payload: dict[str, object]) -> PaymentEvent:
        return PaymentEvent(
            event_id=str(payload["event_id"]),
            invoice_ref=str(payload["invoice_ref"]),
            amount_minor=int(str(payload["amount_minor"])),
            currency=str(payload.get("currency", "USD")),
            created=str(payload.get("created", "")),
        )


def to_ar_payment(event: PaymentEvent, *, deposit_to_code: str = "1000") -> dict[str, object]:
    """Map a received payment to the AR payment path, idempotent by event id."""
    return {
        "idempotency_key": f"stripe:{event.event_id}",
        "invoice_id": event.invoice_ref,
        "date": event.created,
        "amount_minor": str(event.amount_minor),
        "deposit_to_code": deposit_to_code,
    }


__all__ = [
    "PaymentEvent",
    "PaymentProvider",
    "StripePaymentProvider",
    "FakePaymentProvider",
    "to_ar_payment",
]
