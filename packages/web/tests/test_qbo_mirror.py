"""A parallel-run mirror of QuickBooks: exact, append-only, and never overwriting
anything RGNR8 did itself.

During a parallel run QuickBooks is the book of record, so every number RGNR8
shows must be QuickBooks' number. A refresh reads QuickBooks' trial balance,
reads RGNR8's, posts ONE balancing catch-up entry for the difference, and then
proves the two match account by account.
"""

from __future__ import annotations

from typing import Any

from rgnr8_web.ledger_client import LedgerResponse
from rgnr8_web.qbo_mirror import refresh_mirror

TODAY = "2026-10-29"


class FakeQbo:
    def __init__(self, rows: "list[dict[str, Any]]") -> None:
        self.rows = rows

    def trial_balance(self, *, as_of: str = "") -> "list[dict[str, Any]]":
        return [dict(r) for r in self.rows]


def _row(code: str, name: str, kind: str, minor: int) -> "dict[str, Any]":
    return {"code": code, "name": name, "account_type": kind,
            "debit_minor": max(minor, 0), "credit_minor": max(-minor, 0)}


class FakeLedger:
    """Just enough of the ledger: a chart, balances, and an append-only journal."""

    def __init__(self, balances: "dict[str, int]", *, sources: "tuple[str, ...]" = ("quickbooks",)) -> None:
        self.chart = {c: {"code": c, "name": c} for c in balances}
        self.balances = dict(balances)
        self.journal = [{"id": f"e{i}", "date": "2026-09-30", "memo": "opening", "source": s}
                        for i, s in enumerate(sources)]
        self.posted: list[dict[str, Any]] = []
        self.created: list[str] = []

    def accounts(self, tenant: str) -> LedgerResponse:
        return LedgerResponse(200, {"accounts": list(self.chart.values())})

    def entries(self, tenant: str, **_: Any) -> LedgerResponse:
        return LedgerResponse(200, {"entries": self.journal})

    def trial_balance(self, tenant: str, *, to: str = "", frm: str = "") -> LedgerResponse:
        rows = [{"code": c, "debit_minor": str(max(b, 0)), "credit_minor": str(max(-b, 0))}
                for c, b in self.balances.items() if b != 0]
        return LedgerResponse(200, {"rows": rows})

    def create_account(self, tenant: str, code: str, name: str, **_: Any) -> LedgerResponse:
        self.chart[code] = {"code": code, "name": name}
        self.created.append(code)
        return LedgerResponse(201, {})

    def post_entry(self, tenant: str, date: str, lines: "list[dict[str, Any]]", **kw: Any) -> LedgerResponse:
        if any(l["code"] not in self.chart for l in lines):
            return LedgerResponse(400, {"error": "unknown account code"})
        if sum(int(l["amount_minor"]) * (1 if l["side"] == "DEBIT" else -1) for l in lines) != 0:
            return LedgerResponse(400, {"error": "unbalanced"})
        for l in lines:
            sign = 1 if l["side"] == "DEBIT" else -1
            self.balances[l["code"]] = self.balances.get(l["code"], 0) + sign * int(l["amount_minor"])
        entry = {"id": f"e{len(self.journal)}", "date": date, "source": kw.get("source"), **kw}
        self.journal.append(entry)
        self.posted.append({"date": date, "lines": lines, **kw})
        return LedgerResponse(201, {"entry": entry})


SEPT_CLOSE = {"35": 100_000_00, "8": 373_20, "6": -100_373_20}  # cash, A/R, revenue


def _qbo(balances: "dict[str, int]", extra: "list[dict[str, Any]] | None" = None) -> FakeQbo:
    names = {"35": ("Checking", "Bank"), "8": ("Accounts Receivable", "Accounts Receivable"),
             "6": ("Services", "Income")}
    return FakeQbo([_row(c, *names[c], b) for c, b in balances.items()] + (extra or []))


def test_nothing_changed_in_quickbooks_means_nothing_is_posted() -> None:
    ledger = FakeLedger(SEPT_CLOSE)
    res = refresh_mirror(_qbo(SEPT_CLOSE), ledger, "t", as_of=TODAY)
    assert res.ok and res.up_to_date
    assert ledger.posted == []


