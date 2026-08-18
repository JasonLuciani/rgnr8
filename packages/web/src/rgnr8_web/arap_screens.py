"""Owner-facing invoicing and bills — the AR/AP screens.

This is the part of the product an owner touches daily: send an invoice, see
who's late, mark it paid; enter a bill, see what's due, pay it. Each action posts
a real journal entry in the ledger service, so the books and the open-item list
can never drift apart.

Amounts arrive as integer minor-unit strings and are formatted exactly.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _minor, _num, _seq, money


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


_STATUS_STYLE = {
    "OPEN": ("Open", "var(--rg-muted)"),
    "PARTIAL": ("Part paid", "var(--rg-watch,#b8860b)"),
    "PAID": ("Paid", "var(--rg-sage)"),
}


def _status_chip(status: object, overdue: bool) -> str:
    label, color = _STATUS_STYLE.get(str(status), (str(status), "var(--rg-muted)"))
    if overdue and str(status) != "PAID":
        label, color = "Overdue", "var(--rg-risk,#b4462f)"
    return f'<span style="color:{color};font-weight:700;font-size:12px">{_esc(label)}</span>'


def _party_names(parties: Mapping[str, object]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in _seq(parties.get("parties")):
        if isinstance(p, Mapping):
            out[str(p.get("id"))] = str(p.get("name"))
    return out


def render_documents(
    tenant: str,
    kind: str,
    docs: Mapping[str, object],
    parties: Mapping[str, object],
    *,
    today: str,
    can_post: bool,
    message: str = "",
    error: str = "",
) -> str:
    """The invoices or bills list, with totals, plus the create/collect forms."""
    is_ar = kind == "invoices"
    who = "Customer" if is_ar else "Vendor"
    title = "Invoices" if is_ar else "Bills"
    names = _party_names(parties)

    rows = []
    for d in _seq(docs.get("documents")):
        if not isinstance(d, Mapping):
            continue
        doc_id = _esc(d.get("id"))
        open_minor = _minor(d.get("open_minor")) or 0
        overdue = open_minor > 0 and str(d.get("due_date")) < today
        party = names.get(str(d.get("party_id")), str(d.get("party_id")))
        pay_cell = ""
        if can_post and open_minor > 0:
            pay_cell = (
                f'<form method="post" action="/t/{_esc(tenant)}/{_esc(kind)}/{doc_id}/payments" '
                'style="display:flex;gap:6px;align-items:center;margin:0">'
                f'<input name="amount" placeholder="{money(d.get("open_minor"))[1:]}" '
                'style="width:110px;padding:6px 8px" inputmode="decimal">'
                f'<input type="hidden" name="date" value="{_esc(today)}">'
                f'<button class="btn" style="padding:6px 12px;margin:0" type="submit">'
                f'{"Collect" if is_ar else "Pay"}</button></form>'
            )
        rows.append(
            f"<tr><td><strong>{doc_id}</strong><br>"
            f'<span class="muted">{_esc(d.get("memo"))}</span></td>'
            f"<td>{_esc(party)}</td><td>{_esc(d.get('date'))}</td>"
            f"<td>{_esc(d.get('due_date'))}</td>"
            f"{_num(d.get('total_minor'))}{_num(d.get('open_minor'))}"
            f"<td>{_status_chip(d.get('status'), overdue)}</td>"
            f"<td>{pay_cell}</td></tr>"
        )

    empty = (
        f'<tr><td colspan="8" class="muted" style="text-align:center;padding:24px">'
        f"No {title.lower()} yet.</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr>'
        f"<th>{'Invoice' if is_ar else 'Bill'}</th><th>{who}</th><th>Date</th><th>Due</th>"
        '<th class="num">Total</th><th class="num">Open</th><th>Status</th><th></th>'
        "</tr></thead><tbody>" + ("".join(rows) or empty) + "</tbody></table></div>"
    )

    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    outstanding = docs.get("open_total_minor")
    overdue_total = docs.get("overdue_total_minor")
    summary = (
        f'<p class="sub">{"Owed to you" if is_ar else "You owe"}: '
        f"<strong>{money(outstanding)}</strong>"
        + (
            f' · <span style="color:var(--rg-risk,#b4462f)">overdue {money(overdue_total)}</span>'
            if (_minor(overdue_total) or 0) > 0
            else ""
        )
        + f' · <a class="btn-link" href="/t/{_esc(tenant)}/{"receivables" if is_ar else "payables"}/aging">'
        "Aging</a></p>"
    )

    out = head + summary + _card(title, table)
    if can_post:
        out += _render_new_form(tenant, kind, parties, today)
    return out


def _render_new_form(
    tenant: str, kind: str, parties: Mapping[str, object], today: str
) -> str:
    is_ar = kind == "invoices"
    options = "".join(
        f'<option value="{_esc(p.get("id"))}">{_esc(p.get("name"))}</option>'
        for p in _seq(parties.get("parties"))
        if isinstance(p, Mapping)
    )
    if not options:
        who = "customer" if is_ar else "vendor"
        return _card(
            f"New {'invoice' if is_ar else 'bill'}",
            f'<p class="muted">Add a {who} first.</p>'
            f'<form method="post" action="/t/{_esc(tenant)}/{"customers" if is_ar else "vendors"}">'
            '<div class="grid2">'
            f'<div><label>{who.title()} name</label><input name="name" placeholder="Acme Ltd"></div>'
            '<div><label>Payment terms (days)</label><input name="terms_days" placeholder="30"></div>'
            "</div>"
            f'<button class="btn" type="submit">Add {who}</button></form>',
        )

    line = (
        '<div class="grid3">'
        '<div><label>Description</label><input name="desc{i}" placeholder="What was it?"></div>'
        '<div><label>Amount</label><input name="amount{i}" placeholder="0.00" inputmode="decimal"></div>'
        '<div><label>Account code</label><input name="code{i}" placeholder="{code}"></div>'
        "</div>"
    )
    default_code = "4100" if is_ar else "6400"
    lines = "".join(line.format(i=i, code=default_code) for i in range(1, 4))
    who = "Customer" if is_ar else "Vendor"
    return _card(
        f"New {'invoice' if is_ar else 'bill'}",
        f'<form method="post" action="/t/{_esc(tenant)}/{_esc(kind)}">'
        '<div class="grid3">'
        f'<div><label>{who}</label><select name="party_id">{options}</select></div>'
        f'<div><label>Number</label><input name="id" placeholder="{"INV-1001" if is_ar else "BILL-2001"}"></div>'
        f'<div><label>Date</label><input name="date" value="{_esc(today)}"></div>'
        "</div>"
        '<div class="grid2">'
        '<div><label>Memo</label><input name="memo" placeholder="Optional note"></div>'
        '<div><label>Due date (blank = payment terms)</label><input name="due_date" placeholder="YYYY-MM-DD"></div>'
        "</div>"
        f"{lines}"
        f'<p class="note">Each line posts to the account code you give it '
        f'({"income" if is_ar else "expense"}). The total {"debits Accounts Receivable" if is_ar else "credits Accounts Payable"} '
        "in the ledger.</p>"
        f'<button class="btn" type="submit">Create {"invoice" if is_ar else "bill"}</button></form>',
    )


def render_aging(tenant: str, side: str, data: Mapping[str, object],
                 parties: Mapping[str, object]) -> str:
    """The aging report — who is late, and by how much."""
    is_ar = side == "ar"
    names = _party_names(parties)
    labels = _seq(data.get("bucket_labels"))
    header = "".join(f'<th class="num">{_esc(l)}</th>' for l in labels)
    rows = []
    for r in _seq(data.get("rows")):
        if not isinstance(r, Mapping):
            continue
        party = names.get(str(r.get("party_id")), str(r.get("party_id")))
        cells = "".join(_num(b) for b in _seq(r.get("buckets_minor")))
        rows.append(f"<tr><td>{_esc(party)}</td>{cells}{_num(r.get('total_minor'))}</tr>")
    empty = (
        f'<tr><td colspan="{len(labels) + 2}" class="muted" style="text-align:center;padding:24px">'
        f"Nothing outstanding.</td></tr>"
    )
    totals = "".join(_num(t) for t in _seq(data.get("column_totals_minor")))
    table = (
        '<div class="table-scroll"><table><thead><tr>'
        f'<th>{"Customer" if is_ar else "Vendor"}</th>{header}<th class="num">Total</th>'
        "</tr></thead><tbody>" + ("".join(rows) or empty)
        + f'<tr style="font-weight:700"><td>Total</td>{totals}{_num(data.get("grand_total_minor"))}</tr>'
        + "</tbody></table></div>"
    )
    back = (
        f'<a class="btn-link" href="/t/{_esc(tenant)}/{"invoices" if is_ar else "bills"}">'
        f'Back to {"invoices" if is_ar else "bills"}</a>'
    )
    title = f"{'Receivables' if is_ar else 'Payables'} Aging — as of {_esc(data.get('as_of'))}"
    return _card(title, table, back)
