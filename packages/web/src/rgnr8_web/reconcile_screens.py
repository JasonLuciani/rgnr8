"""Owner-facing bank reconciliation — proving the books against the statement.

This is the screen that turns "the books are complete" into "the books are
*trustworthy*". The owner works the way they would with a paper statement in
hand: type the ending balance and date, tick each line that appears on the
statement, and watch the difference fall to zero. Only then can they finish,
which locks those lines in permanently.

Everything on this page is driven by the ledger service — the tick marks live
beside the journal, never on it, because the journal is append-only.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _minor, money


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


def _seq(v: object) -> list[object]:
    return list(v) if isinstance(v, (list, tuple)) else []


def render_pick_account(tenant: str, accounts: Mapping[str, object]) -> str:
    """The list of accounts that can be reconciled — cash and credit cards."""
    rows = []
    for a in _seq(accounts.get("accounts")):
        if not isinstance(a, Mapping):
            continue
        subtype = str(a.get("subtype") or "")
        type_ = str(a.get("type") or "")
        bankish = subtype in {"CASH", "BANK", "CREDIT_CARD"} or (
            type_ == "ASSET" and str(a.get("code", "")).startswith("10")
        )
        if not bankish:
            continue
        code = _esc(a.get("code"))
        rows.append(
            f"<tr><td>{code}</td><td>{_esc(a.get('name'))}</td>"
            f'<td><a class="btn-link" href="/t/{_esc(tenant)}/books/reconcile/{code}">'
            "Reconcile</a></td></tr>"
        )
    empty = (
        '<tr><td colspan="3" class="muted" style="text-align:center;padding:24px">'
        "No bank or credit-card accounts in the chart yet.</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Code</th><th>Account</th><th></th>'
        "</tr></thead><tbody>" + ("".join(rows) or empty) + "</tbody></table></div>"
    )
    back = f'<a class="btn-link" href="/t/{_esc(tenant)}/books">Back to books</a>'
    return _card("Reconcile an account", table, back)


_DASH = '<span class="muted">—</span>'

_STATUS = {
    "UNCLEARED": ("Not cleared", "var(--rg-muted)"),
    "CLEARED": ("Ticked", "var(--rg-watch,#b8860b)"),
    "RECONCILED": ("Reconciled", "var(--rg-sage)"),
}


def _status_chip(status: object) -> str:
    label, color = _STATUS.get(str(status), (str(status), "var(--rg-muted)"))
    return f'<span style="color:{color};font-weight:700;font-size:12px">{_esc(label)}</span>'


def _statement_form(tenant: str, code: str, date: str, balance_minor: object) -> str:
    n = _minor(balance_minor)
    decimal = "" if n is None else f"{n / 100:.2f}"
    return _card(
        "The statement",
        f'<form method="get" action="/t/{_esc(tenant)}/books/reconcile/{_esc(code)}">'
        '<div class="grid2">'
        '<div><label>Statement ending date</label>'
        f'<input name="statement_date" value="{_esc(date)}" placeholder="YYYY-MM-DD"></div>'
        '<div><label>Statement ending balance</label>'
        f'<input name="statement_balance" value="{_esc(decimal)}" inputmode="decimal"></div>'
        "</div>"
        '<p class="note">Copy these two numbers straight off the bank statement. '
        "Then tick every line below that appears on it.</p>"
        '<button class="btn" type="submit">Load</button></form>',
    )


def _tick_button(
    tenant: str, code: str, entry_id: object, status: object,
    date: str, balance_minor: object, can_post: bool,
) -> str:
    if str(status) == "RECONCILED":
        return '<span class="muted" title="Locked in by a finished reconciliation">🔒</span>'
    if not can_post:
        return ""
    ticked = str(status) == "CLEARED"
    return (
        f'<form method="post" action="/t/{_esc(tenant)}/books/reconcile/{_esc(code)}/toggle" '
        'style="margin:0">'
        f'<input type="hidden" name="entry_id" value="{_esc(entry_id)}">'
        f'<input type="hidden" name="cleared" value="{"0" if ticked else "1"}">'
        f'<input type="hidden" name="statement_date" value="{_esc(date)}">'
        f'<input type="hidden" name="statement_balance_minor" value="{_esc(balance_minor)}">'
        f'<button class="btn" style="padding:4px 12px;margin:0" type="submit">'
        f'{"✓" if ticked else "Tick"}</button></form>'
    )


def _import_form(tenant: str, code: str, date: str, balance_minor: object) -> str:
    """Upload the bank's own export and let it tick off the obvious matches."""
    return _card(
        "Import the statement instead",
        f'<form method="post" enctype="multipart/form-data" '
        f'action="/t/{_esc(tenant)}/books/reconcile/{_esc(code)}/import">'
        f'<input type="hidden" name="statement_date" value="{_esc(date)}">'
        f'<input type="hidden" name="statement_balance_minor" value="{_esc(balance_minor)}">'
        '<div class="grid2">'
        '<div><label>Statement file (OFX, QFX or CSV)</label>'
        '<input type="file" name="file"></div>'
        "<div><label></label>"
        '<button class="btn" type="submit">Import and match</button></div>'
        "</div>"
        '<p class="note">Download the transaction export from your bank — not the '
        "PDF summary. Matching lines are ticked for you; anything the statement "
        "shows that your books don't, and anything your books show that the "
        "statement doesn't, is listed so you can look at it. You still press "
        "Finish, because the point of a reconciliation is that a person did.</p>"
        "</form>",
    )


