"""Owner-facing screens for a client's *books* — the general ledger surface.

These render what the ledger service returns (chart of accounts, trial balance,
account register, statements computed from posted entries). This is the screen
layer that makes RGNR8 a system of record rather than a forecast overlay: an
owner can see their accounts, post a transaction, and read a trial balance and
P&L/balance sheet built from their own journal.

Amounts arrive as integer minor-unit strings and are formatted exactly here —
never through a float.
"""

from __future__ import annotations

from collections.abc import Mapping


def _esc(s: object) -> str:
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _minor(v: object) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        return None
    try:
        return int(v)
    except ValueError:
        return None


def money(v: object, currency: str = "USD") -> str:
    n = _minor(v)
    if n is None:
        return "—"
    neg = n < 0
    whole, frac = divmod(abs(n), 100)
    sym = "$" if currency == "USD" else f"{_esc(currency)} "
    return f"{'-' if neg else ''}{sym}{whole:,}.{frac:02d}"


def _num(v: object, currency: str = "USD") -> str:
    n = _minor(v) or 0
    cls = "num neg" if n < 0 else "num"
    return f'<td class="{cls}">{money(v, currency)}</td>'


def _seq(v: object) -> list[object]:
    return list(v) if isinstance(v, (list, tuple)) else []


def _card(title: str, body: str, actions: str = "") -> str:
    head = (
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin:0 0 12px">'
        f'<h2 style="margin:0;font:600 15px/1.2 var(--rg-sans)">{_esc(title)}</h2>{actions}</div>'
    )
    return f'<section class="card">{head}{body}</section>'


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


def unavailable(detail: str = "") -> str:
    """Shown when the ledger service can't be reached — honest, not a blank page."""
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card(
        "Books unavailable",
        _banner("warn", "The ledger service isn't reachable right now.")
        + '<p class="muted">Your books are safe — this screen just can\'t read them at the '
        "moment. Try again shortly.</p>" + extra,
    )


# --- chart of accounts -------------------------------------------------------

def render_chart_of_accounts(tenant: str, data: Mapping[str, object]) -> str:
    accounts = _seq(data.get("accounts"))
    rows = []
    for a in accounts:
        if not isinstance(a, Mapping):
            continue
        code = _esc(a.get("code"))
        sub = a.get("subtype") or a.get("type")
        rows.append(
            f'<tr><td><a href="/t/{_esc(tenant)}/books/accounts/{code}">{code}</a></td>'
            f"<td>{_esc(a.get('name'))}</td><td>{_esc(a.get('type'))}</td>"
            f'<td class="muted">{_esc(sub)}</td></tr>'
        )
    empty = (
        '<tr><td colspan="4" class="muted" style="text-align:center;padding:24px">'
        "No accounts yet — this business's chart hasn't been set up.</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Code</th><th>Account</th>'
        "<th>Type</th><th>Detail type</th></tr></thead><tbody>"
        + ("".join(rows) or empty)
        + "</tbody></table></div>"
    )
    return _card(f"Chart of Accounts ({len(rows)})", table)


# --- trial balance + post-entry form -----------------------------------------

