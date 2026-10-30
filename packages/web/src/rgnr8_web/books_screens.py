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
from .csp import script_open
from .parallel_run import form_label
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


# Account codes and what they mean to a person. Books mirrored from QuickBooks
# keep QuickBooks' code for every account, because that is how the two join;
# for an account QuickBooks has no number for, that code is QuickBooks' internal
# id ("57"), which means nothing to anyone. The app records which codes those
# are (`id_codes`), and screens show those accounts by name alone, ordered by
# type and then name; real account numbers keep showing and sort numerically.

def account_number(code: object, id_codes: frozenset[str] = frozenset()) -> str:
    """The number to show for an account: its code, or "" when the code is
    only QuickBooks' internal id."""
    text = str(code if code is not None else "")
    return "" if text in id_codes else text


def account_label(code: object, name: object,
                  id_codes: frozenset[str] = frozenset()) -> str:
    """How an account reads in a list or a picker: "4000 — Sales", or just
    "Sales" when there's no real number. Plain text; escape it to insert it."""
    number = account_number(code, id_codes)
    named = str(name or "")
    if not named:
        return str(code if code is not None else "")
    return f"{number} — {named}" if number else named


_TYPE_RANK = {"ASSET": 0, "LIABILITY": 1, "EQUITY": 2, "REVENUE": 3, "EXPENSE": 4}


def _link_text(a: Mapping[str, object], id_codes: frozenset[str]) -> str:
    """An account's link text: its name, or its label when it has none, so the
    link never comes out empty and unclickable."""
    return str(a.get("name") or "") or account_label(a.get("code"), a.get("name"), id_codes)


def account_order(code: object, name: object, type_: object,
                  id_codes: frozenset[str] = frozenset()) -> tuple[object, ...]:
    """Sort key for accounts on screen. A fully numbered chart sorts by number,
    as it always has. Where some codes are QuickBooks ids, the number can't
    carry the order, so accounts go by type (assets, liabilities, equity,
    income, expenses), numbered ones first by number, then the rest by name."""
    if not id_codes:
        return (0, 0, code_order(code), "")
    rank = _TYPE_RANK.get(str(type_ or ""), len(_TYPE_RANK))
    if str(code if code is not None else "") in id_codes:
        return (rank, 1, (), str(name or "").lower())
    return (rank, 0, code_order(code), "")


def _listed(accounts: Mapping[str, object], id_codes: frozenset[str],
            types: tuple[str, ...] = ()) -> list[Mapping[str, object]]:
    return sorted((a for a in _seq(accounts.get("accounts")) if isinstance(a, Mapping)
                   and (not types or str(a.get("type")) in types)),
                  key=lambda a: account_order(a.get("code"), a.get("name"), a.get("type"),
                                              id_codes))


def account_options(accounts: Mapping[str, object],
                    id_codes: frozenset[str] = frozenset(), *, selected: str = "",
                    types: tuple[str, ...] = ()) -> str:
    """<option>s for an account picker, in screen order, each labelled the way
    `account_label` reads. The value is always the stored code. `types` keeps
    only accounts of those types."""
    return "".join(
        f'<option value="{_esc(a.get("code"))}"'
        f'{" selected" if str(a.get("code")) == selected else ""}>'
        f'{_esc(account_label(a.get("code"), a.get("name"), id_codes))}</option>'
        for a in _listed(accounts, id_codes, types)
    )


_MONEY_SUBTYPES = ("BANK", "CASH")


def account_select(
    name: str, accounts: Mapping[str, object], id_codes: frozenset[str] = frozenset(), *,
    fallback: str = "", types: tuple[str, ...] = (), blank: str = "",
) -> str:
    """An account field as a picker, for a form that used to take a typed code: a
    company whose codes are QuickBooks ids has no code it could know to type.

    `blank` is for a field the ledger treats as optional: a first, empty choice
    with that label, selected. Otherwise the field has the account the ledger
    would fall back to (`fallback`, e.g. "1000") selected when the chart has it,
    else the first bank or cash account, so leaving it alone still means "the
    bank". With no accounts to offer (the chart couldn't be read), it stays a
    text box, as before."""
    listed = _listed(accounts, id_codes, types)
    if not listed:
        hint = f' placeholder="{_esc(fallback)}"' if fallback else ""
        return f'<input name="{_esc(name)}"{hint}>'
    selected = ""
    if not blank:
        codes = [str(a.get("code")) for a in listed]
        banks = [str(a.get("code")) for a in listed if str(a.get("subtype")) in _MONEY_SUBTYPES]
        selected = fallback if fallback in codes else (banks[0] if banks else "")
    first = f'<option value="" selected>{_esc(blank)}</option>' if blank else ""
    return (f'<select name="{_esc(name)}">{first}'
            f"{account_options(accounts, id_codes, selected=selected, types=types)}</select>")


