"""Bringing QuickBooks' bank and card history into RGNR8, window by window.

The mirror gives RGNR8 QuickBooks' balances, one total per account, and nothing
about when money actually moved. This copies the transactions on every bank and
credit-card account (see `rgnr8_qbo.history`) into the ledger's non-posting
history store, so the cash outlook can learn timing from real receipts and
payments, and the history doesn't stay behind in QuickBooks at cutover.

Two ways in:

- **Refresh from QuickBooks** re-reads the recent weeks (one report call), so the
  history keeps up with the books at no noticeable cost.
- **Bring over history** walks backwards a quarter at a time to two years, under
  a time budget: a web request has about 30 seconds, and a company with a lot of
  history takes several calls. What's done is kept; pressing it again carries on
  from where it stopped.

Every window is checked against QuickBooks' own running balance, and the latest
one against the books, so a line that didn't come across shows up as a
difference rather than as a quietly wrong outlook.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from rgnr8_qbo.history import CashAccount, cash_accounts, read_cash_history

from .qbo_errors import plain_qbo_error

# How far back "Bring over history" goes, and how much one report call covers.
HISTORY_MONTHS = 24
WINDOW_DAYS = 92
# A Refresh re-reads this much: enough to pick up late edits in QuickBooks.
RECENT_DAYS = 45
# What one "Bring over history" request may spend before it stops and says so.
TIME_BUDGET_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class Difference:
    account: str
    difference_minor: int
    against: str  # "QuickBooks" (its own running balance) or "the books"


@dataclass(slots=True)
class ImportResult:
    ok: bool = True
    lines: int = 0
    windows: int = 0
    covered_from: str = ""
    covered_to: str = ""
    finished: bool = False
    problem: str = ""
    differences: list[Difference] = field(default_factory=list)

    def describe(self) -> str:
        if not self.ok:
            return f"bank history wasn't brought over: {self.problem}"
        span = (f" ({self.covered_from} to {self.covered_to})"
                if self.covered_from and self.covered_to else "")
        text = f"brought over {self.lines:,} bank and card transactions{span}"
        if self.differences:
            d = self.differences[0]
            text += (f"; {d.account} is {abs(d.difference_minor) / 100:,.2f} off "
                     f"{d.against}")
            if len(self.differences) > 1:
                text += f" (and {len(self.differences) - 1} more)"
        if not self.finished and self.ok:
            text += "; there's more: press Bring over history again to continue"
        return text


def _books_balances(ledger: Any, tenant: str) -> dict[str, int]:
    """Each account's balance in the books, debit-positive, by code."""
    tb = ledger.trial_balance(tenant)
    if not tb.ok:
        return {}
    out: dict[str, int] = {}
    rows = tb.body.get("rows")
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict):
            out[str(r.get("code"))] = (int(str(r.get("debit_minor") or 0))
                                       - int(str(r.get("credit_minor") or 0)))
    return out


def _coverage(ledger: Any, tenant: str) -> tuple[str, str]:
    res = ledger.bank_history(tenant, lines=False)
    if not res.ok:
        return "", ""
    return str(res.body.get("covered_from") or ""), str(res.body.get("covered_to") or "")


def _import_window(client: Any, ledger: Any, tenant: str, start: date, end: date,
                   accounts: list[CashAccount], result: ImportResult, *,
                   books: "dict[str, int] | None", imported_at: str) -> bool:
    """Read and store one window; False (with `result.problem` set) on failure."""
    try:
        history = read_cash_history(client, start.isoformat(), end.isoformat(), accounts)
    except Exception as exc:  # noqa: BLE001 - a live API failure, said plainly
        result.ok, result.problem = False, plain_qbo_error(exc)
        return False
    res = ledger.save_bank_history(tenant, history.payload(imported_at))
    if not res.ok:
        result.ok, result.problem = False, res.error()
        return False
    result.lines += len(history.lines)
    result.windows += 1
    for w in history.windows:
        if w.has_balances and w.difference_minor:
            result.differences.append(Difference(w.account.name, w.difference_minor, "QuickBooks"))
        if books is not None and w.has_balances and w.account.code in books:
            gap = w.closing_minor - books[w.account.code]
            if gap:
                result.differences.append(Difference(w.account.name, gap, "the books"))
    return True


def refresh_recent(client: Any, ledger: Any, tenant: str, today: date, *,
                   imported_at: str) -> ImportResult:
    """Re-read the last few weeks, as part of a Refresh. One report call."""
    result = ImportResult()
    try:
        accounts = cash_accounts(client)
    except Exception as exc:  # noqa: BLE001
        return ImportResult(ok=False, problem=plain_qbo_error(exc))
    _, covered_to = _coverage(ledger, tenant)
    start = today - timedelta(days=RECENT_DAYS)
    if covered_to:
        # Close any gap since the last import, but never more than one window.
        last = date.fromisoformat(covered_to)
        start = max(min(start, last - timedelta(days=7)), today - timedelta(days=WINDOW_DAYS))
    if _import_window(client, ledger, tenant, start, today, accounts, result,
                      books=_books_balances(ledger, tenant), imported_at=imported_at):
        result.covered_from, result.covered_to = _coverage(ledger, tenant)
        result.finished = True
    return result


def bring_over_history(client: Any, ledger: Any, tenant: str, today: date, *,
                       imported_at: str, months: int = HISTORY_MONTHS,
                       budget_seconds: float = TIME_BUDGET_SECONDS,
                       clock: Callable[[], float] = time.monotonic) -> ImportResult:
    """Fill the history back to `months` ago, newest first, until done or out of time."""
    result = ImportResult()
    started = clock()
    try:
        accounts = cash_accounts(client)
    except Exception as exc:  # noqa: BLE001
        return ImportResult(ok=False, problem=plain_qbo_error(exc))
    floor = (today.replace(day=1) - timedelta(days=months * 31)).replace(day=1)
    covered_from, covered_to = _coverage(ledger, tenant)

    # The newest window first: it is the one the books can be checked against.
    if not covered_to or date.fromisoformat(covered_to) < today:
        start = (max(date.fromisoformat(covered_to) - timedelta(days=7),
                     today - timedelta(days=WINDOW_DAYS))
                 if covered_to else today - timedelta(days=WINDOW_DAYS))
        if not _import_window(client, ledger, tenant, start, today, accounts, result,
                              books=_books_balances(ledger, tenant), imported_at=imported_at):
            return result
        covered_from, covered_to = _coverage(ledger, tenant)

    edge = date.fromisoformat(covered_from) if covered_from else today
    while edge > floor:
        if clock() - started > budget_seconds:
            break
        end = edge - timedelta(days=1)
        start = max(end - timedelta(days=WINDOW_DAYS - 1), floor)
        if not _import_window(client, ledger, tenant, start, end, accounts, result,
                              books=None, imported_at=imported_at):
            return result
        edge = start
    result.covered_from, result.covered_to = _coverage(ledger, tenant)
    result.finished = edge <= floor
    return result
