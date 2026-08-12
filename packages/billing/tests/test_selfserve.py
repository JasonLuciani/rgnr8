"""Self-serve signup → checkout → provisioning flow."""

import hashlib
import hmac
import json
from typing import Any

from rgnr8_billing import (
    AccountStatus,
    BillingService,
    CheckoutCompleted,
    FakeBillingProvider,
    FakeCheckoutProvider,
    HttpResponse,
    InMemoryAccountStore,
    SelfServeSignup,
    StripeCheckoutProvider,
    Tier,
    verify_hmac_sha256,
)

NOW = 1_760_000_000


def _svc(provider: FakeBillingProvider | None = None) -> BillingService:
    return BillingService(InMemoryAccountStore(), provider or FakeBillingProvider(),
                          clock=lambda: NOW)


def _signup(
    svc: BillingService,
    checkout: FakeCheckoutProvider,
    on_provisioned: Any = None,
) -> SelfServeSignup:
    return SelfServeSignup(
        svc, checkout,
        success_url="https://app.rgnr8.com/welcome",
        cancel_url="https://app.rgnr8.com/pricing",
        account_id_factory=(lambda: "acct_1"),
        on_provisioned=on_provisioned,
    )


def _completed_payload(
    *, account_id: str = "acct_1", tier: str = "assisted",
    customer: str = "cus_live_1", subscription: str = "sub_live_1",
    session_id: str = "cs_live_1", event_type: str = "checkout.session.completed",
) -> dict[str, Any]:
    return {
        "type": event_type,
        "data": {
            "object": {
                "id": session_id,
                "client_reference_id": account_id,
                "customer": customer,
                "subscription": subscription,
                "metadata": {"account_id": account_id, "tier": tier},
            }
        },
    }


def test_start_checkout_creates_trialing_account_and_session() -> None:
    svc = _svc()
    checkout = FakeCheckoutProvider()
    signup = _signup(svc, checkout)
    account, session = signup.start_checkout("owner@northwind.com", Tier.ASSISTED)

    assert account.status == AccountStatus.TRIALING
    assert account.id == "acct_1"
    assert account.trial_end == NOW + 14 * 86_400
    # session points the browser at the right redirect for this account/tier
    assert session.id == "cs_fake_acct_1_assisted"
    assert session.url == "https://checkout.test/session/cs_fake_acct_1_assisted"
    # the checkout call carried the account as client_reference_id and the urls
    call = checkout.calls[0]
    assert call["account_id"] == "acct_1"
    assert call["tier"] == "assisted"
    assert call["success_url"] == "https://app.rgnr8.com/welcome"
    assert call["customer_email"] == "owner@northwind.com"


def test_stripe_checkout_provider_posts_correct_shape() -> None:
    class RecordingHttp:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def post_form(
            self, url: str, data: dict[str, str], headers: dict[str, str],
        ) -> HttpResponse:
            assert headers["Authorization"].startswith("Bearer ")
            self.calls.append((url, data))
            return HttpResponse(200, json.dumps({"id": "cs_live_9", "url": "https://pay/x"}))

    http = RecordingHttp()
    prov = StripeCheckoutProvider(
        http, secret_key="sk_test_x",
        price_ids={"assisted": "price_assisted", "self_serve": "price_ss"})
    session = prov.create_checkout_session(
        account_id="acct_1", tier=Tier.ASSISTED,
        success_url="https://ok", cancel_url="https://no",
        customer_email="o@n.com")

    # id/url read back from the response body, no network
    assert session.id == "cs_live_9"
    assert session.url == "https://pay/x"
    url, data = http.calls[0]
    assert url.endswith("/v1/checkout/sessions")
    assert data["mode"] == "subscription"
    assert data["line_items[0][price]"] == "price_assisted"   # the tier's price id
    assert data["line_items[0][quantity]"] == "1"
    assert data["client_reference_id"] == "acct_1"
    assert data["metadata[account_id]"] == "acct_1"
    assert data["metadata[tier]"] == "assisted"
    assert data["success_url"] == "https://ok"
    assert data["cancel_url"] == "https://no"


