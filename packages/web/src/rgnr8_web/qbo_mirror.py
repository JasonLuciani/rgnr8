"""Keep RGNR8's books an exact mirror of QuickBooks during a parallel run.

While QuickBooks is the book of record, RGNR8 must not book anything of its own
judgement: every number it shows has to be QuickBooks' number. The obvious way
to "sync" — re-posting QuickBooks' invoices as RGNR8 sales and queueing its
bank lines for categorisation — double-counts both, because the go-live opening
balances already contain them. So a refresh does something narrower and exact:

    read QuickBooks' trial balance as of today (complete, checked against its
    own TOTAL), read RGNR8's, and post ONE balanced entry that moves every
    account by exactly the difference.

Afterwards the two trial balances are compared again, account by account, and
anything that is not equal to the cent is reported as a failure. If nothing
changed in QuickBooks there is nothing to post. Every catch-up is an ordinary
append-only journal entry, dated today and marked as coming from QuickBooks, so
the history of what moved and when stays inspectable — nothing is rewritten.

A refresh is refused the moment the ledger holds an entry that did not come from
QuickBooks. Bringing the books back into line with QuickBooks would then undo
work done in RGNR8, silently, and that is a cutover decision for a person to
make, not something a "Refresh" button should do.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .golive import subtype_for_account_type

SOURCE = "quickbooks"


@dataclass
class MirrorResult:
    ok: bool
    accounts_moved: int = 0
    accounts_created: int = 0
    moved_minor: int = 0
    entry_id: str = ""
    up_to_date: bool = False
    error: str = ""
    mismatches: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if not self.ok:
            return f"refresh failed: {self.error}"
        if self.up_to_date:
            return "already matches QuickBooks to the cent — nothing to bring across"
        new = f", {self.accounts_created} new account(s)" if self.accounts_created else ""
        return (f"brought {self.accounts_moved} account(s) up to date with QuickBooks{new}; "
                f"the books now match QuickBooks to the cent")


def _int(v: object) -> int:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0


def _ledger_balances(ledger: Any, tenant: str, as_of: str) -> "dict[str, int] | str":
    res = ledger.trial_balance(tenant, to=as_of)
    if not res.ok:
        return f"could not read RGNR8's trial balance: {res.error()}"
    rows = res.body.get("rows")
    out: dict[str, int] = {}
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict):
            out[str(r.get("code"))] = _int(r.get("debit_minor")) - _int(r.get("credit_minor"))
    return out


def _foreign_entries(ledger: Any, tenant: str) -> "list[str] | str":
    res = ledger.entries(tenant)
    if not res.ok:
        return f"could not read RGNR8's journal: {res.error()}"
    raw = res.body.get("entries")
    foreign = []
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, dict) and str(e.get("source") or "") != SOURCE:
            foreign.append(f"{e.get('date', '')} {e.get('memo') or e.get('id', '')}".strip())
    return foreign


def refresh_mirror(client: Any, ledger: Any, tenant: str, *, as_of: str) -> MirrorResult:
    """Bring RGNR8's books to QuickBooks' trial balance as of `as_of`, exactly."""
    foreign = _foreign_entries(ledger, tenant)
    if isinstance(foreign, str):
        return MirrorResult(False, error=foreign)
    if foreign:
        return MirrorResult(False, error=(
            f"RGNR8 holds {len(foreign)} entr{'y' if len(foreign) == 1 else 'ies'} of its own "
            f"(first: {foreign[0]}). Matching QuickBooks now would undo them, so nothing "
            "was changed. Moving fully onto RGNR8 is a cutover step, not a refresh."))

    try:
        qbo_rows = client.trial_balance(as_of=as_of)
    except Exception as exc:  # noqa: BLE001 — a live API failure, reported softly
        return MirrorResult(False, error=f"could not read the QuickBooks trial balance: {exc}")
    target: dict[str, int] = {}
    meta: dict[str, dict[str, object]] = {}
    for r in qbo_rows:
        code = str(r.get("code", "")).strip()
        target[code] = target.get(code, 0) + _int(r.get("debit_minor")) - _int(r.get("credit_minor"))
        meta[code] = r
    if sum(target.values()) != 0:
        return MirrorResult(False, error=(
            f"the trial balance read from QuickBooks is out of balance by "
            f"{abs(sum(target.values())) / 100:,.2f}, so some rows did not come across. "
            "Nothing was changed."))

    accounts = ledger.accounts(tenant)
    if not accounts.ok:
        return MirrorResult(False, error=f"could not read RGNR8's chart: {accounts.error()}")
    known = {str(a.get("code")) for a in accounts.body.get("accounts") or [] if isinstance(a, dict)}

    created = 0
    for code, row in meta.items():
        if code in known or target.get(code, 0) == 0:
            continue
        subtype = subtype_for_account_type(str(row.get("account_type", "")))
        if subtype is None:
            return MirrorResult(False, error=(
                f"QuickBooks has a new account {row.get('name')!r} ({code}) of a type RGNR8 "
                f"can't place ({row.get('account_type')!r}). Nothing was changed."))
        res = ledger.create_account(tenant, code, str(row.get("name") or code), subtype=subtype)
        if not res.ok:
            return MirrorResult(False, error=f"could not add account {code}: {res.error()}")
        created += 1

    ours = _ledger_balances(ledger, tenant, as_of)
    if isinstance(ours, str):
        return MirrorResult(False, error=ours)

    deltas = {code: target.get(code, 0) - ours.get(code, 0)
              for code in set(target) | set(ours)}
    deltas = {c: d for c, d in deltas.items() if d != 0}
    if not deltas:
        return MirrorResult(True, accounts_created=created, up_to_date=True)

    lines = [{"code": c, "side": "DEBIT" if d > 0 else "CREDIT", "amount_minor": str(abs(d))}
             for c, d in sorted(deltas.items())]
    fingerprint = hashlib.sha256(
        "|".join(f"{c}:{d}" for c, d in sorted(deltas.items())).encode()).hexdigest()[:16]
    posted = ledger.post_entry(
        tenant, as_of, lines,
        memo=f"QuickBooks catch-up to {as_of}",
        idempotency_key=f"qbo-mirror:{as_of}:{fingerprint}",
        source=SOURCE,
    )
    if not posted.ok:
        return MirrorResult(False, accounts_created=created,
                            error=f"the ledger refused the catch-up entry: {posted.error()}")

    after = _ledger_balances(ledger, tenant, as_of)
    if isinstance(after, str):
        return MirrorResult(False, error=after)
    mismatches = [
        f"{meta.get(c, {}).get('name', c)}: QuickBooks {target.get(c, 0) / 100:,.2f}, "
        f"RGNR8 {after.get(c, 0) / 100:,.2f}"
        for c in sorted(set(target) | set(after)) if target.get(c, 0) != after.get(c, 0)
    ]
    entry = posted.body.get("entry") if isinstance(posted.body, dict) else None
    entry_id = str(entry.get("id")) if isinstance(entry, dict) else ""
    if mismatches:
        return MirrorResult(False, accounts_moved=len(deltas), accounts_created=created,
                            entry_id=entry_id, mismatches=mismatches,
                            error=f"{len(mismatches)} account(s) still differ from QuickBooks")
    return MirrorResult(True, accounts_moved=len(deltas), accounts_created=created,
                        moved_minor=sum(abs(d) for d in deltas.values()) // 2, entry_id=entry_id)
