"""Expenses: money already spent, recorded as it happened.

Most of what a small business spends never sees a bill. The card is swiped at
the hardware store, the software renews on the debit card, a check goes to the
plumber. A bill is the wrong tool for that: it books a debt and then a payment,
two steps for one event, and leaves "Accounts payable" holding something that
was never owed. An expense is one journal entry: each line's account is
charged, and the total comes out of the account it was paid from.

So the ledger contract doesn't change. An expense is an ordinary balanced entry
posted through the same API as the Ledger's own form; what makes it an expense
is how it is labelled: source "expense", and an idempotency key that carries the
vendor (`exp:<vendor>:<form token>`), so the list here finds its own entries and
a double-submitted form posts once.

Amounts are integer minor units throughout.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from .attachment_screens import attachment_link
from .books_screens import (
    _banner,
    _card,
    _esc,
    _minor,
    _num,
    _seq,
    account_label,
    account_options,
)
from .parallel_run import form_label
from .provenance_labels import Provenance

EXPENSE_SOURCE = "expense"
KEY_PREFIX = "exp:"
LINES = 4

# What an expense can be paid from: money the business holds, or a card it owes.
PAID_FROM_SUBTYPES = ("BANK", "CASH", "CREDIT_CARD")

# Every field the form sends, so a refused submission comes back filled in.
FORM_FIELDS = (
    "date", "vendor_id", "vendor", "paid_from", "total", "memo", "receipt", "receipt_name",
    *(f"{f}{i}" for i in range(1, LINES + 1) for f in ("code", "desc", "amount")),
)


def expense_key(vendor_id: str, token: str) -> str:
    """The idempotency key: who was paid, and which form submission this is."""
    return f"{KEY_PREFIX}{vendor_id}:{token}"


def fallback_token(date: str, vendor_id: str, paid_from: str,
                   lines: list[tuple[str, int]]) -> str:
    """A token for a caller that sent none: the same expense twice is one expense."""
    raw = "|".join([date, vendor_id, paid_from, *(f"{c}:{a}" for c, a in lines)])
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def vendor_of(entry: Mapping[str, object]) -> str:
    """The vendor id an expense's key carries, or "" for any other entry."""
    key = str(entry.get("idempotency_key") or "")
    if not key.startswith(KEY_PREFIX):
        return ""
    return key[len(KEY_PREFIX):].rsplit(":", 1)[0]


def is_expense(entry: Mapping[str, object]) -> bool:
    return (str(entry.get("source") or "") == EXPENSE_SOURCE
            and str(entry.get("idempotency_key") or "").startswith(KEY_PREFIX))


def paid_from_accounts(accounts: Mapping[str, object]) -> list[Mapping[str, object]]:
    return [a for a in _seq(accounts.get("accounts")) if isinstance(a, Mapping)
            and str(a.get("subtype")) in PAID_FROM_SUBTYPES and a.get("active", True)]


def _code_of(account_id: object) -> str:
    """Entries name accounts by id ("acct:1000"); the chart names them by code."""
    text = str(account_id or "")
    return text[len("acct:"):] if text.startswith("acct:") else text


