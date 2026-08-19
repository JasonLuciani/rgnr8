"""RGNR8 external integrations — the ten connectors a real owner's stack needs.

Each provider is a Protocol seam over an injected HTTP client, with a
shape-correct real adapter (Plaid, Stripe, Gusto, ACH bill-pay) and a Fake for
tests, plus a mapping function that lands the provider's data in RGNR8's existing
ledger paths — the review inbox, the AR/AP payment paths, the payroll journal —
idempotently. No adapter opens a socket itself; supplying credentials and binding
a real HTTP client turns them all live at once.
"""

from __future__ import annotations

from .billpay import (
    AchDisbursementProvider,
    Disbursement,
    DisbursementProvider,
    FakeDisbursementProvider,
    to_bill_payment,
)
from .gusto import (
    FakePayrollProvider,
    GustoPayrollProvider,
    PayrollProvider,
    PayrollRun,
    to_payroll_run,
)
from .http import HttpClient, RecordingHttpClient
from .plaid import (
    BankFeedProvider,
    BankTransaction,
    FakeBankFeedProvider,
    PlaidBankFeedProvider,
    to_inbox_items,
)
from .stripe import (
    FakePaymentProvider,
    PaymentEvent,
    PaymentProvider,
    StripePaymentProvider,
    to_ar_payment,
)

__all__ = [
    "HttpClient",
    "RecordingHttpClient",
    "BankTransaction",
    "BankFeedProvider",
    "PlaidBankFeedProvider",
    "FakeBankFeedProvider",
    "to_inbox_items",
    "PaymentEvent",
    "PaymentProvider",
    "StripePaymentProvider",
    "FakePaymentProvider",
    "to_ar_payment",
    "PayrollRun",
    "PayrollProvider",
    "GustoPayrollProvider",
    "FakePayrollProvider",
    "to_payroll_run",
    "Disbursement",
    "DisbursementProvider",
    "AchDisbursementProvider",
    "FakeDisbursementProvider",
    "to_bill_payment",
]
