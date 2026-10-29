"""Every bank and card transaction, from QuickBooks' General Ledger report: the
line-level history a mirror's one-total-per-account books don't carry."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

from rgnr8_qbo import QboApiClient, QboApiError
from rgnr8_qbo.history import cash_accounts, read_cash_history

KEYS = ["tx_date", "txn_type", "doc_num", "name", "memo", "split_acc", "subt_nat_amount", "rbal_nat_amount"]
TITLES = ["Date", "Transaction Type", "Num", "Name", "Memo/Description", "Split", "Amount", "Balance"]


def _columns(with_keys: bool = True) -> dict[str, Any]:
    return {"Column": [
        {"ColTitle": t, "ColType": "String",
         **({"MetaData": [{"Name": "ColKey", "Value": k}]} if with_keys else {})}
        for k, t in zip(KEYS, TITLES)
    ]}


def _row(date: str, txn: str, txn_id: str, name: str, amount: str, balance: str,
         split: str = "Software") -> dict[str, Any]:
    return {"type": "Data", "ColData": [
        {"value": date}, {"value": txn, "id": txn_id}, {"value": ""}, {"value": name, "id": "9"},
        {"value": ""}, {"value": split, "id": "77"}, {"value": amount}, {"value": balance}]}


def _begin(balance: str) -> dict[str, Any]:
    return {"type": "Data", "ColData": [{"value": "Beginning Balance"}, {"value": ""}, {"value": ""},
                                        {"value": ""}, {"value": ""}, {"value": ""}, {"value": ""},
                                        {"value": balance}]}


def _section(name: str, acct_id: str, rows: list[dict[str, Any]],
             nested: "list[dict[str, Any]] | None" = None) -> dict[str, Any]:
    return {"type": "Section", "Header": {"ColData": [{"value": name, "id": acct_id}] + [{"value": ""}] * 7},
            "Rows": {"Row": rows + (nested or [])},
            "Summary": {"ColData": [{"value": f"Total for {name}"}] + [{"value": ""}] * 5
                        + [{"value": "999.00"}, {"value": ""}]}}


# Mercury checking (bank): starts at 1,000.00, a deposit, two expenses (one a
# journal entry hitting the account twice), a paycheck. AMEX (card): QuickBooks
# shows a charge as an increase in what's owed, so its amounts are positive.
GL = {
    "Columns": _columns(),
    "Rows": {"Row": [
        _section("Mercury Checking", "151", [
            _begin("1,000.00"),
            _row("2026-10-02", "Deposit", "501", "Northwind", "2,500.00", "3,500.00", "Services"),
            _row("2026-10-03", "Expense", "502", "Figma", "-45.00", "3,455.00"),
            _row("2026-10-09", "Journal Entry", "503", "", "-10.00", "3,445.00", "-Split-"),
            _row("2026-10-09", "Journal Entry", "503", "", "-5.00", "3,440.00", "-Split-"),
            _row("2026-10-15", "Paycheck", "504", "Jason", "-1,200.00", "2,240.00", "Payroll"),
        ]),
        _section("AMEX", "168", [
            _begin("100.00"),
            _row("2026-10-04", "Credit Card Expense", "601", "Delta", "300.00", "400.00", "Flights"),
            _row("2026-10-20", "Credit Card Payment", "602", "", "-100.00", "300.00", "Mercury Checking"),
        ]),
        {"type": "Section", "Summary": {"ColData": [{"value": "TOTAL"}]}},
    ]},
}

ACCOUNTS = {"QueryResponse": {"Account": [
    {"Id": "151", "AcctNum": "151", "Name": "Mercury Checking", "AccountType": "Bank"},
    {"Id": "168", "AcctNum": "", "Name": "AMEX", "AccountType": "Credit Card"},
    {"Id": "4", "AcctNum": "4000", "Name": "Services", "AccountType": "Income"},
]}}


class FakeHttp:
    def __init__(self, report: dict[str, Any], *, refuse_columns: bool = False) -> None:
        self.report = report
        self.refuse_columns = refuse_columns
        self.urls: list[str] = []

    def get(self, url: str, headers: Any) -> Any:
        self.urls.append(url)
        query = parse_qs(urlsplit(url).query)
        body: dict[str, Any] = self.report if "/reports/" in url else ACCOUNTS
        status = 200
        if "/reports/" in url and self.refuse_columns and "columns" in query:
            status, body = 400, {"Fault": {"Error": [{"Message": "Invalid column"}]}}

        class R:
            pass
        r = R()
        r.status = status  # type: ignore[attr-defined]
        r.body = json.dumps(body)  # type: ignore[attr-defined]
        return r


def _client(http: FakeHttp) -> QboApiClient:
    return QboApiClient(http=http, api_base="https://sandbox", realm_id="R1", access_token="tok")


def test_cash_accounts_are_bank_and_card_only_by_their_book_code() -> None:
    accounts = cash_accounts(_client(FakeHttp(GL)))
    assert [(a.code, a.name, a.kind) for a in accounts] == [("151", "Mercury Checking", "bank"),
                                                             ("168", "AMEX", "card")]


def test_every_line_comes_across_signed_money_in_and_out() -> None:
    http = FakeHttp(GL)
    h = read_cash_history(_client(http), "2026-10-01", "2026-10-31")
    got = [(ln.account.code, ln.date, ln.txn_type, ln.amount_minor) for ln in h.lines]
    assert got == [
        ("151", "2026-10-02", "Deposit", 250000),
        ("151", "2026-10-03", "Expense", -4500),
        ("168", "2026-10-04", "Credit Card Expense", -30000),   # a charge is money out
        ("151", "2026-10-09", "Journal Entry", -1000),
        ("151", "2026-10-09", "Journal Entry", -500),
        ("151", "2026-10-15", "Paycheck", -120000),             # paychecks aren't API entities
        ("168", "2026-10-20", "Credit Card Payment", 10000),     # paying the card is money in to it
    ]
    # Two lines of one journal entry on the same account keep distinct ids.
    ids = [ln.id for ln in h.lines]
    assert len(set(ids)) == len(ids)
    url = http.urls[-1]
    assert "/reports/GeneralLedger" in url
    q = parse_qs(urlsplit(url).query)
    assert q["account"] == ["151,168"] and q["start_date"] == ["2026-10-01"]


def test_each_window_ties_to_quickbooks_running_balance() -> None:
    h = read_cash_history(_client(FakeHttp(GL)), "2026-10-01", "2026-10-31")
    by = {w.account.code: w for w in h.windows}
    assert (by["151"].opening_minor, by["151"].movement_minor, by["151"].closing_minor) == (100000, 124000, 224000)
    assert by["151"].difference_minor == 0
    # The card in our sign: owed is negative.
    assert (by["168"].opening_minor, by["168"].closing_minor) == (-10000, -30000)
    assert by["168"].difference_minor == 0
    payload = h.payload("2026-10-29T00:00:00Z")
    assert payload["start"] == "2026-10-01" and len(payload["lines"]) == 7  # type: ignore[arg-type]
    assert {t["account_code"] for t in payload["tie_out"]} == {"151", "168"}  # type: ignore[union-attr]


def test_a_report_without_column_keys_is_read_by_title() -> None:
    h = read_cash_history(_client(FakeHttp({**GL, "Columns": _columns(with_keys=False)})),
                          "2026-10-01", "2026-10-31")
    assert len(h.lines) == 7


def test_a_refused_column_list_falls_back_to_the_default_layout() -> None:
    http = FakeHttp(GL, refuse_columns=True)
    h = read_cash_history(_client(http), "2026-10-01", "2026-10-31")
    assert len(h.lines) == 7
    assert "columns" not in parse_qs(urlsplit(http.urls[-1]).query)


def test_a_report_missing_the_amount_column_is_an_error_not_an_empty_history() -> None:
    cols = {"Column": [{"ColTitle": "Date", "MetaData": [{"Name": "ColKey", "Value": "tx_date"}]}]}
    try:
        read_cash_history(_client(FakeHttp({**GL, "Columns": cols})), "2026-10-01", "2026-10-31")
    except QboApiError as exc:
        assert "subt_nat_amount" in str(exc)
    else:
        raise AssertionError("expected QboApiError")


def test_sub_accounts_nested_in_a_section_are_read_as_their_own_account() -> None:
    nested = _section("Savings", "152", [_begin("0.00"),
                                         _row("2026-10-05", "Transfer", "701", "", "50.00", "50.00")])
    report = {"Columns": _columns(), "Rows": {"Row": [
        _section("Mercury Checking", "151", [_begin("1,000.00")], nested=[nested])]}}
    accounts = cash_accounts(_client(FakeHttp(GL)))
    from rgnr8_qbo.history import CashAccount
    accounts.append(CashAccount(qbo_id="152", code="152", name="Savings", kind="bank"))
    h = read_cash_history(_client(FakeHttp(report)), "2026-10-01", "2026-10-31", accounts)
    assert [(ln.account.code, ln.amount_minor) for ln in h.lines] == [("152", 5000)]