def render_books_home(
    tenant: str,
    tb: Mapping[str, object],
    accounts: Mapping[str, object],
    *,
    dimensions: Mapping[str, object] | None = None,
    can_post: bool,
    message: str = "",
    error: str = "",
) -> str:
    ccy = str(tb.get("currency", "USD"))
    rows = []
    for r in _seq(tb.get("rows")):
        if not isinstance(r, Mapping):
            continue
        code = _esc(r.get("code"))
        rows.append(
            f'<tr><td><a href="/t/{_esc(tenant)}/books/accounts/{code}">{code}</a></td>'
            f"<td>{_esc(r.get('name'))}</td>"
            f"{_num(r.get('debit_minor'), ccy)}{_num(r.get('credit_minor'), ccy)}</tr>"
        )
    empty = (
        '<tr><td colspan="4" class="muted" style="text-align:center;padding:24px">'
        "Nothing posted yet. Record the first transaction below.</td></tr>"
    )
    in_balance = bool(tb.get("in_balance", True))
    balance_banner = (
        _banner("good", "In balance — total debits equal total credits")
        if in_balance
        else _banner("warn", "OUT OF BALANCE — this should never happen; contact support")
    )
    table = (
        balance_banner
        + '<div class="table-scroll"><table><thead><tr><th>Code</th><th>Account</th>'
        '<th class="num">Debit</th><th class="num">Credit</th></tr></thead><tbody>'
        + ("".join(rows) or empty)
        + '<tr style="font-weight:700"><td colspan="2">Totals</td>'
        + _num(tb.get("total_debit_minor"), ccy)
        + _num(tb.get("total_credit_minor"), ccy)
        + "</tr></tbody></table></div>"
    )
    actions = (
        f'<span><a class="btn-link" href="/t/{_esc(tenant)}/books/accounts">Chart of accounts</a>'
        f' · <a class="btn-link" href="/t/{_esc(tenant)}/books/statements">Statements</a>'
        f' · <a class="btn-link" href="/t/{_esc(tenant)}/books/gl">General ledger</a>'
        f' · <a class="btn-link" href="/t/{_esc(tenant)}/books/budget">Budget</a>'
        f' · <a class="btn-link" href="/t/{_esc(tenant)}/books/reconcile">Reconcile</a>'
        f' · <a class="btn-link" href="/t/{_esc(tenant)}/books/dimensions">Classes</a>'
        f' · <a class="btn-link" href="/t/{_esc(tenant)}/books/recurring">Recurring</a>'
        f' · <a class="btn-link" href="/t/{_esc(tenant)}/books/wip">Work in progress</a>'
        f' · <a class="btn-link" href="/t/{_esc(tenant)}/inventory">Inventory</a></span>'
    )
    out = ""
    if message:
        out += _banner("good", message)
    if error:
        out += _banner("warn", error)
    out += _card("Trial Balance", table, actions)
    if can_post:
        out += _render_entry_form(tenant, accounts, dimensions or {})
    return out


def _render_entry_form(
    tenant: str, accounts: Mapping[str, object],
    dimensions: Mapping[str, object] | None = None,
) -> str:
    options = []
    for a in _seq(accounts.get("accounts")):
        if not isinstance(a, Mapping):
            continue
        code = _esc(a.get("code"))
        options.append(f'<option value="{code}">{code} — {_esc(a.get("name"))}</option>')
    opts = "".join(options)
    if not opts:
        return _card(
            "Record a transaction",
            '<p class="muted">Set up a chart of accounts first.</p>',
        )
    from .dimension_screens import dimension_selects

    line = (
        '<div class="grid3">'
        '<div><label>Account</label><select name="{sel}">{opts}</select></div>'
        '<div><label>Debit</label><input name="{dr}" placeholder="0.00" inputmode="decimal"></div>'
        '<div><label>Credit</label><input name="{cr}" placeholder="0.00" inputmode="decimal"></div>'
        "</div>{dims}"
    )
    lines = "".join(
        line.format(
            sel=f"code{i}", dr=f"debit{i}", cr=f"credit{i}", opts=opts,
            dims=(
                f'<div class="grid3">{dimension_selects(dimensions or {}, suffix=str(i))}</div>'
                if dimension_selects(dimensions or {}) else ""
            ),
        )
        for i in range(1, 5)
    )
    return _card(
        "Record a transaction",
        f'<form method="post" action="/t/{_esc(tenant)}/books/entries">'
        '<div class="grid2">'
        '<div><label>Date</label><input name="date" placeholder="YYYY-MM-DD"></div>'
        '<div><label>Memo</label><input name="memo" placeholder="What was this?"></div>'
        "</div>"
        f"{lines}"
        '<p class="note">Enter an amount in either Debit or Credit on each line. '
        "Total debits must equal total credits — the ledger refuses anything else.</p>"
        '<button class="btn" type="submit">Post entry</button>'
        "</form>",
    )


# --- account register --------------------------------------------------------

