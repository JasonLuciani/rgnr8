"""Every bank and credit-card transaction QuickBooks holds, line by line.

The mirror keeps RGNR8's books equal to QuickBooks' trial balance: one total per
account. That is enough for balances and not enough for anything about *timing*:
when customers pay, when payroll and rent leave, how spending moves through the
month. This reads the transactions themselves.

The source is QuickBooks' General Ledger report, filtered to bank and credit-card
accounts. One report, rather than a query per entity, because the entity API has
no single list of "everything that touched this account": expenses, checks,
deposits, customer payments, bill payments, transfers, journal entries, sales
receipts, refunds and paychecks are all different entities, and payroll
paychecks are not exposed as entities at all. The General Ledger has every one
of them, with the running balance QuickBooks itself computes, which is what each
window is checked against.

Amounts come back signed from the account's own point of view: money in positive,
money out negative, on bank and card accounts alike (a card charge is money out).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import QboApiError, _to_minor

# QuickBooks' account types that hold cash or spend it.
BANK_TYPES = frozenset({"Bank"})
CARD_TYPES = frozenset({"Credit Card"})

# The General Ledger columns this reads, by QuickBooks' column key, with the title
# QuickBooks gives each in its default layout (used when a key is absent).
_COLUMNS = {
    "tx_date": "Date",
    "txn_type": "Transaction Type",
    "doc_num": "Num",
    "name": "Name",
    "memo": "Memo/Description",
    "split_acc": "Split",
    "subt_nat_amount": "Amount",
    "rbal_nat_amount": "Balance",
}
_COLUMN_PARAM = ",".join(_COLUMNS)

# Transaction types that are spending wherever they appear. Used to tell which
# way QuickBooks signs an account's amounts: on a bank account an expense is
# negative; on a credit card QuickBooks shows a charge as an increase in what is
# owed, i.e. positive. Reading the sign off the data, per account and window,
# keeps this right whichever way a given company's report comes back.
_SPEND_TYPES = frozenset({
    "Expense", "Check", "Credit Card Expense", "Cash Expense", "Purchase",
})


@dataclass(frozen=True, slots=True)
class CashAccount:
    qbo_id: str
    code: str          # as the books know it: the account number, else QuickBooks' id
    name: str
    kind: str          # "bank" | "card"


@dataclass(frozen=True, slots=True)
class CashLine:
    id: str
    account: CashAccount
    date: str
    txn_type: str
    number: str
    name: str
    memo: str
    split: str
    amount_minor: int  # signed: + money in, - money out


@dataclass(frozen=True, slots=True)
class AccountWindow:
    account: CashAccount
    opening_minor: int          # QuickBooks' balance before the window, our sign
    movement_minor: int         # the sum of the lines
    closing_minor: int          # QuickBooks' running balance at the end, our sign
    has_balances: bool          # whether the report gave opening and closing

    @property
    def difference_minor(self) -> int:
        return self.opening_minor + self.movement_minor - self.closing_minor


@dataclass(frozen=True, slots=True)
class CashHistory:
    start: str
    end: str
    lines: tuple[CashLine, ...]
    windows: tuple[AccountWindow, ...] = field(default=())

    def payload(self, imported_at: str) -> dict[str, object]:
        """The shape the ledger's bank-history import takes."""
        return {
            "start": self.start, "end": self.end, "imported_at": imported_at,
            "lines": [{
                "id": ln.id, "account_code": ln.account.code, "account_name": ln.account.name,
                "account_kind": ln.account.kind, "date": ln.date, "txn_type": ln.txn_type,
                "number": ln.number, "name": ln.name, "memo": ln.memo, "split": ln.split,
                "amount_minor": str(ln.amount_minor),
            } for ln in self.lines],
            "tie_out": [{
                "account_code": w.account.code, "account_name": w.account.name,
                "opening_minor": str(w.opening_minor), "movement_minor": str(w.movement_minor),
                "closing_minor": str(w.closing_minor),
            } for w in self.windows if w.has_balances],
        }


def cash_accounts(client: Any) -> list[CashAccount]:
    """The company's bank and credit-card accounts, inactive ones included (an
    account closed last spring still has last spring's transactions)."""
    out: list[CashAccount] = []
    for a in client.chart_of_accounts(include_inactive=True):
        kind = ("bank" if a["account_type"] in BANK_TYPES
                else "card" if a["account_type"] in CARD_TYPES else "")
        if kind:
            out.append(CashAccount(qbo_id=a["id"], code=a["code"], name=a["name"], kind=kind))
    return out


def _column_index(report: dict[str, Any]) -> dict[str, int]:
    """Where each column this reads sits, by key, falling back to its title."""
    cols = report.get("Columns")
    raw = cols.get("Column") if isinstance(cols, dict) else None
    index: dict[str, int] = {}
    titles = {title: key for key, title in _COLUMNS.items()}
    for i, c in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(c, dict):
            continue
        key = ""
        for m in c.get("MetaData") or []:
            if isinstance(m, dict) and m.get("Name") == "ColKey":
                key = str(m.get("Value", ""))
        if key not in _COLUMNS:
            key = titles.get(str(c.get("ColTitle", "")).strip(), "")
        if key and key not in index:
            index[key] = i
    missing = {"tx_date", "subt_nat_amount"} - index.keys()
    if missing:
        raise QboApiError(f"the General Ledger report has no {', '.join(sorted(missing))} column")
    return index