def render_import_result(
    tenant: str, code: str, result: Mapping[str, object], date: str
) -> str:
    """What the import matched — and, more usefully, what it couldn't."""
    missing = _seq(result.get("missing_from_books"))
    outstanding = _seq(result.get("not_on_statement"))

    def _rows(items: list[object], label_key: str, id_key: str = "") -> str:
        rows = []
        for i in items:
            if not isinstance(i, Mapping):
                continue
            amount = _minor(i.get("amount_minor")) or 0
            cls = "num neg" if amount < 0 else "num"
            rows.append(
                f"<tr><td class='muted'>{_esc(i.get('date'))}</td>"
                f"<td>{_esc(i.get(label_key)) or '—'}"
                + (f'<br><span class="muted" style="font-size:12px">'
                   f'{_esc(i.get(id_key))}</span>' if id_key else "")
                + f"</td><td class='{cls}'>{money(i.get('amount_minor'))}</td></tr>"
            )
        return "".join(rows)

    parsed = _minor(result.get("parsed")) or 0
    cleared = _minor(result.get("newly_cleared")) or 0
    head = _banner(
        "good" if not missing else "warn",
        f"Read {parsed} transactions and ticked off {cleared}."
        + (f" {len(missing)} on the statement aren't in your books."
           if missing else " Everything on the statement is in your books."),
    )

    blocks = ""
    if missing:
        blocks += _card(
            "On the statement, not in your books",
            '<div class="table-scroll"><table><thead><tr><th>Date</th>'
            '<th>Description</th><th class="num">Amount</th></tr></thead><tbody>'
            + _rows(missing, "description") + "</tbody></table></div>"
            + '<p class="note">These are transactions the business doesn\'t know '
            "happened. Usually a missed receipt or a subscription nobody recorded — "
            "occasionally something worse. Add them to your books, then import again.</p>",
        )
    if outstanding:
        blocks += _card(
            "In your books, not on the statement",
            '<div class="table-scroll"><table><thead><tr><th>Date</th>'
            '<th>Memo</th><th class="num">Amount</th></tr></thead><tbody>'
            + _rows(outstanding, "memo", "entry_id") + "</tbody></table></div>"
            + '<p class="note">Normally outstanding cheques and deposits in transit, '
            "which is fine and expected. Worth a look if something here is old, or "
            "if you see the same amount twice.</p>",
        )

    view = result.get("view")
    balance = view.get("statement_balance_minor") if isinstance(view, Mapping) else ""
    back = (
        f'<a class="btn-link" href="/t/{_esc(tenant)}/books/reconcile/{_esc(code)}'
        f'?statement_date={_esc(date)}&statement_balance_minor={_esc(balance)}">'
        "Back to the worksheet</a>"
    )
    return head + blocks + _card("Next", f'<p class="note">{back}</p>')


