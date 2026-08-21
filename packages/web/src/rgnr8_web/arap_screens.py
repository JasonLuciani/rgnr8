"""Owner-facing invoicing and bills — the AR/AP screens.

This is the part of the product an owner touches daily: send an invoice, see
who's late, mark it paid; enter a bill, see what's due, pay it. Each action posts
a real journal entry in the ledger service, so the books and the open-item list
can never drift apart.

Amounts arrive as integer minor-unit strings and are formatted exactly.
"""

from __future__ import annotations

from collections.abc import Mapping

from .attachment_screens import attachment_link
from .books_screens import _card, _esc, _minor, _num, _seq, money
from .provenance_labels import Provenance
from .provenance_labels import badge as prov_badge
from .provenance_labels import legend as prov_legend

# AR/AP figures: an open-item is a POSTED journal entry; once a payment is matched
# to the bank it is RECONCILED. Nothing here is a forecast.
_ARAP_PROV = (Provenance.IMPORTED, Provenance.POSTED, Provenance.RECONCILED)


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


def _paperclip(
    tenant: str, kind: str, d: Mapping[str, object],
    counts: Mapping[str, object] | None,
) -> str:
    """A link to whatever evidences this document, filled when there is any."""
    doc_id = str(d.get("id"))
    subject = "invoice" if kind == "invoices" else "bill"
    raw = (counts or {}).get(doc_id, 0)
    return attachment_link(tenant, subject, doc_id, _minor(raw) or 0)


def _contractor_fields() -> str:
    """Flagging a contractor early is the whole trick.

    The W-9 is easy to collect while you are hiring someone and hard to collect
    the following January. Asking here costs a moment; asking later costs a
    search for somebody who has moved on."""
    return (
        '<div class="grid2">'
        "<div><label>1099 contractor?</label><select name=\"is_1099\">"
        '<option value="">No</option>'
        '<option value="1">Yes — track payments for a 1099</option></select></div>'
        '<div><label>Tax ID (from their W-9, if you have it)</label>'
        '<input name="tax_id" placeholder="12-3456789"></div>'
        "</div>"
        '<p class="note">Flag them now even without the tax id — that is what puts '
        "them on the missing-W-9 list in March instead of surprising you in "
        "January.</p>"
    )


def _tax_note(d: Mapping[str, object]) -> str:
    """Sales tax, spelled out. It is the state's money, not the business's."""
    tax = _minor(d.get("tax_minor")) or 0
    if tax <= 0:
        return ""
    ppm = _minor(d.get("tax_rate_ppm")) or 0
    rate = f"{ppm / 10000:.4f}".rstrip("0").rstrip(".")
    return (
        f'<br><span class="muted" style="font-size:12px">'
        f'{money(d.get("net_minor"))} + {money(d.get("tax_minor"))} sales tax'
        f'{f" ({rate}%)" if ppm else ""}</span>'
    )


def _adjust_controls(
    tenant: str, kind: str, d: Mapping[str, object], can_post: bool
) -> str:
    """Credit what is still owed; refund what has already been collected.

    Only one of the two is ever offered, because only one of them is ever the
    right answer: money that hasn't arrived can't be sent back, and money that
    has can't be un-owed.
    """
    if not can_post:
        return ""
    doc_id = _esc(d.get("id"))
    open_minor = _minor(d.get("open_minor")) or 0
    total = _minor(d.get("total_minor")) or 0
    collected = total - open_minor
    is_ar = kind == "invoices"

    if open_minor > 0:
        label = "Credit" if is_ar else "Vendor credit"
        return (
            f'<form method="post" action="/t/{_esc(tenant)}/{_esc(kind)}/{doc_id}/credits" '
            'style="display:flex;gap:6px;align-items:center;margin:4px 0 0">'
            f'<input name="amount" placeholder="{money(d.get("open_minor"))[1:]}" '
            'style="width:100px;padding:6px 8px" inputmode="decimal">'
            f'<button class="btn ghost" style="padding:6px 10px;margin:0" type="submit" '
            f'title="Reduce what is owed — the document itself is never edited">'
            f'{label}</button></form>'
        )
    if is_ar and collected > 0:
        return (
            f'<form method="post" action="/t/{_esc(tenant)}/{_esc(kind)}/{doc_id}/refunds" '
            'style="display:flex;gap:6px;align-items:center;margin:4px 0 0">'
            f'<input name="amount" placeholder="{money(str(collected))[1:]}" '
            'style="width:100px;padding:6px 8px" inputmode="decimal">'
            '<button class="btn ghost" style="padding:6px 10px;margin:0" type="submit" '
            'title="Send money back — this moves cash out of the bank">'
            "Refund</button></form>"
        )
    return ""


def render_documents(
    tenant: str,
    kind: str,
    docs: Mapping[str, object],
    parties: Mapping[str, object],
    *,
    today: str,
    can_post: bool,
    attachment_counts: Mapping[str, object] | None = None,
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
            f'<span class="muted">{_esc(d.get("memo"))}</span>{_tax_note(d)}</td>'
            f"<td>{_esc(party)}</td><td>{_esc(d.get('date'))}</td>"
            f"<td>{_esc(d.get('due_date'))}</td>"
            f"{_num(d.get('total_minor'))}{_num(d.get('open_minor'))}"
            f"<td>{_status_chip(d.get('status'), overdue)}</td>"
            f"<td>{_paperclip(tenant, kind, d, attachment_counts)}</td>"
            f"<td>{pay_cell}{_adjust_controls(tenant, kind, d, can_post)}</td></tr>"
        )

    empty = (
        f'<tr><td colspan="9" class="muted" style="text-align:center;padding:24px">'
        f"No {title.lower()} yet.</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr>'
        f"<th>{'Invoice' if is_ar else 'Bill'}</th><th>{who}</th><th>Date</th><th>Due</th>"
        '<th class="num">Total</th><th class="num">Open</th><th>Status</th>'
        "<th></th><th></th></tr></thead><tbody>" + ("".join(rows) or empty) + "</tbody></table></div>"
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
        "Aging</a>"
        + ("" if is_ar
           else f' · <a class="btn-link" href="/t/{_esc(tenant)}/books/1099">1099s</a>')
        + "</p>"
    )

    out = head + summary + prov_legend(_ARAP_PROV) + _card(
        title, table, actions=prov_badge(Provenance.POSTED))
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
            + ("" if is_ar else _contractor_fields())
            + f'<button class="btn" type="submit">Add {who}</button></form>',
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
        '<div class="grid3">'
        '<div><label>Memo</label><input name="memo" placeholder="Optional note"></div>'
        '<div><label>Due date (blank = payment terms)</label><input name="due_date" placeholder="YYYY-MM-DD"></div>'
        + (
            '<div><label>Sales tax rate %</label>'
            '<input name="tax_rate" placeholder="8.25" inputmode="decimal"></div>'
            if is_ar else "<div></div>"
        )
        + "</div>"
        f"{lines}"
        f'<p class="note">Each line posts to the account code you give it '
        f'({"income" if is_ar else "expense"}). The total {"debits Accounts Receivable" if is_ar else "credits Accounts Payable"} '
        "in the ledger."
        + (
            " Sales tax is held as a liability — it is the state's money, not "
            "income, so it never shows up in your revenue."
            if is_ar else ""
        )
        + "</p>"
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
