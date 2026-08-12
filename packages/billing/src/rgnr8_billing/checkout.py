"""The self-serve checkout seam — Stripe Checkout Sessions over the same shapes.

`CheckoutProvider` is what the signup flow calls to (a) open a hosted Checkout
Session for a tier and (b) turn an inbound ``checkout.session.completed`` webhook
back into the account/subscription facts we provision on. `FakeCheckoutProvider`
is deterministic and records every call (tests + local). `StripeCheckoutProvider`
builds the exact Stripe ``POST /v1/checkout/sessions`` form over an injected
`HttpClient`, so request construction is unit-testable without a network or a live
key; it also carries a webhook-signature verify seam (constant-time HMAC-SHA256,
the same way the connectors package verifies inbound webhooks). Price ids per tier
are injected, never hard-coded. No secret is ever logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from .plans import Tier
from .provider import HttpClient


@dataclass(frozen=True, slots=True)
class CheckoutSession:
    """A started hosted-checkout session: an id plus the redirect the browser
    should be sent to."""

    id: str
    url: str


@dataclass(frozen=True, slots=True)
class CheckoutCompleted:
    """The facts distilled from a ``checkout.session.completed`` webhook — enough
    to provision the account onto its paid subscription."""

    account_id: str
    tier: Tier
    customer_id: str
    subscription_id: str
    session_id: str = ""


class CheckoutProvider(Protocol):
    def create_checkout_session(
        self,
        *,
        account_id: str,
        tier: Tier,
        success_url: str,
        cancel_url: str,
        customer_email: str,
    ) -> CheckoutSession: ...

    def parse_completed_event(self, payload: Mapping[str, object]) -> CheckoutCompleted | None: ...


# --- signature verification --------------------------------------------------

def verify_hmac_sha256(payload: str, signature: str, secret: str, *, prefix: str = "") -> bool:
    """Verify an HMAC-SHA256 hex signature over ``payload``. Constant-time via
    ``hmac.compare_digest`` and never raises on a malformed/wrong-length
    signature — it simply returns False — so it is safe on untrusted input. An
    optional header ``prefix`` (e.g. ``"sha256="``) is stripped before comparing.
    """
    provided = signature
    if prefix and provided.startswith(prefix):
        provided = provided[len(prefix):]
    expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    # compare_digest is constant-time and length-safe; it handles the
    # unequal-length case internally without leaking the bytes.
    return hmac.compare_digest(provided, expected)


# --- shared payload parsing --------------------------------------------------

def _str(m: Mapping[str, object], key: str) -> str:
    v = m.get(key)
    return v if isinstance(v, str) else ""


def _parse_completed(payload: Mapping[str, object]) -> CheckoutCompleted | None:
    """Map a Stripe event envelope → CheckoutCompleted, or None if it isn't a
    completed-checkout event we can provision from. Defensive: any missing/foreign
    field yields None rather than a partial record."""
    if _str(payload, "type") != "checkout.session.completed":
        return None
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    obj = data.get("object")
    if not isinstance(obj, Mapping):
        return None
    metadata = obj.get("metadata")
    meta: Mapping[str, object] = metadata if isinstance(metadata, Mapping) else {}
    # account id: client_reference_id is authoritative; metadata is the fallback.
    account_id = _str(obj, "client_reference_id") or _str(meta, "account_id")
    tier_value = _str(meta, "tier")
    customer_id = _str(obj, "customer")
    subscription_id = _str(obj, "subscription")
    if not account_id or not tier_value or not subscription_id:
        return None
    try:
        tier = Tier(tier_value)
    except ValueError:
        return None
    return CheckoutCompleted(
        account_id=account_id,
        tier=tier,
        customer_id=customer_id,
        subscription_id=subscription_id,
        session_id=_str(obj, "id"),
    )


# --- fake --------------------------------------------------------------------

@dataclass
class FakeCheckoutProvider:
    """Deterministic in-memory checkout provider. Session ids are derived (no
    randomness/clock) so runs are reproducible; every create call is recorded."""

    sessions: list[CheckoutSession] = field(default_factory=list)
    calls: list[dict[str, str]] = field(default_factory=list)
    base_url: str = "https://checkout.test/session"

    def create_checkout_session(
        self,
        *,
        account_id: str,
        tier: Tier,
        success_url: str,
        cancel_url: str,
        customer_email: str,
    ) -> CheckoutSession:
        sid = f"cs_fake_{account_id}_{tier.value}"
        session = CheckoutSession(id=sid, url=f"{self.base_url}/{sid}")
        self.calls.append({
            "account_id": account_id,
            "tier": tier.value,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "customer_email": customer_email,
        })
        self.sessions.append(session)
        return session

    def parse_completed_event(self, payload: Mapping[str, object]) -> CheckoutCompleted | None:
        return _parse_completed(payload)


# --- stripe ------------------------------------------------------------------

class StripeCheckoutProvider:
    """Builds the real Stripe Checkout Session shape over an injected `HttpClient`.

    ``create_checkout_session`` issues ``POST /v1/checkout/sessions`` with
    ``mode=subscription``, one line item at the tier's price id, the account id as
    ``client_reference_id`` (and in metadata alongside the tier), and the
    success/cancel redirects — so the request construction is unit-testable with a
    recording HttpClient, no network. The session id/url are read back from the
    response body when present. Price ids per tier are injected.
    """

    _API = "https://api.stripe.com/v1"

    def __init__(
        self,
        http: HttpClient,
        *,
        secret_key: str,
        price_ids: dict[str, str],
        signing_secret: str = "",
    ) -> None:
        self._http = http
        self._key = secret_key
        self._price_ids = price_ids        # tier.value -> Stripe price id
        self._signing_secret = signing_secret

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}",
                "Content-Type": "application/x-www-form-urlencoded"}

    def create_checkout_session(
        self,
        *,
        account_id: str,
        tier: Tier,
        success_url: str,
        cancel_url: str,
        customer_email: str,
    ) -> CheckoutSession:
        price = self._price_ids.get(tier.value, "")
        data: dict[str, str] = {
            "mode": "subscription",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": account_id,
            "customer_email": customer_email,
            "line_items[0][price]": price,
            "line_items[0][quantity]": "1",
            "metadata[account_id]": account_id,
            "metadata[tier]": tier.value,
        }
        resp = self._http.post_form(f"{self._API}/checkout/sessions", data, self._headers())
        if not (200 <= resp.status < 300):
            raise RuntimeError(f"Stripe checkout/sessions failed: HTTP {resp.status}")
        sid, url = self._read_session(resp.body, account_id)
        return CheckoutSession(id=sid, url=url)

    @staticmethod
    def _read_session(body: str, account_id: str) -> tuple[str, str]:
        """Best-effort read of id/url from the response body, with deterministic
        fallbacks so the caller always gets a usable session even before a real
        Stripe response is wired in."""
        sid = f"cs_pending_{account_id}"
        url = ""
        try:
            parsed: object = json.loads(body)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, Mapping):
            if isinstance(parsed.get("id"), str):
                sid = str(parsed["id"])
            if isinstance(parsed.get("url"), str):
                url = str(parsed["url"])
        if not url:
            url = f"https://checkout.stripe.com/c/pay/{sid}"
        return sid, url

    def parse_completed_event(self, payload: Mapping[str, object]) -> CheckoutCompleted | None:
        return _parse_completed(payload)

    def verify_signature(self, payload: str, signature: str, *, prefix: str = "") -> bool:
        """Verify an inbound webhook signature against the injected signing secret
        (constant-time HMAC-SHA256). Returns False if no signing secret is set."""
        if not self._signing_secret:
            return False
        return verify_hmac_sha256(payload, signature, self._signing_secret, prefix=prefix)
