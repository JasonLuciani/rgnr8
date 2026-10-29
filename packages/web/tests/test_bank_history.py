"""Bank history from QuickBooks, through the REAL ledger service.

The mirror keeps RGNR8's books at QuickBooks' totals, one per account; this is
the line-level history behind them: every bank and card transaction, copied
window by window, checked against QuickBooks' running balance and the books,
and never posted. Skipped (not silently passed) if the service hasn't been built.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote_plus, urlsplit

from test_books_e2e import (  # noqa: F401 - ledger_service is a fixture
    NOW,
    SECRET,
    _app,
    _req,
    ledger_service,
)

from rgnr8_web import Request, Role, User, sign_jwt
from rgnr8_web.bank_history import bring_over_history, refresh_recent

TODAY = date(2026, 10, 29)
KEYS = ["tx_date", "txn_type", "doc_num", "name", "memo", "split_acc", "subt_nat_amount", "rbal_nat_amount"]

ACCOUNTS = [
    {"id": "151", "code": "151", "number": "151", "name": "Mercury Checking", "account_type": "Bank"},
    {"id": "168", "code": "168", "number": "", "name": "AMEX", "account_type": "Credit Card"},
    {"id": "4", "code": "4000", "number": "4000", "name": "Services", "account_type": "Income"},
]


def _dataset() -> list[tuple[str, str, str, str, str, int]]:
    """Two years of a small business: a client payment on the 5th, rent on the
    1st, payroll on the 15th, and a card charge on the 20th, every month."""
    rows: list[tuple[str, str, str, str, str, int]] = []
    d = date(2024, 10, 1)
    n = 0
    while d <= TODAY:
        for day, acct, kind, name, cents in ((1, "151", "Expense", "Landlord", -200000),
                                             (5, "151", "Deposit", "Northwind", 900000),
                                             (15, "151", "Paycheck", "Jason", -400000),
                                             (20, "168", "Credit Card Expense", "Delta", 35000)):
            when = d.replace(day=day)
            if when <= TODAY:
                n += 1
                rows.append((acct, when.isoformat(), kind, str(n), name, cents))
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return rows


DATA = _dataset()
OPENING = {"151": 1_000_000, "168": 0}  # before the first row, QuickBooks' sign


def _money(cents: int) -> str:
    return f"{cents / 100:.2f}"


class FakeQbo:
    """A QuickBooks company that answers the General Ledger report from DATA."""

    def __init__(self, data: "list[tuple[str, str, str, str, str, int]] | None" = None) -> None:
        self.data = DATA if data is None else data
        self.calls: list[dict[str, str]] = []
        self.realm_id = "9341453432535209"

    def chart_of_accounts(self, *, include_inactive: bool = False) -> list[dict[str, str]]:
        return ACCOUNTS

    def report(self, name: str, params: dict[str, str]) -> dict[str, Any]:
        assert name == "GeneralLedger"
        self.calls.append(dict(params))
        start, end = params["start_date"], params["end_date"]
        sections = []
        for acct in params["account"].split(","):
            before = OPENING.get(acct, 0) + sum(r[5] for r in self.data if r[0] == acct and r[1] < start)
            bal = before
            rows: list[dict[str, Any]] = [{"type": "Data", "ColData": [{"value": "Beginning Balance"}]
                                           + [{"value": ""}] * 6 + [{"value": _money(before)}]}]
            for a, when, kind, txn_id, who, cents in self.data:
                if a == acct and start <= when <= end:
                    bal += cents
                    rows.append({"type": "Data", "ColData": [
                        {"value": when}, {"value": kind, "id": txn_id}, {"value": ""}, {"value": who},
                        {"value": ""}, {"value": "Split"}, {"value": _money(cents)}, {"value": _money(bal)}]})
            sections.append({"type": "Section", "Header": {"ColData": [{"value": acct, "id": acct}]},
                             "Rows": {"Row": rows}})
        return {"Columns": {"Column": [{"ColTitle": k, "MetaData": [{"Name": "ColKey", "Value": k}]}
                                       for k in KEYS]},
                "Rows": {"Row": sections}}


def _balances(data: list[tuple[str, str, str, str, str, int]]) -> dict[str, int]:
    """Each account's current balance in our sign (card owed is negative)."""
    bank = OPENING["151"] + sum(r[5] for r in data if r[0] == "151")
    card = -(OPENING["168"] + sum(r[5] for r in data if r[0] == "168"))
    return {"151": bank, "168": card}


def _go_live(app: Any, tenant: str, *, bank_minor: int, card_minor: int) -> None:
    res = app._ledger.go_live(tenant, {
        "contract": "go-live/1", "tenant_id": tenant, "source_system": "quickbooks",
        "cutover_date": "2026-09-30", "currency": "USD", "coa_category": "PROFESSIONAL_SERVICES",
        "opening_balance_equity_code": "3010",
        "source_accounts": [
            {"code": "151", "name": "Mercury Checking", "balance_minor": str(bank_minor), "subtype": "BANK"},
            {"code": "168", "name": "AMEX", "balance_minor": str(card_minor), "subtype": "CREDIT_CARD"},
            {"code": "3900", "name": "Retained Earnings", "balance_minor": str(-(bank_minor + card_minor)),
             "subtype": "RETAINED_EARNINGS"},
        ],
    })
    assert res.ok, res.body
    assert app._qbo_pin(SimpleNamespace(tenant_id=tenant), "9341453432535209", "2026-09-30") == ""


def _mirrored(ledger_service: str, tenant: str) -> Any:
    app = _app(ledger_service, [tenant])
    b = _balances(DATA)
    _go_live(app, tenant, bank_minor=b["151"], card_minor=b["168"])
    return app