def render_chart_of_accounts(tenant: str, data: Mapping[str, object], *,
                             id_codes: frozenset[str] = frozenset()) -> str:
    accounts = sorted((a for a in _seq(data.get("accounts")) if isinstance(a, Mapping)),
                      key=lambda a: account_order(a.get("code"), a.get("name"), a.get("type"),
                                                  id_codes))
    # A chart with no real numbers at all drops the column rather than show it empty.
    numbers = not accounts or any(account_number(a.get("code"), id_codes) for a in accounts)
    rows = []
    for a in accounts:
        code = _esc(a.get("code"))
        sub = a.get("subtype") or a.get("type")
        number = (f'<td class="muted">{_esc(account_number(a.get("code"), id_codes))}</td>'
                  if numbers else "")
        rows.append(
            f'<tr>{number}'
            f'<td><a href="/t/{_esc(tenant)}/books/accounts/{code}">{_esc(_link_text(a, id_codes))}</a></td>'
            f"<td>{_esc(a.get('type'))}</td>"
            f'<td class="muted">{_esc(sub)}</td></tr>'
        )
    empty = (
        '<tr><td colspan="4" class="empty">'
        "No accounts yet — this business's chart hasn't been set up.</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr>'
        + ("<th>No.</th>" if numbers else "")
        + "<th>Account</th><th>Type</th><th>Detail type</th></tr></thead><tbody>"
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
    id_codes: frozenset[str] = frozenset(),
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
    out += render_accounts_by_type(tenant, accounts, tb, ccy, id_codes=id_codes)
    if can_post:
        out += _render_entry_form(tenant, accounts, dimensions or {}, id_codes=id_codes)
    return out


# The order an owner reads a chart in: what you have, what you owe, what's
# yours, what came in, what went out.
_TYPE_ORDER = (
    ("ASSET", "Assets", 1), ("LIABILITY", "Liabilities", -1), ("EQUITY", "Equity", -1),
    ("REVENUE", "Income", -1), ("EXPENSE", "Expenses", 1),
)


def render_accounts_by_type(
    tenant: str, accounts: Mapping[str, object], tb: Mapping[str, object], ccy: str,
    *, id_codes: frozenset[str] = frozenset(),
) -> str:
    """The Ledger's opening view: every account under its type, with its balance
    in the direction people read it (what you owe as a positive amount owed),
    numbers in numeric order, and a total per type. An account with no real
    number sorts by name with its number left blank; a chart with no numbers at
    all drops the column."""
    balances: dict[str, int] = {}
    for r in _seq(tb.get("rows")):
        if isinstance(r, Mapping):
            balances[str(r.get("code"))] = (_minor(r.get("debit_minor")) or 0) - (
                _minor(r.get("credit_minor")) or 0)
    listed = [a for a in _seq(accounts.get("accounts")) if isinstance(a, Mapping)]
    numbers = any(account_number(a.get("code"), id_codes) for a in listed)
    cols = 3 if numbers else 2
    sections = []
    for kind, heading, sign in _TYPE_ORDER:
        mine = sorted((a for a in listed if str(a.get("type")) == kind),
                      key=lambda a: account_order(a.get("code"), a.get("name"), kind, id_codes))
        if not mine:
            continue
        total = 0
        body = []
        for a in mine:
            code = _esc(a.get("code"))
            amount = sign * balances.get(str(a.get("code")), 0)
            total += amount
            number = (f'<td class="muted">{_esc(account_number(a.get("code"), id_codes))}</td>'
                      if numbers else "")
            body.append(
                f'<tr><td><a href="/t/{_esc(tenant)}/books/accounts/{code}">{_esc(_link_text(a, id_codes))}</a></td>'
                f"{number}{_num(amount, ccy)}</tr>"
            )
        sections.append(
            f'<tr class="rg-eyebrow"><td colspan="{cols}">{_esc(heading)}</td></tr>' + "".join(body)
            + f'<tr style="font-weight:700"><td colspan="{cols - 1}">Total {_esc(heading.lower())}</td>'
            f"{_num(total, ccy)}</tr>"
        )
    if not sections:
        table = ('<p class="empty">No accounts yet — this business\'s chart hasn\'t been '
                 "set up.</p>")
    else:
        table = (
            '<div class="table-scroll"><table><thead><tr><th>Account</th>'
            + ("<th>No.</th>" if numbers else "")
            + '<th class="num">Balance</th></tr></thead><tbody>' + "".join(sections)
            + "</tbody></table></div>"
        )
    return _card("Accounts", table, provenance=Provenance.POSTED)


def render_trial_balance(tenant: str, tb: Mapping[str, object], *,
                         id_codes: frozenset[str] = frozenset()) -> str:
    """Every account's debit or credit balance, and that the two sides agree."""
    ccy = str(tb.get("currency", "USD"))
    rows = []
    ordered = sorted((r for r in _seq(tb.get("rows")) if isinstance(r, Mapping)),
                     key=lambda r: account_order(r.get("code"), r.get("name"), r.get("type"),
                                                 id_codes))
    numbers = not ordered or any(account_number(r.get("code"), id_codes) for r in ordered)
    for r in ordered:
        code = _esc(r.get("code"))
        number = (f'<td class="muted">{_esc(account_number(r.get("code"), id_codes))}</td>'
                  if numbers else "")
        rows.append(
            f"<tr>{number}"
            f'<td><a href="/t/{_esc(tenant)}/books/accounts/{code}">{_esc(_link_text(r, id_codes))}</a></td>'
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
        + '<div class="table-scroll"><table><thead><tr>'
        + ("<th>No.</th>" if numbers else "")
        + '<th>Account</th><th class="num">Debit</th><th class="num">Credit</th></tr></thead><tbody>'
        + ("".join(rows) or empty)
        + f'<tr style="font-weight:700"><td colspan="{2 if numbers else 1}">Totals</td>'
        + _num(tb.get("total_debit_minor"), ccy)
        + _num(tb.get("total_credit_minor"), ccy)
        + "</tr></tbody></table></div>"
    )
    return _card("Trial balance", table, provenance=Provenance.POSTED)


# Plain top-level functions in the page's own nonced script, so the dispatcher can
# find them by name (data-action="jeAddLine" and so on). Rows are `[data-je-line]`
# blocks whose inputs end in their index; a new row is a clone of the last one
# renumbered. Pasted rows are tab-separated: account, debit, credit, memo, name.
# Amounts go in as typed ("1,234.56"); the server parses them exactly.
_JE_LINES_JS = r"""
function jeRows(form){return Array.prototype.slice.call(form.querySelectorAll('[data-je-line]'));}
function jeNextIndex(form){var n=0;jeRows(form).forEach(function(r){r.querySelectorAll('[name]').forEach(function(f){var m=/(\d+)$/.exec(f.name);if(m&&+m[1]>n)n=+m[1];});});return n+1;}
function jeAddLine(el){
  var form=el.form||el.closest('form'),rows=jeRows(form),src=rows[rows.length-1],row=src.cloneNode(true),n=jeNextIndex(form);
  row.querySelectorAll('input,select').forEach(function(f){if(f.tagName==='SELECT')f.selectedIndex=0;else f.value='';f.name=f.name.replace(/\d+$/,n);});
  src.parentNode.insertBefore(row,src.nextSibling);
  return row;
}
function jeRemoveLine(el){
  var row=el.closest('[data-je-line]'),form=row.closest('form');
  if(jeRows(form).length>1){row.parentNode.removeChild(row);return;}
  row.querySelectorAll('input').forEach(function(f){f.value='';});
  row.querySelectorAll('select').forEach(function(f){f.selectedIndex=0;});
}
function jeParse(text){
  var out=[];
  String(text).split(/\r?\n/).forEach(function(s){
    if(!s.trim())return;
    var c=s.split('\t').map(function(x){return x.trim().replace(/^"(.*)"$/,'$1');});
    if(/^account$/i.test(c[0]))return;
    out.push({code:c[0]||'',debit:c[1]||'',credit:c[2]||'',memo:c[3]||'',name:c[4]||''});
  });
  return out;
}
function jeFilled(row){
  return ['select[name^="code"]','input[name^="debit"]','input[name^="credit"]','input[name^="name"]','input[name^="memo"]']
    .some(function(s){var f=row.querySelector(s);return f&&f.value;});
}
function jeFillFromPaste(el){
  var form=el.form||el.closest('form'),status=form.querySelector('[data-je-status]'),rows=jeParse(form.elements.paste.value),problems=[];
  if(!rows.length){status.textContent='Nothing to fill. Copy rows from the spreadsheet, then paste them here.';return;}
  rows.forEach(function(r,k){
    var row=jeRows(form).filter(function(x){return !jeFilled(x);})[0]||jeAddLine(el);
    var code=row.querySelector('select[name^="code"]');
    code.value=r.code;
    if(r.code&&code.value!==r.code)problems.push('row '+(k+1)+': no account '+r.code+' in this chart');
    row.querySelector('input[name^="debit"]').value=r.debit;
    row.querySelector('input[name^="credit"]').value=r.credit;
    row.querySelector('input[name^="name"]').value=r.name;
    row.querySelector('input[name^="memo"]').value=r.memo;
  });
  status.textContent=problems.length?problems.join('; ')+'.':'Filled '+rows.length+' line'+(rows.length===1?'':'s')+'. Check them, then post.';
}
"""


def _render_entry_form(
    tenant: str, accounts: Mapping[str, object],
    dimensions: Mapping[str, object] | None = None,
    *, id_codes: frozenset[str] = frozenset(),
) -> str:
    opts = account_options(accounts, id_codes)
    if not opts:
        return _card(
            "Record a transaction",
            '<p class="muted">Set up a chart of accounts first.</p>',
        )
    from .dimension_screens import dimension_selects

    # Lines are numbered from 1 and the POST handler reads whatever indexes it
    # finds, so "Add line" can keep going: it clones the last row under a new index.
    lines = "".join(
        '<div data-je-line>'
        '<div class="grid3">'
        f'<div><label>Account</label><select name="code{i}">{opts}</select></div>'
        f'<div><label>Debit</label><input name="debit{i}" placeholder="0.00" inputmode="decimal"></div>'
        f'<div><label>Credit</label><input name="credit{i}" placeholder="0.00" inputmode="decimal"></div>'
        "</div>"
        '<div class="grid3">'
        f'<div><label>Name</label><input name="name{i}" placeholder="Customer or vendor"></div>'
        f'<div><label>Line memo</label><input name="memo{i}" placeholder="Optional"></div>'
        f"{dimension_selects(dimensions or {}, suffix=str(i))}"
        "</div>"
        '<button type="button" class="btn-link" data-action="jeRemoveLine" data-self>'
        "Remove line</button></div>"
        for i in range(1, 5)
    )
    return _card(
        "Record a transaction",
        f'<form method="post" enctype="multipart/form-data" '
        f'action="/t/{_esc(tenant)}/books/entries"'
        f'{form_label("Journal entry", "+ New → Journal entry")}>'
        '<div class="grid2">'
        '<div><label>Date</label><input name="date" placeholder="YYYY-MM-DD"></div>'
        '<div><label>Memo</label><input name="memo" placeholder="What was this?"></div>'
        "</div>"
        f"{lines}"
        '<p><button type="button" data-action="jeAddLine" data-self>+ Add line</button></p>'
        '<div><label>Paste from a spreadsheet (optional)</label>'
        '<textarea name="paste" rows="3" placeholder="Account, Debit, Credit, Memo, Name, '
        'one line per row, tab-separated (copy the rows from Excel or Sheets)"></textarea></div>'
        '<p><button type="button" data-action="jeFillFromPaste" data-self>Fill lines from paste</button></p>'
        '<p class="note" data-je-status></p>'
        '<div><label>Attach a file (optional)</label>'
        '<input type="file" name="file" accept="image/*,application/pdf"></div>'
        '<p class="note">Enter an amount in either Debit or Credit on each line. '
        "Total debits must equal total credits — the ledger refuses anything else. "
        "A line's name is kept in front of its memo. "
        "A file you attach stays with the entry, behind its paperclip.</p>"
        + script_open() + _JE_LINES_JS + "</script>"
        + '<button class="btn" type="submit">Post entry</button>'
        "</form>",
    )


# --- account register --------------------------------------------------------

def render_register(tenant: str, reg: Mapping[str, object], *,
                    id_codes: frozenset[str] = frozenset()) -> str:
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
    return _card(account_label(reg.get("code"), reg.get("name"), id_codes), table, back,
                 provenance=Provenance.POSTED)


# --- statements from the client's own books ----------------------------------

def render_books_statements(
    period: str, data: Mapping[str, object], *, sealed: bool = False,
    id_codes: frozenset[str] = frozenset(),
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
                f"<tr><td>{_esc(account_number(l.get('code'), id_codes))}</td>"
                f"<td>{_esc(l.get('name'))}</td>"
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
                f"<tr><td>{_esc(account_number(l.get('code'), id_codes))}</td>"
                f"<td>{_esc(l.get('name'))}</td>"
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