def render_reconcile(
    tenant: str,
    view: Mapping[str, object],
    *,
    can_post: bool,
    message: str = "",
    error: str = "",
) -> str:
    """The reconciliation worksheet for one account."""
    code = str(view.get("account_code", ""))
    date = str(view.get("statement_date", ""))
    balance_minor = view.get("statement_balance_minor")
    difference = _minor(view.get("difference_minor")) or 0
    can_finish = bool(view.get("can_finish"))

    rows = []
    for line in _seq(view.get("lines")):
        if not isinstance(line, Mapping):
            continue
        amount = _minor(line.get("amount_minor")) or 0
        cls = "num neg" if amount < 0 else "num"
        rows.append(
            f"<tr><td>{_esc(line.get('date'))}</td>"
            f"<td>{_esc(line.get('memo')) or _DASH}</td>"
            f'<td class="{cls}">{money(line.get("amount_minor"))}</td>'
            f"<td>{_status_chip(line.get('status'))}</td>"
            f"<td>{_tick_button(tenant, code, line.get('entry_id'), line.get('status'), date, balance_minor, can_post)}</td>"
            "</tr>"
        )
    empty = (
        '<tr><td colspan="5" class="muted" style="text-align:center;padding:24px">'
        "Nothing has hit this account yet.</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Date</th><th>Description</th>'
        '<th class="num">Amount</th><th>Status</th><th></th></tr></thead><tbody>'
        + ("".join(rows) or empty)
        + "</tbody></table></div>"
    )

    diff_color = "var(--rg-sage)" if difference == 0 else "var(--rg-risk,#b4462f)"
    through = view.get("reconciled_through")
    tally = (
        '<div class="grid2" style="gap:8px 24px">'
        f'<div><span class="muted">Last reconciled through</span><br>'
        f'<strong>{_esc(through) if through else "never"}</strong></div>'
        f'<div><span class="muted">Already reconciled</span><br>'
        f'<strong>{money(view.get("reconciled_balance_minor"))}</strong></div>'
        f'<div><span class="muted">Ticked this session</span><br>'
        f'<strong>{money(view.get("cleared_this_session_minor"))}</strong></div>'
        f'<div><span class="muted">Cleared balance</span><br>'
        f'<strong>{money(view.get("cleared_balance_minor"))}</strong></div>'
        f'<div><span class="muted">Statement balance</span><br>'
        f'<strong>{money(balance_minor)}</strong></div>'
        f'<div><span class="muted">Difference</span><br>'
        f'<strong style="color:{diff_color};font-size:18px">'
        f'{money(view.get("difference_minor"))}</strong></div>'
        "</div>"
    )

    finish = ""
    if can_post:
        if can_finish:
            finish = (
                f'<form method="post" action="/t/{_esc(tenant)}/books/reconcile/{_esc(code)}/finish">'
                f'<input type="hidden" name="statement_date" value="{_esc(date)}">'
                f'<input type="hidden" name="statement_balance_minor" value="{_esc(balance_minor)}">'
                '<p class="note">Finishing locks every ticked line as reconciled. '
                "They can't be un-ticked afterwards.</p>"
                '<button class="btn" type="submit">Finish reconciliation</button></form>'
            )
        else:
            finish = (
                '<p class="note">The difference has to reach zero before you can finish. '
                "If it won't, a transaction is missing from the books, entered twice, "
                "or entered for the wrong amount.</p>"
            )

    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    title = f"Reconcile {code} — {_esc(view.get('account_name'))}"
    back = f'<a class="btn-link" href="/t/{_esc(tenant)}/books/reconcile">Other accounts</a>'
    return (
        head
        + _card(f"{title} · as of {_esc(date)}", tally + finish, back)
        + _statement_form(tenant, code, date, balance_minor)
        + (_import_form(tenant, code, date, balance_minor) if can_post else "")
        + _card("Transactions", table)
    )
