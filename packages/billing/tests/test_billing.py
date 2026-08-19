"""Accounts, entitlements, metering, and period invoicing."""

import sqlite3
from typing import Any, cast

from rgnr8_billing import (
    AccountStatus,
    BillingService,
    EntitlementError,
    Entitlements,
    FakeBillingProvider,
    Feature,
    InMemoryAccountStore,
    SqlAccountStore,
    Tier,
    UsageKind,
    build_invoice,
    plan_for,
)
from rgnr8_forecast import Money

NOW = 1_760_000_000


def _svc(provider: FakeBillingProvider | None = None) -> BillingService:
    return BillingService(InMemoryAccountStore(), provider or FakeBillingProvider(),
                          clock=lambda: NOW)


def test_create_account_provisions_customer_and_trial() -> None:
    p = FakeBillingProvider()
    svc = _svc(p)
    a = svc.create_account("acct_1", "Northwind LLC", "owner@northwind.com", Tier.ASSISTED,
                           trial_days=14)
    assert a.status == AccountStatus.TRIALING
    assert a.trial_end == NOW + 14 * 86_400
    assert a.stripe_customer_id == "cus_fake_acct_1"
    assert p.customers["acct_1"] == "cus_fake_acct_1"


def test_activate_starts_subscription() -> None:
    p = FakeBillingProvider()
    svc = _svc(p)
    svc.create_account("acct_1", "N", "o@n.com", Tier.SELF_SERVE)
    a = svc.activate("acct_1")
    assert a.status == AccountStatus.ACTIVE
    assert a.stripe_subscription_id == "sub_fake_acct_1_self_serve"
    assert p.subscriptions["acct_1"].startswith("sub_fake_acct_1")


def test_self_serve_cannot_add_second_business() -> None:
    svc = _svc()
    svc.create_account("acct_1", "N", "o@n.com", Tier.SELF_SERVE)
    svc.attach_tenant("acct_1", "biz-1")
    try:
        svc.attach_tenant("acct_1", "biz-2")
        assert False, "expected EntitlementError"
    except EntitlementError:
        pass


def test_co_delivery_is_multi_tenant() -> None:
    svc = _svc()
    svc.create_account("firm", "CPA Firm", "ops@firm.com", Tier.CO_DELIVERY)
    for i in range(7):  # past the 5 included → allowed as overage
        svc.attach_tenant("firm", f"client-{i}")
    a = svc.get_account("firm")
    assert a is not None and a.tenant_count == 7
    assert Entitlements.of(a).feature_enabled(Feature.MULTI_TENANT)
    assert Entitlements.of(a).feature_enabled(Feature.API_ACCESS)


def test_feature_gates_by_tier_and_status() -> None:
    svc = _svc()
    svc.create_account("s", "S", "o@s.com", Tier.SELF_SERVE)
    assert svc.feature_enabled("s", Feature.CASH) is True
    assert svc.feature_enabled("s", Feature.API_ACCESS) is False   # self-serve lacks it
    svc.set_status("s", AccountStatus.PAUSED)
    assert svc.feature_enabled("s", Feature.CASH) is False          # paused loses access


def test_metering_and_invoice_with_overage() -> None:
    p = FakeBillingProvider()
    svc = _svc(p)
    svc.create_account("a", "Acme", "o@a.com", Tier.ASSISTED)  # 120 mins included
    svc.activate("a")
    svc.attach_tenant("a", "biz-1")
    svc.record_analyst_minutes("a", 100, "2026-08")
    svc.record_analyst_minutes("a", 80, "2026-08")  # total 180 → 60 over
    summary = svc.usage_summary("a", "2026-08")
    assert summary.analyst_minutes == 180
    # provider saw the metered usage
    assert [u for u in p.usage if u[1] == UsageKind.ANALYST_MINUTES]

    inv = svc.close_period("a", "2026-08")
    assert inv.currency == "USD"
    # base 499 + 60 min * 2.50 = 150.00 overage = 649.00
    assert inv.total == Money.from_decimal("649.00")
    descs = [ln.description for ln in inv.lines]
    assert any("Analyst minutes over 120" in d for d in descs)
    assert p.invoices == ["in_fake_a_2026-08"]


