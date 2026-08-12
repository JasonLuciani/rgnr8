"""RGNR8 billing — accounts, plans/entitlements, usage metering, and invoicing.

Tenancy is Account → Tenant(s) → Users. This package owns the Account (billing
customer) and everything money: the plan/tier model and entitlements, the usage
meter, period invoicing, and a Stripe-shaped provider seam. Authorization stays
per-tenant in `rgnr8_web.rbac`; here we bill and gate features by plan.
"""

from __future__ import annotations

from .accounts import Account, AccountStatus
from .plans import PLANS, Feature, Plan, Tier, plan_for
from .entitlements import Entitlements
from .usage import UsageEvent, UsageKind, UsageSummary
from .invoice import Invoice, InvoiceLine, build_invoice
from .provider import (
    BillingPortalProvider,
    BillingProvider,
    FakeBillingPortalProvider,
    FakeBillingProvider,
    HttpClient,
    HttpResponse,
    StripeBillingPortalProvider,
    StripeBillingProvider,
)
from .store import AccountStore, InMemoryAccountStore, SqlAccountStore
from .plan_changes import PlanChange, PlanChangeKind, classify_change
from .service import BillingError, BillingService, EntitlementError
from .checkout import (
    CheckoutCompleted,
    CheckoutProvider,
    CheckoutSession,
    FakeCheckoutProvider,
    StripeCheckoutProvider,
    verify_hmac_sha256,
)
from .signup import SelfServeSignup

__version__ = "0.1.0"

__all__ = [
    "Account",
    "AccountStatus",
    "Tier",
    "Plan",
    "Feature",
    "PLANS",
    "plan_for",
    "Entitlements",
    "UsageEvent",
    "UsageKind",
    "UsageSummary",
    "Invoice",
    "InvoiceLine",
    "build_invoice",
    "BillingProvider",
    "BillingPortalProvider",
    "FakeBillingProvider",
    "FakeBillingPortalProvider",
    "StripeBillingProvider",
    "StripeBillingPortalProvider",
    "HttpClient",
    "HttpResponse",
    "AccountStore",
    "InMemoryAccountStore",
    "SqlAccountStore",
    "BillingService",
    "BillingError",
    "EntitlementError",
    "PlanChange",
    "PlanChangeKind",
    "classify_change",
    "CheckoutProvider",
    "CheckoutSession",
    "CheckoutCompleted",
    "FakeCheckoutProvider",
    "StripeCheckoutProvider",
    "verify_hmac_sha256",
    "SelfServeSignup",
]
