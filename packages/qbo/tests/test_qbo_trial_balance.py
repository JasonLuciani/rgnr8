"""QuickBooks trial-balance fetch for a migration: the chart of accounts joined
with the TrialBalance report, normalized into the go-live source-account input
rows — and proven to feed the real converter."""

from __future__ import annotations

import json
from typing import Any

from rgnr8_qbo import QboApiClient
from rgnr8_qbo.client import _to_minor


class FakeHttp:
    """Routes by URL: the reports endpoint returns the TrialBalance report; any
    query endpoint returns the Account list."""

    def __init__(self, accounts: list[dict[str, Any]], report: dict[str, Any]) -> None:
        self.accounts = accounts
        self.report = report
        self.urls: list[str] = []

    def get(self, url: str, headers: Any) -> Any:
        self.urls.append(url)
        body = (json.dumps(self.report) if "/reports/TrialBalance" in url
                else json.dumps({"QueryResponse": {"Account": self.accounts}}))

        class R:
            status = 200
        R.body = body  # type: ignore[attr-defined]
        return R()


ACCOUNTS = [
    {"Id": "1", "AcctNum": "1000", "Name": "Checking", "AccountType": "Bank"},
    {"Id": "2", "AcctNum": "2300", "Name": "Payroll Liabilities",
     "AccountType": "Other Current Liability"},
    {"Id": "3", "AcctNum": "3000", "Name": "Owner Equity", "AccountType": "Equity"},
]

# A QBO TrialBalance report: account rows carry the account id + debit/credit;
# the TOTAL summary row carries no account id and must be skipped.
REPORT = {"Rows": {"Row": [
    {"ColData": [{"value": "Checking", "id": "1"}, {"value": "1,201.00"}, {"value": ""}]},
    {"ColData": [{"value": "Payroll Liabilities", "id": "2"}, {"value": ""}, {"value": "701.00"}]},
    {"ColData": [{"value": "Owner Equity", "id": "3"}, {"value": ""}, {"value": "500.00"}]},
    {"Summary": {"ColData": [{"value": "TOTAL"}, {"value": "1201.00"}, {"value": "1201.00"}]}},
]}}


def _client() -> QboApiClient:
    return QboApiClient(http=FakeHttp(ACCOUNTS, REPORT), api_base="https://sandbox",
                        realm_id="R1", access_token="tok")


def test_to_minor_parses_qbo_decimal_strings() -> None:
    assert _to_minor("1,201.00") == 120100
    assert _to_minor("") == 0
    assert _to_minor("-50") == -5000
    assert _to_minor("$3.5") == 350
    assert _to_minor(None) == 0
    assert _to_minor("garbage") == 0


def test_trial_balance_joins_the_report_with_the_chart() -> None:
    rows = _client().trial_balance(as_of="2026-08-25")
    by_code = {r["code"]: r for r in rows}
    assert set(by_code) == {"1000", "2300", "3000"}          # the TOTAL row is skipped
    assert by_code["1000"] == {"code": "1000", "name": "Checking", "account_type": "Bank",
                               "debit_minor": 120100, "credit_minor": 0}
    assert by_code["2300"]["credit_minor"] == 70100 and by_code["2300"]["debit_minor"] == 0
    assert by_code["2300"]["account_type"] == "Other Current Liability"  # from the chart join

# The go-live converter that consumes these rows is exercised end to end in the
# web package's test_qbo_migrate.py — the qbo package doesn't depend on it.