def test_invoice_no_overage_is_base_only() -> None:
    svc = _svc()
    svc.create_account("a", "Acme", "o@a.com", Tier.SELF_SERVE)  # 199, 0 minutes, 1 tenant
    svc.attach_tenant("a", "biz-1")
    inv = svc.close_period("a", "2026-07")
    assert inv.line_count == 1
    assert inv.total == Money.from_decimal("199.00")


def test_extra_business_overage_line() -> None:
    acct = _svc()
    acct.create_account("firm", "F", "o@f.com", Tier.CO_DELIVERY)  # 5 included, $149 extra
    a = acct.get_account("firm")
    assert a is not None
    plan = plan_for(Tier.CO_DELIVERY)
    from rgnr8_billing import UsageSummary
    summary = UsageSummary("firm", "2026-08")
    inv = build_invoice(a, plan, summary, tenant_count=8)  # 3 over
    over = [ln for ln in inv.lines if "Additional businesses" in ln.description]
    assert over and over[0].amount == Money.from_decimal("447.00")  # 3 * 149


def test_canceled_account_cannot_attach_or_meter() -> None:
    svc = _svc()
    svc.create_account("a", "Acme", "o@a.com", Tier.CO_DELIVERY)
    svc.set_status("a", AccountStatus.CANCELED)
    for op in (lambda: svc.attach_tenant("a", "biz-x"),
               lambda: svc.record_analyst_minutes("a", 10, "2026-08")):
        try:
            op()
            assert False, "canceled account must be blocked"
        except Exception as e:
            assert "canceled" in str(e)
    svc.set_status("a", AccountStatus.PAUSED)
    try:
        svc.attach_tenant("a", "biz-y")
        assert False
    except Exception as e:
        assert "paused" in str(e)


def test_close_period_is_idempotent() -> None:
    p = FakeBillingProvider()
    svc = _svc(p)
    svc.create_account("a", "Acme", "o@a.com", Tier.SELF_SERVE)
    svc.attach_tenant("a", "biz-1")
    inv1 = svc.close_period("a", "2026-07")
    inv2 = svc.close_period("a", "2026-07")   # retry / re-run
    assert inv1.total == inv2.total
    assert p.invoices == ["in_fake_a_2026-07"]   # finalized exactly once


def test_sql_store_roundtrip() -> None:
    conn = sqlite3.connect(":memory:")
    store = SqlAccountStore(cast("Any", conn), placeholder="?")
    store.create_schema()
    svc = BillingService(store, FakeBillingProvider(), clock=lambda: NOW)
    svc.create_account("a", "Acme", "o@a.com", Tier.CO_DELIVERY)
    svc.attach_tenant("a", "biz-1")
    svc.attach_tenant("a", "biz-2")
    svc.record_analyst_minutes("a", 45, "2026-08", tenant_id="biz-1")
    # rehydrate through a fresh store on the same connection
    store2 = SqlAccountStore(cast("Any", conn), placeholder="?")
    a = store2.get_account("a")
    assert a is not None and a.tier == Tier.CO_DELIVERY and a.tenant_count == 2
    assert store2.find_by_tenant("biz-2") is not None
    assert store2.usage_for("a", "2026-08")[0].quantity == 45


def test_stripe_provider_builds_request_shapes() -> None:
    from rgnr8_billing import HttpResponse, StripeBillingProvider

    class RecordingHttp:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        def post_form(self, url: str, data: dict[str, str], headers: dict[str, str]) -> HttpResponse:
            assert headers["Authorization"].startswith("Bearer ")
            self.calls.append((url, data))
            return HttpResponse(200, "{}")

    http = RecordingHttp()
    prov = StripeBillingProvider(http, secret_key="sk_test_x",
                                 price_ids={"assisted": "price_assisted"},
                                 usage_item_ids={"a": "si_123"})
    svc = BillingService(InMemoryAccountStore(), prov, clock=lambda: NOW)
    svc.create_account("a", "Acme", "o@a.com", Tier.ASSISTED)
    svc.activate("a")
    svc.record_analyst_minutes("a", 30, "2026-08")
    urls = [c[0] for c in http.calls]
    assert any(u.endswith("/v1/customers") for u in urls)
    assert any(u.endswith("/v1/subscriptions") for u in urls)
    assert any("usage_records" in u for u in urls)