def recent_expenses(
    entries: Mapping[str, object], accounts: Mapping[str, object],
    vendors: Mapping[str, object], *, limit: int = 25,
    id_codes: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    """The expenses this form recorded, newest first, ready to list."""
    names = {str(p.get("id")): str(p.get("name")) for p in _seq(vendors.get("parties"))
             if isinstance(p, Mapping)}
    chart = {str(a.get("code")): a for a in _seq(accounts.get("accounts"))
             if isinstance(a, Mapping)}
    listed = [e for e in _seq(entries.get("entries")) if isinstance(e, Mapping)]
    reversed_ids = {str(e.get("idempotency_key"))[len("reverse:"):] for e in listed
                    if str(e.get("idempotency_key") or "").startswith("reverse:")}
    rows: list[dict[str, object]] = []
    for e in listed:
        if not is_expense(e):
            continue
        lines = [ln for ln in _seq(e.get("lines")) if isinstance(ln, Mapping)]
        amount = sum(_minor(ln.get("amount_minor")) or 0 for ln in lines
                     if ln.get("side") == "DEBIT")
        credit = next((ln for ln in lines if ln.get("side") == "CREDIT"), None)
        code = _code_of(credit.get("account_id")) if credit else ""
        acct = chart.get(code)
        vendor = vendor_of(e)
        # The entry's memo leads with the vendor so the general ledger reads
        # well; here the vendor has its own column, so only the note is left.
        memo = str(e.get("memo") or "")
        for name in {names.get(vendor, ""), vendor} - {""}:
            if memo == name:
                memo = ""
            elif memo.startswith(f"{name} — "):
                memo = memo[len(name) + 3:]
        rows.append({
            "id": str(e.get("id")),
            "date": str(e.get("date") or ""),
            "sequence": _minor(e.get("sequence")) or 0,
            "vendor": names.get(vendor, vendor),
            "paid_from": account_label(code, acct.get("name"), id_codes) if acct else code,
            "amount_minor": amount,
            "memo": memo,
            "reversed": str(e.get("id")) in reversed_ids,
        })
    rows.sort(key=lambda r: (str(r["date"]), int(str(r["sequence"]))), reverse=True)
    return rows[:limit]


def _list_card(tenant: str, rows: list[dict[str, object]],
               counts: Mapping[str, object]) -> str:
    body = []
    for r in rows:
        clip = attachment_link(tenant, "entry", str(r["id"]),
                               _minor(counts.get(str(r["id"]), 0)) or 0)
        state = (' <span class="muted small">reversed</span>' if r["reversed"] else "")
        body.append(
            f"<tr><td class='muted'>{_esc(r['date'])}</td>"
            f"<td>{_esc(r['vendor']) or '—'}{state}</td>"
            f"<td class='muted'>{_esc(r['paid_from'])}</td>"
            f"{_num(r['amount_minor'])}"
            f"<td class='muted'>{_esc(r['memo'])}</td><td>{clip}</td></tr>"
        )
    empty = ('<tr><td colspan="6" class="empty">No expenses recorded here yet.</td></tr>')
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Date</th><th>Paid to</th>'
        '<th>Paid from</th><th class="num">Amount</th><th>Memo</th><th></th>'
        "</tr></thead><tbody>" + ("".join(body) or empty) + "</tbody></table></div>"
    )
    return _card("Recent expenses", table, provenance=Provenance.POSTED)


def _vendor_fields(vendors: Mapping[str, object], prefill: Mapping[str, str]) -> str:
    chosen = prefill.get("vendor_id", "")
    opts = "".join(
        f'<option value="{_esc(p.get("id"))}"'
        f'{" selected" if str(p.get("id")) == chosen else ""}>{_esc(p.get("name"))}</option>'
        for p in _seq(vendors.get("parties")) if isinstance(p, Mapping)
    )
    typed = (f'<input name="vendor" value="{_esc(prefill.get("vendor", ""))}" '
             'placeholder="Type a new vendor">')
    if not opts:
        return f"<div><label>Paid to</label>{typed}</div>"
    return (
        '<div><label>Paid to</label><select name="vendor_id">'
        f'<option value="">New vendor (type below)</option>{opts}</select>'
        f'<div style="margin-top:6px">{typed}</div></div>'
    )