def test_two_years_come_across_window_by_window_and_post_nothing(ledger_service: str) -> None:
    app = _mirrored(ledger_service, "bhone")
    before = app._ledger.entries("bhone").body["entries"]
    qbo = FakeQbo()
    result = bring_over_history(qbo, app._ledger, "bhone", TODAY, imported_at="2026-10-29T12:00:00+00:00")
    assert result.ok and result.finished, result.describe()
    assert result.differences == []                    # every window ties, and the newest to the books
    assert result.lines == len(DATA)
    assert len(qbo.calls) == result.windows >= 8        # a quarter per report call
    assert result.covered_to == TODAY.isoformat()
    assert result.covered_from <= "2024-10-01"
    assert app._ledger.entries("bhone").body["entries"] == before  # nothing posted

    stored = app._ledger.bank_history("bhone", lines=False).body
    assert stored["count"] == len(DATA)
    oct25 = [p for p in stored["periods"] if p["period"] == "2025-10" and p["account_code"] == "151"]
    assert oct25 and oct25[0]["in_minor"] == "900000" and oct25[0]["out_minor"] == "600000"
    card = [p for p in stored["periods"] if p["period"] == "2025-10" and p["account_code"] == "168"]
    assert card[0]["out_minor"] == "35000" and card[0]["in_minor"] == "0"   # a charge is money out


def test_a_tight_time_budget_stops_and_the_next_press_carries_on(ledger_service: str) -> None:
    app = _mirrored(ledger_service, "bhtwo")
    ticks = iter(range(0, 10_000, 10))  # each window "takes" ten seconds
    first = bring_over_history(FakeQbo(), app._ledger, "bhtwo", TODAY, imported_at="t1",
                               budget_seconds=15, clock=lambda: float(next(ticks)))
    assert first.ok and not first.finished
    assert "press Bring over history again" in first.describe()
    second = bring_over_history(FakeQbo(), app._ledger, "bhtwo", TODAY, imported_at="t2")
    assert second.ok and second.finished
    assert app._ledger.bank_history("bhtwo", lines=False).body["count"] == len(DATA)


def test_refresh_keeps_the_recent_weeks_and_says_when_the_books_disagree(ledger_service: str) -> None:
    app = _mirrored(ledger_service, "bhthree")
    bring_over_history(FakeQbo(), app._ledger, "bhthree", TODAY, imported_at="t1")
    # QuickBooks gains a transaction the books haven't caught up with yet.
    late = [*DATA, ("151", "2026-10-28", "Expense", "9999", "Figma", -4500)]
    recent = refresh_recent(FakeQbo(late), app._ledger, "bhthree", TODAY, imported_at="t2")
    assert recent.ok
    assert app._ledger.bank_history("bhthree", lines=False).body["count"] == len(DATA) + 1
    assert [d.against for d in recent.differences] == ["the books"]
    assert "Mercury Checking is 45.00 off the books" in recent.describe()


def test_the_history_page_and_the_import_button(ledger_service: str) -> None:
    app = _mirrored(ledger_service, "bhfour")
    app._qbo = SimpleNamespace(api_client=lambda tenant: FakeQbo())
    empty = _req(app, "bhfour", "/t/bhfour/inbox/history")
    assert empty.status == 200 and "No history yet" in empty.body
    assert 'action="/t/bhfour/inbox/history/import"' in empty.body

    pressed = _req(app, "bhfour", "/t/bhfour/inbox/history/import", "POST")
    assert pressed.status == 302
    loc = pressed.headers["Location"]
    assert loc.startswith("/t/bhfour/inbox/history?done=")
    assert "brought over" in unquote_plus(parse_qs(urlsplit(loc).query)["done"][0])

    page = _req(app, "bhfour", loc).body
    assert "Covers 2024-" in page and "Ties to QuickBooks" in page
    assert "2025-10" in page and "Northwind" in page and "Paycheck" in page
    assert "Card spending" in page
    one = _req(app, "bhfour", "/t/bhfour/inbox/history?account=168&from=2026-10-01").body
    assert "Delta" in one and "Landlord" not in one
    # The history tab sits beside the review queue, which points to it when empty.
    inbox = _req(app, "bhfour", "/t/bhfour/inbox").body
    assert 'href="/t/bhfour/inbox/history"' in inbox
    assert f"Your {len(DATA):,} bank and card transactions from QuickBooks are under History" in inbox


def test_a_viewer_can_read_the_history_but_not_import_it(ledger_service: str) -> None:
    app = _mirrored(ledger_service, "bhfive")
    app._qbo = SimpleNamespace(api_client=lambda tenant: FakeQbo())
    app._users.upsert_user(User("u-viewer", "viewer@bhfive.com", "Viewer"))
    app._users.set_membership("u-viewer", "bhfive", Role.VIEWER)
    tok = sign_jwt({"sub": "u-viewer", "tenant": "bhfive", "exp": NOW + 3600}, SECRET)
    headers = {"authorization": f"Bearer {tok}"}
    page = app.handle(Request("GET", "/t/bhfive/inbox/history", headers))
    assert page.status == 200 and "history/import" not in page.body
    refused = app.handle(Request("POST", "/t/bhfive/inbox/history/import",
                                 {**headers, "content-type": "application/x-www-form-urlencoded"}, ""))
    assert refused.status == 403


def test_the_import_stays_open_during_the_parallel_run(ledger_service: str) -> None:
    from rgnr8_web.parallel_run import is_posting_path

    assert not is_posting_path(["t", "acme", "inbox", "history", "import"], "POST")
    app = _mirrored(ledger_service, "bhsix")
    app._qbo = SimpleNamespace(api_client=lambda tenant: FakeQbo())
    page = _req(app, "bhsix", "/t/bhsix/inbox/history").body
    assert "Bring over history" in page and "book of record during the parallel run" not in page