def render_register(tenant: str, reg: Mapping[str, object]) -> str:
    rows = []
    for r in _seq(reg.get("rows")):
        if not isinstance(r, Mapping):
            continue
        rows.append(
            f"<tr><td>{_esc(r.get('date'))}</td><td>{_esc(r.get('memo'))}</td>"
            f"{_num(r.get('debit_minor'))}{_num(r.get('credit_minor'))}{_num(r.get('balance_minor'))}</tr>"
        )
    empty = (
        '<tr><td colspan="5" class="muted" style="text-align:center;padding:24px">'
        "No activity in this account.</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Date</th><th>Memo</th>'
        '<th class="num">Debit</th><th class="num">Credit</th><th class="num">Balance</th>'
        "</tr></thead><tbody>"
        f'<tr class="muted"><td colspan="4">Opening balance</td>{_num(reg.get("opening_minor"))}</tr>'
        + ("".join(rows) or empty)
        + '<tr style="font-weight:700"><td colspan="2">Closing balance</td>'
        + _num(reg.get("total_debit_minor"))
        + _num(reg.get("total_credit_minor"))
        + _num(reg.get("closing_minor"))
        + "</tr></tbody></table></div>"
    )
    back = f'<a class="btn-link" href="/t/{_esc(tenant)}/books">Back to books</a>'
    return _card(f"{_esc(reg.get('code'))} — {_esc(reg.get('name'))}", table, back)


# --- statements from the client's own books ----------------------------------

def render_books_statements(period: str, data: Mapping[str, object]) -> str:
    ccy = str(data.get("currency", "USD"))
    income = data.get("income_statement")
    bs = data.get("balance_sheet")
    out = f'<p class="sub">Computed from this business\'s posted journal — period {_esc(period)}.</p>'

    if isinstance(income, Mapping):
        def lines(key: str) -> str:
            return "".join(
                f"<tr><td>{_esc(l.get('code'))}</td><td>{_esc(l.get('name'))}</td>"
                f"{_num(_cents(l.get('amount')), ccy)}</tr>"
                for l in _seq(income.get(key))
                if isinstance(l, Mapping)
            )
        out += _card(
            "Income Statement",
            '<div class="table-scroll"><table><tbody>'
            '<tr class="rg-eyebrow"><td colspan="3">Revenue</td></tr>' + lines("revenue")
            + '<tr style="font-weight:700"><td colspan="2">Total revenue</td>'
            + _num(_cents(income.get("total_revenue")), ccy) + "</tr>"
            '<tr class="rg-eyebrow"><td colspan="3">Expenses</td></tr>' + lines("expenses")
            + '<tr style="font-weight:700"><td colspan="2">Total expenses</td>'
            + _num(_cents(income.get("total_expenses")), ccy) + "</tr>"
            + '<tr style="font-weight:800"><td colspan="2">Net income</td>'
            + _num(_cents(income.get("net_income")), ccy) + "</tr>"
            "</tbody></table></div>",
        )

    if isinstance(bs, Mapping):
        balanced = bool(bs.get("balanced"))
        banner = (
            _banner("good", "Balanced — assets = liabilities + equity")
            if balanced
            else _banner("warn", "Balance sheet does not balance")
        )
        def section(key: str, label: str) -> str:
            body = "".join(
                f"<tr><td>{_esc(l.get('code'))}</td><td>{_esc(l.get('name'))}</td>"
                f"{_num(_cents(l.get('amount')), ccy)}</tr>"
                for l in _seq(bs.get(key))
                if isinstance(l, Mapping)
            )
            return f'<tr class="rg-eyebrow"><td colspan="3">{label}</td></tr>{body}'
        out += _card(
            "Balance Sheet",
            banner + '<div class="table-scroll"><table><tbody>'
            + section("assets", "Assets")
            + '<tr style="font-weight:700"><td colspan="2">Total assets</td>'
            + _num(_cents(bs.get("total_assets")), ccy) + "</tr>"
            + section("liabilities", "Liabilities")
            + '<tr style="font-weight:700"><td colspan="2">Total liabilities</td>'
            + _num(_cents(bs.get("total_liabilities")), ccy) + "</tr>"
            + section("equity", "Equity")
            + '<tr style="font-weight:700"><td colspan="2">Total equity</td>'
            + _num(_cents(bs.get("total_equity")), ccy) + "</tr>"
            "</tbody></table></div>",
        )
    return out


def _cents(v: object) -> object:
    """The financial-statements/1 contract carries minor units as JS numbers."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    return int(v)