def test_completed_event_activates_and_stamps_ids() -> None:
    svc = _svc()
    checkout = FakeCheckoutProvider()
    signup = _signup(svc, checkout)
    signup.start_checkout("owner@northwind.com", Tier.ASSISTED)

    acct = signup.complete_checkout(_completed_payload())
    assert acct is not None
    assert acct.status == AccountStatus.ACTIVE
    assert acct.stripe_customer_id == "cus_live_1"
    assert acct.stripe_subscription_id == "sub_live_1"
    # persisted, not just returned
    stored = svc.get_account("acct_1")
    assert stored is not None and stored.status == AccountStatus.ACTIVE


def test_completing_twice_is_idempotent() -> None:
    svc = _svc()
    checkout = FakeCheckoutProvider()
    hits: list[tuple[str, str]] = []
    signup = _signup(svc, checkout, on_provisioned=lambda aid, hint: hits.append((aid, hint)))
    signup.start_checkout("owner@northwind.com", Tier.ASSISTED)

    a1 = signup.complete_checkout(_completed_payload())
    a2 = signup.complete_checkout(_completed_payload())  # replay
    assert a1 is not None and a2 is not None
    assert a1.status == a2.status == AccountStatus.ACTIVE
    # provisioning hook fired exactly once, with account id + tenant hint (email)
    assert hits == [("acct_1", "owner@northwind.com")]


def test_foreign_and_bad_payloads_are_ignored() -> None:
    svc = _svc()
    checkout = FakeCheckoutProvider()
    calls: list[tuple[str, str]] = []
    signup = _signup(svc, checkout, on_provisioned=lambda aid, hint: calls.append((aid, hint)))
    signup.start_checkout("owner@northwind.com", Tier.ASSISTED)

    # wrong event type
    assert signup.complete_checkout(
        _completed_payload(event_type="invoice.paid")) is None
    # unknown account
    assert signup.complete_checkout(
        _completed_payload(account_id="acct_other")) is None
    # structurally broken payload
    assert signup.complete_checkout({"type": "checkout.session.completed"}) is None
    # unknown tier value
    assert signup.complete_checkout(_completed_payload(tier="platinum")) is None

    # none of these provisioned, and the account is still trialing
    assert calls == []
    stored = svc.get_account("acct_1")
    assert stored is not None and stored.status == AccountStatus.TRIALING


def test_parse_completed_event_returns_facts() -> None:
    checkout = FakeCheckoutProvider()
    parsed = checkout.parse_completed_event(_completed_payload())
    assert parsed == CheckoutCompleted(
        account_id="acct_1", tier=Tier.ASSISTED,
        customer_id="cus_live_1", subscription_id="sub_live_1",
        session_id="cs_live_1")


def test_webhook_signature_accepts_valid_rejects_tampered() -> None:
    secret = "whsec_test"
    body = json.dumps(_completed_payload(), sort_keys=True)
    good = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()

    prov = StripeCheckoutProvider(
        _NoHttp(), secret_key="sk_test_x", price_ids={}, signing_secret=secret)

    assert prov.verify_signature(body, good) is True
    # a valid hmac accepted even with the sha256= prefix stripped
    assert prov.verify_signature(body, "sha256=" + good, prefix="sha256=") is True
    # tampered body → rejected
    assert prov.verify_signature(body + " ", good) is False
    # tampered signature → rejected
    assert prov.verify_signature(body, good[:-1] + ("0" if good[-1] != "0" else "1")) is False
    # malformed (wrong length) signature is safe — no raise, just False
    assert prov.verify_signature(body, "not-a-real-hex") is False
    # no signing secret configured → cannot verify
    prov_nosecret = StripeCheckoutProvider(_NoHttp(), secret_key="sk", price_ids={})
    assert prov_nosecret.verify_signature(body, good) is False


def test_verify_hmac_sha256_standalone() -> None:
    secret = "s3cret"
    payload = "hello.world"
    good = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    assert verify_hmac_sha256(payload, good, secret) is True
    assert verify_hmac_sha256(payload, good, "wrong") is False
    assert verify_hmac_sha256(payload, "sha256=" + good, secret, prefix="sha256=") is True


class _NoHttp:
    """An HttpClient that never expects to be called (signature-only tests)."""

    def post_form(self, url: str, data: dict[str, str], headers: dict[str, str]) -> HttpResponse:
        raise AssertionError("no HTTP call expected in this test")
