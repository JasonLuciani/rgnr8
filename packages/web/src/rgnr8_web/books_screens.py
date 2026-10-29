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

import re
from collections.abc import Mapping

from .components import card
from .provenance_labels import Provenance, legend as prov_legend


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


def _card(title: str, body: str, actions: str = "", *,
          provenance: "Provenance | None" = None) -> str:
    """Every books-family card goes through the one shared layout: title and
    provenance label in the header, `actions` after the content, bottom-left."""
    return card(title, body, actions=actions, provenance=provenance)


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

def code_order(code: object) -> tuple[object, ...]:
    """Sort key for account codes as people read them: "9" before "10" before
    "1000", and "1000-01" after "1000". The ledger hands them back sorted as
    text ("10" before "9"), which is right for storage and wrong on screen."""
    parts = re.split(r"(\d+)", str(code or ""))
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in parts if p)


def render_chart_of_accounts(tenant: str, data: Mapping[str, object]) -> str:
    accounts = sorted((a for a in _seq(data.get("accounts")) if isinstance(a, Mapping)),
                      key=lambda a: code_order(a.get("code")))
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
        '<tr><td colspan="4" class="empty">'
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
    out = ""
    if message:
        out += _banner("good", message)
    if error:
        out += _banner("warn", error)
    # The books' integrity at a glance, on the page people land on: that the two
    # sides agree, and plainly when nothing has been posted yet.
    if not _seq(tb.get("rows")):
        out += _banner("good", "Nothing posted yet. Record the first transaction below."
                       if can_post else "Nothing posted yet.")
    elif bool(tb.get("in_balance", True)):
        out += _banner("good", "In balance — total debits equal total credits")
    else:
        out += _banner("warn", "OUT OF BALANCE — this should never happen; contact support")
    out += render_accounts_by_type(tenant, accounts, tb, ccy)
    if can_post:
        out += _render_entry_form(tenant, accounts, dimensions or {})
    return out


# The order an owner reads a chart in: what you have, what you owe, what's
# yours, what came in, what went out.
_TYPE_ORDER = (
    ("ASSET", "Assets", 1), ("LIABILITY", "Liabilities", -1), ("EQUITY", "Equity", -1),
    ("REVENUE", "Income", -1), ("EXPENSE", "Expenses", 1),
)


def render_accounts_by_type(
    tenant: str, accounts: Mapping[str, object], tb: Mapping[str, object], ccy: str,
) -> str:
    """The Ledger's opening view: every account under its type, with its balance
    in the direction people read it (what you owe as a positive amount owed),
    numbers in numeric order, and a total per type."""
    balances: dict[str, int] = {}
    for r in _seq(tb.get("rows")):
        if isinstance(r, Mapping):
            balances[str(r.get("code"))] = (_minor(r.get("debit_minor")) or 0) - (
                _minor(r.get("credit_minor")) or 0)
    listed = [a for a in _seq(accounts.get("accounts")) if isinstance(a, Mapping)]
    sections = []
    for kind, heading, sign in _TYPE_ORDER:
        mine = sorted((a for a in listed if str(a.get("type")) == kind),
                      key=lambda a: code_order(a.get("code")))
        if not mine:
            continue
        total = 0
        body = []
        for a in mine:
            code = _esc(a.get("code"))
            amount = sign * balances.get(str(a.get("code")), 0)
            total += amount
            body.append(
                f'<tr><td><a href="/t/{_esc(tenant)}/books/accounts/{code}">{_esc(a.get("name"))}</a></td>'
                f'<td class="muted">{code}</td>{_num(amount, ccy)}</tr>'
            )
        sections.append(
            f'<tr class="rg-eyebrow"><td colspan="3">{_esc(heading)}</td></tr>' + "".join(body)
            + f'<tr style="font-weight:700"><td colspan="2">Total {_esc(heading.lower())}</td>'
            f"{_num(total, ccy)}</tr>"
        )
    if not sections:
        table = ('<p class="empty">No accounts yet — this business\'s chart hasn\'t been '
                 "set up.</p>")
    else:
        table = (
            '<div class="table-scroll"><table><thead><tr><th>Account</th><th>No.</th>'
            '<th class="num">Balance</th></tr></thead><tbody>' + "".join(sections)
            + "</tbody></table></div>"
        )
    return _card("Accounts", table, provenance=Provenance.POSTED)


def render_trial_balance(tenant: str, tb: Mapping[str, object]) -> str:
    """Every account's debit or credit balance, and that the two sides agree."""
    ccy = str(tb.get("currency", "USD"))
    rows = []
    ordered = sorted((r for r in _seq(tb.get("rows")) if isinstance(r, Mapping)),
                     key=lambda r: code_order(r.get("code")))
    for r in ordered:
        code = _esc(r.get("code"))
        rows.append(
            f'<tr><td><a href="/t/{_esc(tenant)}/books/accounts/{code}">{code}</a></td>'
            f"<td>{_esc(r.get('name'))}</td>"
            f"{_num(r.get('debit_minor'), ccy)}{_num(r.get('credit_minor'), ccy)}</tr>"
        )
    empty = (
        '<tr><td colspan="4" class="empty">'
        "Nothing posted yet.</td></tr>"
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
    return _card("Trial balance", table, provenance=Provenance.POSTED)


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
        '<tr><td colspan="5" class="empty">'
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
    return _card(f"{reg.get('code')} — {reg.get('name')}", table, back, provenance=Provenance.POSTED)


# --- statements from the client's own books ----------------------------------

def render_books_statements(
    period: str, data: Mapping[str, object], *, sealed: bool = False,
) -> str:
    ccy = str(data.get("currency", "USD"))
    income = data.get("income_statement")
    bs = data.get("balance_sheet")
    # Every figure here is at least POSTED (built from the journal); once the
    # period is published and locked it is SEALED and will not move.
    level = Provenance.SEALED if sealed else Provenance.POSTED
    out = (
        f'<p class="sub">Computed from this business\'s posted journal — period {_esc(period)}.</p>'
        + prov_legend((Provenance.POSTED, Provenance.RECONCILED, Provenance.SEALED))
    )

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
            provenance=level,
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
            provenance=level,
        )
    return out


def _cents(v: object) -> object:
    """The financial-statements/1 contract carries minor units as JS numbers."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    return int(v)
