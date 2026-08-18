"""General ledger detail and budget vs actual.

The **general ledger** is the report you open when a number on the trial balance
doesn't look right. Every posting, in order, per account, with the running
balance beside it — so "why is cash $12,251?" has an answer you can point at
rather than an answer you have to trust.

**Budget vs actual** hinges on one word. Spending less than you planned is good;
earning less is not. A report that treats every under-run as a win congratulates
a business for missing its revenue target, so favourable and unfavourable are
coloured from the account's class, not from the sign of the number.

Amounts arrive as integer minor-unit strings and are formatted exactly.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _minor, _num, _seq, money


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


def render_general_ledger(
    tenant: str, data: Mapping[str, object], *, frm: str = "", to: str = ""
) -> str:
    """Every posting, grouped by account, with a running balance."""
    blocks = []
    for a in _seq(data.get("accounts")):
        if not isinstance(a, Mapping):
            continue
        rows = []
        for r in _seq(a.get("rows")):
            if not isinstance(r, Mapping):
                continue
            rows.append(
                f"<tr><td class='muted'>{_esc(r.get('date'))}</td>"
                f"<td>{_esc(r.get('memo')) or '—'}</td>"
                f"<td class='muted' style='font-size:12px'>{_esc(r.get('entry_id'))}</td>"
                f"{_num(r.get('debit_minor'))}{_num(r.get('credit_minor'))}"
                f"{_num(r.get('balance_minor'))}</tr>"
            )
        table = (
            '<div class="table-scroll"><table><thead><tr><th>Date</th><th>Memo</th>'
            '<th>Entry</th><th class="num">Debit</th><th class="num">Credit</th>'
            '<th class="num">Balance</th></tr></thead><tbody>'
            f'<tr class="muted"><td colspan="5">Opening balance</td>'
            f'{_num(a.get("opening_minor"))}</tr>'
            + "".join(rows)
            + '<tr style="font-weight:700"><td colspan="3">Closing balance</td>'
            + _num(a.get("total_debit_minor")) + _num(a.get("total_credit_minor"))
            + _num(a.get("closing_minor")) + "</tr></tbody></table></div>"
        )
        blocks.append(_card(f"{_esc(a.get('code'))} — {_esc(a.get('name'))}", table))

    if not blocks:
        blocks.append(_card(
            "General Ledger",
            '<p class="muted">Nothing was posted in this period.</p>',
        ))

    balanced = (
        str(data.get("total_debit_minor")) == str(data.get("total_credit_minor"))
    )
    head = _banner(
        "good" if balanced else "warn",
        "Debits equal credits across every account"
        if balanced
        else "OUT OF BALANCE — this should never happen; contact support",
    )
    return head + _filter_form(tenant, frm, to) + "".join(blocks)


def _filter_form(tenant: str, frm: str, to: str) -> str:
    return _card(
        "General Ledger",
        f'<form method="get" action="/t/{_esc(tenant)}/books/gl">'
        '<div class="grid3">'
        f'<div><label>From</label><input name="from" value="{_esc(frm)}" '
        'placeholder="YYYY-MM-DD"></div>'
        f'<div><label>To</label><input name="to" value="{_esc(to)}" '
        'placeholder="YYYY-MM-DD"></div>'
        '<div><label>Only these accounts (codes, comma separated)</label>'
        '<input name="codes" placeholder="6300,6500"></div>'
        "</div>"
        '<p class="note">Anything before the window is folded into each account\'s '
        "opening balance rather than dropped, so the ledger still reconciles.</p>"
        '<button class="btn" type="submit">Show</button></form>',
        f'<a class="btn-link" href="/t/{_esc(tenant)}/books">Back to books</a>',
    )


# --- budget vs actual --------------------------------------------------------

def render_budget(
    tenant: str,
    period: str,
    data: Mapping[str, object],
    accounts: Mapping[str, object],
    *,
    can_post: bool,
    message: str = "",
    error: str = "",
) -> str:
    """The plan against what happened, with favourable read from the account."""
    rows = []
    for line in _seq(data.get("lines")):
        if not isinstance(line, Mapping):
            continue
        variance = _minor(line.get("variance_minor")) or 0
        favorable = bool(line.get("favorable"))
        if variance == 0:
            chip = '<span class="muted" style="font-size:12px">on plan</span>'
        else:
            colour = "var(--rg-sage)" if favorable else "var(--rg-risk,#b4462f)"
            word = "better than planned" if favorable else "worse than planned"
            chip = (
                f'<span style="color:{colour};font-weight:700;font-size:12px">'
                f"{_esc(word)}</span>"
            )
        rows.append(
            f"<tr><td>{_esc(line.get('code'))}</td><td>{_esc(line.get('name'))}</td>"
            f"{_num(line.get('budget_minor'))}{_num(line.get('actual_minor'))}"
            f"{_num(line.get('variance_minor'))}<td>{chip}</td></tr>"
        )
    empty = (
        '<tr><td colspan="6" class="muted" style="text-align:center;padding:24px">'
        "Nothing budgeted or posted for this period yet.</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Code</th><th>Account</th>'
        '<th class="num">Budget</th><th class="num">Actual</th>'
        '<th class="num">Variance</th><th></th></tr></thead><tbody>'
        + ("".join(rows) or empty)
        + '<tr style="font-weight:700"><td colspan="2">Totals</td>'
        + _num(data.get("total_budget_minor")) + _num(data.get("total_actual_minor"))
        + _num(data.get("total_variance_minor")) + "<td></td></tr>"
        + "</tbody></table></div>"
    )

    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    budgeted = _minor(data.get("budgeted_accounts")) or 0
    if budgeted == 0:
        head += _banner(
            "warn",
            f"No budget set for {period} — the actuals below are what happened, "
            "with nothing to compare them against.",
        )
    unknown = _seq(data.get("unknown_accounts"))
    if unknown:
        head += _banner(
            "warn",
            "Budgeted against accounts that no longer exist: "
            + ", ".join(str(u) for u in unknown),
        )

    out = head + _period_form(tenant, period) + _card(
        f"Budget vs Actual — {_esc(period)}", table
    )
    if can_post:
        out += _budget_form(tenant, period, accounts, data)
    return out


def _period_form(tenant: str, period: str) -> str:
    return (
        f'<form method="get" action="/t/{_esc(tenant)}/books/budget" '
        'style="display:flex;gap:8px;align-items:end;margin:0 0 12px">'
        f'<div><label>Period</label><input name="period" value="{_esc(period)}" '
        'placeholder="YYYY-MM"></div>'
        '<button class="btn" style="margin:0" type="submit">Show</button>'
        f'<a class="btn-link" href="/t/{_esc(tenant)}/books" '
        'style="margin-left:auto">Back to books</a></form>'
    )


def _budget_form(
    tenant: str, period: str, accounts: Mapping[str, object], data: Mapping[str, object]
) -> str:
    """Pre-filled with what's already budgeted, so revising is editing, not retyping."""
    existing: dict[str, str] = {}
    for line in _seq(data.get("lines")):
        if isinstance(line, Mapping):
            n = _minor(line.get("budget_minor"))
            if n:
                existing[str(line.get("code"))] = f"{n / 100:.2f}"

    rows = []
    for a in _seq(accounts.get("accounts")):
        if not isinstance(a, Mapping):
            continue
        type_ = str(a.get("type") or "")
        # Only income and expense accounts are budgeted; you don't plan a
        # balance-sheet total, you plan what you earn and what you spend.
        if type_ not in ("REVENUE", "EXPENSE"):
            continue
        code = str(a.get("code"))
        rows.append(
            f'<div><label>{_esc(code)} — {_esc(a.get("name"))}</label>'
            f'<input name="amount_{_esc(code)}" value="{_esc(existing.get(code, ""))}" '
            'placeholder="0.00" inputmode="decimal"></div>'
        )
    if not rows:
        return ""
    grid = "".join(
        f'<div class="grid3">{"".join(rows[i:i + 3])}</div>' for i in range(0, len(rows), 3)
    )
    return _card(
        f"Set the budget for {_esc(period)}",
        f'<form method="post" action="/t/{_esc(tenant)}/books/budget">'
        f'<input type="hidden" name="period" value="{_esc(period)}">'
        + grid
        + '<p class="note">Leave a line blank to leave it unbudgeted. Only income and '
        "expense accounts appear — a budget is about what you earn and spend, not what "
        "you hold.</p>"
        '<button class="btn" type="submit">Save the budget</button></form>',
    )