def test_the_month_so_far_comes_across_as_one_exact_catch_up() -> None:
    # In QuickBooks since Sep 30: $1,500 collected into the bank as new revenue.
    now = {"35": 101_500_00, "8": 373_20, "6": -101_873_20}
    ledger = FakeLedger(SEPT_CLOSE)
    res = refresh_mirror(_qbo(now), ledger, "t", as_of=TODAY)
    assert res.ok and not res.up_to_date
    assert len(ledger.posted) == 1, "one balancing entry, not a re-booking of every transaction"
    entry = ledger.posted[0]
    assert entry["date"] == TODAY and entry["source"] == "quickbooks"
    assert entry["idempotency_key"].startswith(f"qbo-mirror:{TODAY}:")
    assert {k: v for k, v in ledger.balances.items() if v} == now


def test_a_second_refresh_with_nothing_new_is_a_no_op() -> None:
    now = {"35": 101_500_00, "8": 373_20, "6": -101_873_20}
    ledger = FakeLedger(SEPT_CLOSE)
    refresh_mirror(_qbo(now), ledger, "t", as_of=TODAY)
    again = refresh_mirror(_qbo(now), ledger, "t", as_of=TODAY)
    assert again.up_to_date and len(ledger.posted) == 1


def test_an_account_new_in_quickbooks_is_added_before_it_is_posted_to() -> None:
    now = {"35": 99_000_00, "8": 373_20, "6": -100_373_20}
    software = _row("212", "Software & apps", "Expense", 1_000_00)
    ledger = FakeLedger(SEPT_CLOSE)
    res = refresh_mirror(_qbo(now, [software]), ledger, "t", as_of=TODAY)
    assert res.ok and res.accounts_created == 1 and ledger.created == ["212"]
    assert ledger.balances["212"] == 1_000_00


def test_a_ledger_with_entries_of_its_own_is_never_overwritten() -> None:
    """Once RGNR8 holds work of its own, matching QuickBooks would undo it."""
    ledger = FakeLedger(SEPT_CLOSE, sources=("quickbooks", "manual"))
    res = refresh_mirror(_qbo({"35": 1, "8": 0, "6": -1}), ledger, "t", as_of=TODAY)
    assert not res.ok and "of its own" in res.error
    assert ledger.posted == [] and ledger.created == []


def test_an_incomplete_read_of_quickbooks_changes_nothing() -> None:
    short = {"35": 101_500_00, "8": 373_20, "6": -100_373_20}  # does not balance
    ledger = FakeLedger(SEPT_CLOSE)
    res = refresh_mirror(_qbo(short), ledger, "t", as_of=TODAY)
    assert not res.ok and "out of balance by 1,500.00" in res.error
    assert ledger.posted == []


# --- the route: opening balances at the last month-end, then the month so far ---


def test_go_live_takes_opening_balances_at_the_last_month_end(monkeypatch) -> None:
    """Going live *today* locked today's whole month (go-live freezes the cutover
    month and everything before it), so nothing - not even a mirror catch-up -
    could be posted for the rest of it. Opening balances now come from the last
    month-end, and the month so far follows straight after as a catch-up."""
    from datetime import date, timedelta

    import rgnr8_web.qbo_migrate as qm
    import rgnr8_web.qbo_mirror as mirror_mod
    from rgnr8_web import LedgerClient, Request
    from rgnr8_web.qbo_mirror import MirrorResult
    from test_qbo_connect import RecordingLedgerTransport, _connected_app, _h

    seen: dict[str, Any] = {}

    def fake_migrate(client, ledger, tenant, *, cutover_date, currency="USD", coa_category=None):
        seen["cutover"] = cutover_date
        return qm.MigrationResult(True, 54, opening_entry_id="t:1")

    def fake_refresh(client, ledger, tenant, *, as_of):
        seen["refresh_as_of"] = as_of
        return MirrorResult(True, accounts_moved=12)

    monkeypatch.setattr(qm, "migrate_qbo_to_ledger", fake_migrate)
    monkeypatch.setattr(mirror_mod, "refresh_mirror", fake_refresh)

    app = _connected_app(ledger=LedgerClient(RecordingLedgerTransport()))
    today = date.fromtimestamp(app._session_clock())  # noqa: SLF001 - the app's own clock
    r = app.handle(Request("POST", "/t/acme/connect/qbo/migrate", _h("owner@acme.com")))

    assert dict(r.extra_headers)["Location"] == "/t/acme/connect?migrate=ok"
    assert seen["cutover"] == (today.replace(day=1) - timedelta(days=1)).isoformat()
    assert seen["refresh_as_of"] == today.isoformat(), "the month so far comes straight across"
