"""The QuickBooks → RGNR8 ledger migration: pull the trial balance, build the
go-live/1 request, and hand it to the ledger (which seeds the chart of accounts +
opening balances). Proven against fakes — no network, no real ledger."""

from __future__ import annotations

from typing import Any

from rgnr8_web import LedgerResponse
from rgnr8_web.qbo_migrate import migrate_qbo_to_ledger


class FakeQbo:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def trial_balance(self, *, as_of: str = "") -> list[dict[str, Any]]:
        return list(self._rows)


class FakeLedger:
    def __init__(self, *, ok: bool = True) -> None:
        self._ok = ok
        self.request: dict[str, Any] | None = None

    def go_live(self, tenant: str, request: dict[str, Any]) -> LedgerResponse:
        self.request = request
        if not self._ok:
            return LedgerResponse(409, {"error": "period already locked"})
        return LedgerResponse(201, {"opening_entry_id": "OB-1"})


ROWS = [
    {"code": "1000", "name": "Checking", "account_type": "Bank",
     "debit_minor": 120100, "credit_minor": 0},
    {"code": "3000", "name": "Owner Equity", "account_type": "Equity",
     "debit_minor": 0, "credit_minor": 120100},
]


def test_migration_builds_go_live_and_seeds_the_ledger() -> None:
    ledger = FakeLedger()
    res = migrate_qbo_to_ledger(FakeQbo(ROWS), ledger, "acme", cutover_date="2026-08-25")
    assert res.ok and res.accounts == 2 and res.opening_entry_id == "OB-1"
    req = ledger.request
    assert req is not None
    assert req["contract"] == "go-live/1" and req["source_system"] == "quickbooks"
    assert req["cutover_date"] == "2026-08-25"
    assert {a["code"] for a in req["source_accounts"]} == {"1000", "3000"}


def test_migration_surfaces_a_ledger_refusal() -> None:
    res = migrate_qbo_to_ledger(FakeQbo(ROWS), FakeLedger(ok=False), "acme", cutover_date="2026-08-25")
    assert not res.ok and "locked" in res.error


def test_migration_reports_an_empty_trial_balance() -> None:
    res = migrate_qbo_to_ledger(FakeQbo([]), FakeLedger(), "acme", cutover_date="2026-08-25")
    assert not res.ok and "no trial-balance" in res.error


def test_migration_surfaces_an_unmappable_account() -> None:
    bad = [{"code": "9", "name": "Weird", "account_type": "Martian Asset",
            "debit_minor": 100, "credit_minor": 0}]
    res = migrate_qbo_to_ledger(FakeQbo(bad), FakeLedger(), "acme", cutover_date="2026-08-25")
    assert not res.ok and "unmappable" in res.error
