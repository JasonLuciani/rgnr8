"""Each adapter maps a provider's data into an existing ledger path, idempotently.

The Fakes exercise the mapping contracts; the real adapters are exercised against
a RecordingHttpClient so their request-building and response-parsing are covered
without a network.
"""

from __future__ import annotations

from rgnr8_integrations import (
    BankTransaction,
    FakeBankFeedProvider,
    FakeDisbursementProvider,
    FakePaymentProvider,
    FakePayrollProvider,
    GustoPayrollProvider,
    PayrollRun,
    PlaidBankFeedProvider,
    RecordingHttpClient,
    StripePaymentProvider,
    to_ar_payment,
    to_bill_payment,
    to_inbox_items,
    to_payroll_run,
)


# --- Plaid bank feed ---------------------------------------------------------

def test_bank_feed_maps_to_inbox_items_keyed_by_provider_id() -> None:
    provider = FakeBankFeedProvider([
        BankTransaction("tx1", "2026-02-01", -4500, "Home Depot"),
        BankTransaction("tx2", "2026-02-02", 120000, "Customer deposit"),
    ])
    txns, cursor = provider.fetch_transactions("access-tok", "")
    items = to_inbox_items(txns, account_code="1000")
    assert [i["external_id"] for i in items] == ["tx1", "tx2"]
    assert items[0]["amount_minor"] == "-4500"
    assert items[1]["description"] == "Customer deposit"


def test_plaid_adapter_builds_the_sync_request_and_inverts_sign() -> None:
    # Plaid: amount 45.00 positive means money LEFT the account → -4500 to us.
    http = RecordingHttpClient([
        {"added": [{"transaction_id": "p1", "date": "2026-02-01", "amount": 45.00, "name": "Depot"}],
         "next_cursor": "CURSOR2"}
    ])
    provider = PlaidBankFeedProvider(http, "cid", "sec")
    txns, cursor = provider.fetch_transactions("acc-tok", "")
    assert cursor == "CURSOR2"
    assert txns[0].external_id == "p1"
    assert txns[0].amount_minor == -4500
    # the request carried the credentials and the access token
    call = http.calls[0]
    assert call["url"].endswith("/transactions/sync")
    assert call["body"]["access_token"] == "acc-tok"


# --- Stripe payments ---------------------------------------------------------

def test_stripe_payment_maps_to_an_ar_payment_idempotently() -> None:
    provider = FakePaymentProvider()
    event = provider.parse_webhook(
        {"event_id": "evt_1", "invoice_ref": "INV-100", "amount_minor": 50000, "created": "2026-03-01"}
    )
    payment = to_ar_payment(event)
    assert payment["idempotency_key"] == "stripe:evt_1"
    assert payment["invoice_id"] == "INV-100"
    assert payment["amount_minor"] == "50000"


def test_stripe_adapter_builds_a_checkout_session() -> None:
    http = RecordingHttpClient([{"url": "https://pay.stripe/xyz"}])
    provider = StripePaymentProvider(http, "sk_test")
    link = provider.create_payment_link("INV-9", 12345, "USD")
    assert link == "https://pay.stripe/xyz"
    call = http.calls[0]
    assert call["headers"]["Authorization"] == "Bearer sk_test"
    assert call["body"]["client_reference_id"] == "INV-9"


# --- Gusto payroll -----------------------------------------------------------

def test_gusto_run_maps_to_the_payroll_journal_payload() -> None:
    provider = FakePayrollProvider([
        PayrollRun("g1", "2026-02-15", 1000000, 200000, 90000, 800000),
        PayrollRun("g0", "2026-01-15", 900000, 180000, 81000, 720000),
    ])
    runs = provider.fetch_payrolls("comp", "2026-02-01")
    assert [r.external_id for r in runs] == ["g1"]  # 'since' filter applied
    payload = to_payroll_run(runs[0])
    assert payload["idempotency_key"] == "gusto:g1"
    assert payload["gross_minor"] == "1000000"
    assert payload["net_minor"] == "800000"


def test_gusto_adapter_parses_processed_payrolls() -> None:
    http = RecordingHttpClient([
        {"payrolls": [
            {"payroll_uuid": "u1", "check_date": "2026-02-15",
             "totals": {"gross_pay": "10000.00", "employee_taxes": "2000.00",
                        "employer_taxes": "900.00", "net_pay": "8000.00"}}
        ]}
    ])
    provider = GustoPayrollProvider(http, "tok")
    runs = provider.fetch_payrolls("comp", "2026-02-01")
    assert runs[0].gross_minor == 1000000
    assert runs[0].net_minor == 800000
    assert "processed=true" in str(http.calls[0]["url"])


# --- Bill pay ----------------------------------------------------------------

def test_billpay_disbursement_maps_to_ap_payment_idempotently() -> None:
    provider = FakeDisbursementProvider(status="settled")
    disb = provider.send_payment("BILL-7", 75000, "1000", "2026-04-01")
    assert disb.status == "settled"
    payment = to_bill_payment(disb)
    assert payment["idempotency_key"] == "billpay:BILL-7"
    assert payment["bill_id"] == "BILL-7"
    assert payment["amount_minor"] == "75000"
    assert payment["reference"] == "disb-1"