def _form_card(
    tenant: str, accounts: Mapping[str, object], vendors: Mapping[str, object],
    dimensions: Mapping[str, object], *, token: str, today: str,
    prefill: Mapping[str, str], id_codes: frozenset[str],
) -> str:
    sources = paid_from_accounts(accounts)
    if not sources:
        return _card("Record an expense", '<p class="muted">Add a bank or credit card '
                     "account to the chart first: an expense is paid from one.</p>")
    from .dimension_screens import dimension_selects

    chosen_source = prefill.get("paid_from", "")
    source_opts = "".join(
        f'<option value="{_esc(a.get("code"))}"'
        f'{" selected" if str(a.get("code")) == chosen_source else ""}>'
        f'{_esc(account_label(a.get("code"), a.get("name"), id_codes))}</option>'
        for a in sources
    )
    dims = dimension_selects(dimensions)
    lines = []
    for i in range(1, LINES + 1):
        opts = account_options(accounts, id_codes, selected=prefill.get(f"code{i}", ""),
                               types=("EXPENSE",))
        line_dims = (f'<div class="grid3">{dimension_selects(dimensions, suffix=str(i))}</div>'
                     if dims else "")
        lines.append(
            '<div class="grid3">'
            f'<div><label>Expense account</label><select name="code{i}">'
            f'<option value="">—</option>{opts}</select></div>'
            f'<div><label>Description</label><input name="desc{i}" '
            f'value="{_esc(prefill.get(f"desc{i}", ""))}" placeholder="What was it?"></div>'
            f'<div><label>Amount</label><input name="amount{i}" '
            f'value="{_esc(prefill.get(f"amount{i}", ""))}" placeholder="0.00" '
            'inputmode="decimal"></div>'
            f"</div>{line_dims}"
        )
    receipt = prefill.get("receipt", "")
    if receipt:
        evidence = (
            f'<input type="hidden" name="receipt" value="{_esc(receipt)}">'
            f'<input type="hidden" name="receipt_name" value="{_esc(prefill.get("receipt_name", ""))}">'
            '<p class="note">The receipt you kept '
            f'(<strong>{_esc(prefill.get("receipt_name", "") or "receipt")}</strong>) is '
            "attached to this expense when you record it.</p>"
        )
    else:
        evidence = ('<div><label>Receipt (optional)</label>'
                    '<input type="file" name="file" accept="image/*,application/pdf"></div>')
    return _card(
        "Record an expense",
        f'<form method="post" enctype="multipart/form-data" action="/t/{_esc(tenant)}/expenses"'
        f'{form_label("Expense", "+ New → Expense")}>'
        f'<input type="hidden" name="token" value="{_esc(token)}">'
        '<div class="grid3">'
        f'<div><label>Date</label><input name="date" type="date" '
        f'value="{_esc(prefill.get("date", "") or today)}" required></div>'
        + _vendor_fields(vendors, prefill)
        + f'<div><label>Paid from</label><select name="paid_from">{source_opts}</select></div>'
        "</div>"
        + "".join(lines)
        + '<div class="grid2">'
        f'<div><label>Total on the receipt (optional)</label><input name="total" '
        f'value="{_esc(prefill.get("total", ""))}" placeholder="0.00" inputmode="decimal"></div>'
        f'<div><label>Memo</label><input name="memo" value="{_esc(prefill.get("memo", ""))}" '
        'placeholder="Optional note"></div>'
        "</div>"
        + evidence
        + '<p class="note">An expense is money already spent, by card, debit or check: '
        "each line is charged to its account and the total comes out of the account it "
        "was paid from, all at once. For something you will pay later, enter a bill "
        "instead. Give the total from the receipt and the lines must add up to it.</p>"
        '<button class="btn" type="submit">Record expense</button></form>',
    )


def render_expenses(
    tenant: str,
    *,
    accounts: Mapping[str, object],
    vendors: Mapping[str, object],
    dimensions: Mapping[str, object],
    rows: list[dict[str, object]],
    counts: Mapping[str, object],
    can_post: bool,
    token: str,
    today: str,
    prefill: Mapping[str, str] | None = None,
    message: str = "",
    error: str = "",
    id_codes: frozenset[str] = frozenset(),
) -> str:
    out = ""
    if message:
        out += _banner("good", message)
    if error:
        out += _banner("warn", error)
    out += _list_card(tenant, rows, counts)
    if can_post:
        out += _form_card(tenant, accounts, vendors, dimensions, token=token, today=today,
                          prefill=prefill or {}, id_codes=id_codes)
    return out


__all__ = [
    "EXPENSE_SOURCE", "FORM_FIELDS", "LINES", "PAID_FROM_SUBTYPES", "expense_key",
    "fallback_token", "is_expense", "paid_from_accounts", "recent_expenses",
    "render_expenses", "vendor_of",
]