def _cell(cols: list[Any], index: dict[str, int], key: str) -> dict[str, Any]:
    i = index.get(key)
    cell = cols[i] if i is not None and i < len(cols) else None
    return cell if isinstance(cell, dict) else {}


def _text(cols: list[Any], index: dict[str, int], key: str) -> str:
    return str(_cell(cols, index, key).get("value", "") or "").strip()


@dataclass
class _Acc:
    """One account's rows while the report is walked."""

    opening: "int | None" = None
    last_balance: "int | None" = None
    rows: list[tuple[str, str, str, str, str, str, int, str]] = field(default_factory=list)


def _walk(container: Any, account_id: str, index: dict[str, int],
          found: dict[str, _Acc], wanted: set[str]) -> None:
    rows = container.get("Row") if isinstance(container, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        here = account_id
        header = row.get("Header")
        hcols = header.get("ColData") if isinstance(header, dict) else None
        if isinstance(hcols, list) and hcols and isinstance(hcols[0], dict):
            hid = str(hcols[0].get("id", "") or "").strip()
            if hid:
                here = hid
        cols = row.get("ColData")
        if isinstance(cols, list) and here in wanted:
            acc = found.setdefault(here, _Acc())
            first = _text(cols, index, "tx_date")
            balance_raw = _text(cols, index, "rbal_nat_amount")
            if first.lower().startswith("beginning balance"):
                acc.opening = _to_minor(balance_raw) if balance_raw else _to_minor(
                    _text(cols, index, "subt_nat_amount"))
                acc.last_balance = acc.opening
            elif len(first) == 10 and first[4] == "-" and first[7] == "-":
                txn = _cell(cols, index, "txn_type")
                acc.rows.append((
                    first, str(txn.get("value", "") or "").strip(), str(txn.get("id", "") or "").strip(),
                    _text(cols, index, "doc_num"), _text(cols, index, "name"),
                    _text(cols, index, "memo"), _to_minor(_text(cols, index, "subt_nat_amount")),
                    _text(cols, index, "split_acc"),
                ))
                if balance_raw:
                    acc.last_balance = _to_minor(balance_raw)
        nested = row.get("Rows")
        if isinstance(nested, dict):
            _walk(nested, here, index, found, wanted)


def _sign(kind: str, rows: list[tuple[str, str, str, str, str, str, int, str]]) -> int:
    """+1 when QuickBooks' amounts on this account already read as money in /
    money out, -1 when they need flipping (see `_SPEND_TYPES`)."""
    spend = sum(r[6] for r in rows if r[1] in _SPEND_TYPES)
    if spend > 0:
        return -1
    if spend < 0:
        return 1
    return 1 if kind == "bank" else -1


def read_cash_history(client: Any, start: str, end: str,
                      accounts: "list[CashAccount] | None" = None) -> CashHistory:
    """Every bank and card line dated `start` to `end` (ISO dates), with each
    account's opening and closing balance as QuickBooks' own report gives them."""
    accounts = accounts if accounts is not None else cash_accounts(client)
    if not accounts:
        return CashHistory(start=start, end=end, lines=())
    by_id = {a.qbo_id: a for a in accounts}
    params = {"start_date": start, "end_date": end,
              "account": ",".join(by_id), "columns": _COLUMN_PARAM}
    try:
        report = client.report("GeneralLedger", params)
    except QboApiError as exc:
        if exc.status != 400:
            raise
        # A company whose report refuses the column list still has the default
        # layout, which carries every column this reads.
        params.pop("columns")
        report = client.report("GeneralLedger", params)
    index = _column_index(report)
    found: dict[str, _Acc] = {}
    _walk(report.get("Rows"), "", index, found, set(by_id))

    lines: list[CashLine] = []
    windows: list[AccountWindow] = []
    for qbo_id, acc in sorted(found.items()):
        account = by_id[qbo_id]
        sign = _sign(account.kind, acc.rows)
        seen: dict[str, int] = {}
        movement = 0
        for date, txn_type, txn_id, number, name, memo, amount, split in acc.rows:
            base = f"{account.code}:{txn_type or 'txn'}:{txn_id or date}"
            n = seen.get(base, 0)
            seen[base] = n + 1
            signed = sign * amount
            movement += signed
            lines.append(CashLine(
                id=base if n == 0 else f"{base}:{n}", account=account, date=date,
                txn_type=txn_type, number=number, name=name, memo=memo, split=split,
                amount_minor=signed,
            ))
        has = acc.opening is not None and acc.last_balance is not None
        windows.append(AccountWindow(
            account=account, opening_minor=sign * (acc.opening or 0), movement_minor=movement,
            closing_minor=sign * (acc.last_balance or 0), has_balances=has,
        ))
    lines.sort(key=lambda ln: (ln.date, ln.id))
    return CashHistory(start=start, end=end, lines=tuple(lines), windows=tuple(windows))
