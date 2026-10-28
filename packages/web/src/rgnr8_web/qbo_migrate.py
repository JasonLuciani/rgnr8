"""Bring a connected QuickBooks company's chart of accounts + opening trial
balance into the RGNR8 ledger — the "go-live" that makes RGNR8 the system of
record so the books mirror QuickBooks and every account code resolves.

Two halves feed a connected tenant. The forecast overlay (`qbo_sync`) reads QBO
for the cash picture. This is the other half: it pulls QuickBooks' trial balance,
turns it into the go-live/1 request, and hands it to the ledger, which seeds the
chart and posts the opening-balance journal. Until this runs, the ledger has no
chart of accounts, which is why the overlay sync reports "unknown account code".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """The outcome of one QuickBooks → ledger migration."""

    ok: bool
    accounts: int
    opening_entry_id: str = ""
    error: str = ""

    def describe(self) -> str:
        if self.ok:
            tail = f", opening entry {self.opening_entry_id}" if self.opening_entry_id else ""
            return f"migrated {self.accounts} accounts from QuickBooks{tail}"
        return f"migration failed: {self.error}"


def migrate_qbo_to_ledger(
    client: Any,
    ledger: Any,
    tenant: str,
    *,
    cutover_date: str,
    currency: str = "USD",
    coa_category: str | None = None,
) -> MigrationResult:
    """Pull QuickBooks' trial balance and run the go-live against the ledger.

    Reads the connected company's trial balance (via the QBO client), normalizes
    it into go-live `source_accounts`, assembles the go-live/1 request, and posts
    it to the ledger service — which seeds the chart of accounts and the opening
    balances. Returns a `MigrationResult`; an expected data/API problem comes back
    as `ok=False` with a message rather than raising.
    """
    from .golive import (
        GoLiveError,
        build_go_live_request,
        qbo_trial_balance_to_source_accounts,
    )

    try:
        rows = client.trial_balance(as_of=cutover_date)
    except Exception as exc:  # a live QBO API/parse failure, surfaced softly
        return MigrationResult(False, 0, error=f"could not read the QuickBooks trial balance: {exc}")
    if not rows:
        return MigrationResult(False, 0, error="QuickBooks returned no trial-balance accounts")

    try:
        source_accounts = qbo_trial_balance_to_source_accounts(rows)
    except GoLiveError as exc:
        return MigrationResult(False, 0, error=str(exc))

    # QuickBooks' trial balance balances on its own. If the rows read from it
    # do not, something was lost on the way, and go-live would cover the gap
    # with an Opening Balance Equity plug that makes the books *look* right.
    # That is how a first migration came across $206,556.77 of revenue short
    # and still showed "in balance". Refuse instead, and say by how much.
    net = sum(int(str(a["balance_minor"])) for a in source_accounts)
    if net != 0:
        return MigrationResult(
            False, len(source_accounts),
            error=(f"the trial balance read from QuickBooks is out of balance by "
                   f"{abs(net) / 100:,.2f}, so some rows did not come across. Nothing was "
                   "imported."))

    try:
        request = build_go_live_request(
            tenant, "quickbooks", cutover_date, source_accounts,
            currency=currency, coa_category=coa_category,
        )
    except GoLiveError as exc:
        return MigrationResult(False, 0, error=str(exc))

    res = ledger.go_live(tenant, request)
    if not res.ok:
        return MigrationResult(False, len(source_accounts), error=res.error())

    body = res.body if isinstance(res.body, dict) else {}
    opening = str(body.get("opening_entry_id") or body.get("openingEntryId") or "")
    return MigrationResult(True, len(source_accounts), opening_entry_id=opening)
