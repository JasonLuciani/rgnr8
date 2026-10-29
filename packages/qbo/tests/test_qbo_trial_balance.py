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
                               "numbered": True, "debit_minor": 120100, "credit_minor": 0}
    assert by_code["2300"]["credit_minor"] == 70100 and by_code["2300"]["debit_minor"] == 0
    assert by_code["2300"]["account_type"] == "Other Current Liability"  # from the chart join


def test_an_account_without_a_number_is_coded_by_its_id_and_says_so() -> None:
    """A company that doesn't number its accounts still comes across whole, coded
    by QuickBooks' id; each row says whether its code is a real number, so the
    screens can show that account by name instead of by a meaningless "57"."""
    accounts = [{"Id": "57", "Name": "Checking", "AccountType": "Bank"},
                {"Id": "2", "AcctNum": "2300", "Name": "Payroll Liabilities",
                 "AccountType": "Other Current Liability"},
                {"Id": "3", "Name": "Owner Equity", "AccountType": "Equity"}]
    report = {"Rows": {"Row": [
        {"ColData": [{"value": "Checking", "id": "57"}, {"value": "1,201.00"}, {"value": ""}]},
        {"ColData": [{"value": "Payroll Liabilities", "id": "2"}, {"value": ""}, {"value": "701.00"}]},
        {"ColData": [{"value": "Owner Equity", "id": "3"}, {"value": ""}, {"value": "500.00"}]},
    ]}}
    client = QboApiClient(http=FakeHttp(accounts, report), api_base="https://sandbox",
                          realm_id="R1", access_token="tok")
    rows = {r["code"]: r["numbered"] for r in client.trial_balance(as_of="2026-08-25")}
    assert rows == {"57": False, "2300": True, "3": False}
    chart = {a["code"]: a["number"] for a in client.chart_of_accounts()}
    assert chart == {"57": "", "2300": "2300", "3": ""}

# The go-live converter that consumes these rows is exercised end to end in the
# web package's test_qbo_migrate.py — the qbo package doesn't depend on it.


# --- completeness: nothing may go missing on the way across -----------------
#
# The first real go-live dropped thirteen accounts carrying balances - among
# them $206,556.77 of revenue, $68,000 of salaries and two bank accounts -
# because the chart of accounts was read with one bare query. QuickBooks
# answers that with at most 100 rows; the company had more. Trial-balance rows
# for the other accounts found no match in the join and were skipped as if they
# were total lines, and the go-live plugged the difference into Opening Balance
# Equity, so the books still showed "in balance".

import re  # noqa: E402

import pytest  # noqa: E402

from rgnr8_qbo.client import QboApiError  # noqa: E402


class _RealisticQbo:
    """Behaves like QuickBooks where it matters: a query returns at most 100
    rows unless it pages, and at most 1,000 a page however it is asked."""

    def __init__(self, n_accounts: int) -> None:
        self.accounts = [
            {"Id": str(i), "Name": f"Account {i}", "AccountType": "Expense"}
            for i in range(1, n_accounts)
        ] + [{"Id": str(n_accounts), "Name": "Equity", "AccountType": "Equity"}]
        rows = [{"ColData": [{"value": a["Name"], "id": a["Id"]}, {"value": "1.00"}, {"value": ""}]}
                for a in self.accounts[:-1]]
        total = f"{n_accounts - 1}.00"
        rows.append({"ColData": [{"value": "Equity", "id": str(n_accounts)}, {"value": ""},
                                 {"value": total}]})
        rows.append({"Summary": {"ColData": [{"value": "TOTAL"}, {"value": total}, {"value": total}]}})
        self.report = {"Rows": {"Row": rows}}

    def get(self, url: str, headers: Any) -> Any:
        if "/reports/TrialBalance" in url:
            body = json.dumps(self.report)
        else:
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(url).query)["query"][0]
            m = re.search(r"STARTPOSITION (\d+) MAXRESULTS (\d+)", q)
            start, size = (int(m.group(1)), min(int(m.group(2)), 1000)) if m else (1, 100)
            body = json.dumps({"QueryResponse": {"Account": self.accounts[start - 1:start - 1 + size]}})

        class R:
            status = 200
        R.body = body  # type: ignore[attr-defined]
        return R()


def _realistic(n: int) -> QboApiClient:
    return QboApiClient(http=_RealisticQbo(n), api_base="https://q", realm_id="R", access_token="t")


def test_a_company_with_more_than_a_hundred_accounts_comes_across_whole() -> None:
    rows = _realistic(1250).trial_balance(as_of="2026-10-28")
    assert len(rows) == 1250, "every account with a balance, not the first hundred"
    assert sum(r["debit_minor"] for r in rows) == sum(r["credit_minor"] for r in rows)


def test_the_chart_of_accounts_pages_past_a_thousand() -> None:
    assert len(_realistic(2500).chart_of_accounts(include_inactive=True)) == 2500


def test_a_row_for_an_unknown_account_is_an_error_not_a_skip() -> None:
    fake = _RealisticQbo(3)
    fake.accounts = fake.accounts[1:]  # the chart forgets account 1; the report does not
    client = QboApiClient(http=fake, api_base="https://q", realm_id="R", access_token="t")
    with pytest.raises(QboApiError, match="did not return"):
        client.trial_balance(as_of="2026-10-28")


def test_rows_that_do_not_add_up_to_the_reports_total_are_refused() -> None:
    fake = _RealisticQbo(3)
    fake.report["Rows"]["Row"][-1]["Summary"]["ColData"][1]["value"] = "999.00"
    client = QboApiClient(http=fake, api_base="https://q", realm_id="R", access_token="t")
    with pytest.raises(QboApiError, match="some rows did not come across"):
        client.trial_balance(as_of="2026-10-28")
