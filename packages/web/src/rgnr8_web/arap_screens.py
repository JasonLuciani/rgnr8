"""Owner-facing invoicing and bills — the AR/AP screens.

This is the part of the product an owner touches daily: send an invoice, see
who's late, mark it paid; enter a bill, see what's due, pay it. Each action posts
a real journal entry in the ledger service, so the books and the open-item list
can never drift apart.

Amounts arrive as integer minor-unit strings and are formatted exactly.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import quote

from .attachment_screens import attachment_link
from .books_screens import _card, _esc, _minor, _num, _seq, money
from .parallel_run import form_label
from .provenance_labels import Provenance
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
        f'<br><span class="muted small" >'
        f'{money(d.get("net_minor"))} + {money(d.get("tax_minor"))} sales tax'
        f'{f" ({rate}%)" if ppm else ""}</span>'
    )


def _only_control_lines(d: Mapping[str, object], control_codes: frozenset[str]) -> bool:
    """A document that came over at cutover: its one line is the A/R or A/P
    balance it stands for, so a credit can't reverse an income or expense line."""
    lines = [ln for ln in _seq(d.get("lines")) if isinstance(ln, Mapping)]
    return bool(lines) and all(str(ln.get("account_code")) in control_codes for ln in lines)


def _account_pick(accounts: "tuple[tuple[str, str], ...]") -> str:
    options = '<option value="">Account…</option>' + "".join(
        f'<option value="{_esc(c)}">{_esc(n)}</option>' for c, n in accounts)
    return (f'<select name="account_code" aria-label="Account" required '
            f'style="width:auto;padding:6px 8px">{options}</select>')


def _adjust_controls(
    tenant: str, kind: str, d: Mapping[str, object], can_post: bool,
    credit_accounts: "tuple[tuple[str, str], ...]" = (),
    control_codes: frozenset[str] = frozenset(),
) -> str:
    """Credit what is still owed; refund what has already been collected.

    Only one of the two is ever offered, because only one of them is ever the
    right answer: money that hasn't arrived can't be sent back, and money that
    has can't be un-owed.
    """
    if not can_post:
        return ""
    doc_id = _esc(quote(str(d.get("id") or ""), safe=""))
    pick = _account_pick(credit_accounts) if _only_control_lines(d, control_codes) else ""
    open_minor = _minor(d.get("open_minor")) or 0
    total = _minor(d.get("total_minor")) or 0
    collected = total - open_minor
    is_ar = kind == "invoices"

    if open_minor > 0:
        label = "Credit" if is_ar else "Vendor credit"
        return (
            f'<form method="post" action="/t/{_esc(tenant)}/{_esc(kind)}/{doc_id}/credits"'
            + (form_label("Credit memo", "+ New → Credit memo") if is_ar
               else form_label("Vendor credit", "+ New → Vendor credit"))
            + ' style="display:flex;gap:6px;align-items:center;margin:4px 0 0">'
            f'<input name="amount" placeholder="{money(d.get("open_minor"))[1:]}" '
            f'style="width:100px;padding:6px 8px" inputmode="decimal">{pick}'
            f'<button class="btn ghost" style="padding:6px 10px;margin:0" type="submit" '
            f'title="Reduce what is owed — the document itself is never edited">'
            f'{label}</button></form>'
        )
    if is_ar and collected > 0:
        return (
            f'<form method="post" action="/t/{_esc(tenant)}/{_esc(kind)}/{doc_id}/refunds"'
            f'{form_label("Refund", "+ New → Refund receipt")}'
            ' style="display:flex;gap:6px;align-items:center;margin:4px 0 0">'
            f'<input name="amount" placeholder="{money(str(collected))[1:]}" '
            f'style="width:100px;padding:6px 8px" inputmode="decimal">{pick}'
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
    banks: "tuple[tuple[str, str], ...]" = (),
    credit_accounts: "tuple[tuple[str, str], ...]" = (),
    control_codes: frozenset[str] = frozenset(),
) -> str:
    """The invoices or bills list, with totals, plus the create/collect forms.

    `banks` is (code, name) for each bank account. With more than one, a payment
    says which it went through: the ledger refuses to guess on a chart without
    RGNR8's default 1000 (a QuickBooks chart, after cutover)."""
    bank_pick = ""
    if len(banks) > 1:
        # No default: money landing in the wrong bank is a reconciliation problem.
        options = '<option value="">Bank…</option>' + "".join(
            f'<option value="{_esc(c)}">{_esc(n)}</option>' for c, n in banks)
        bank_pick = (f'<select name="bank_code" aria-label="Bank account" required '
                     f'style="width:auto;padding:6px 8px">{options}</select>')
    is_ar = kind == "invoices"
    who = "Customer" if is_ar else "Vendor"
    title = "Invoices" if is_ar else "Bills"
    names = _party_names(parties)

    rows = []
    for d in _seq(docs.get("documents")):
        if not isinstance(d, Mapping):
            continue
        doc_id = _esc(d.get("id"))
        doc_path = _esc(quote(str(d.get("id") or ""), safe=""))
        open_minor = _minor(d.get("open_minor")) or 0
        overdue = open_minor > 0 and str(d.get("due_date")) < today
        party = names.get(str(d.get("party_id")), str(d.get("party_id")))
        pay_cell = ""
        if can_post and open_minor > 0:
            pay_cell = (
                f'<form method="post" action="/t/{_esc(tenant)}/{_esc(kind)}/{doc_path}/payments"'
                + (form_label("Receive payment", "+ New → Receive payment") if is_ar
                   else form_label("Pay bill", "+ New → Pay bills"))
                + ' style="display:flex;gap:6px;align-items:center;margin:0">'
                f'<input name="amount" placeholder="{money(d.get("open_minor"))[1:]}" '
                'style="width:110px;padding:6px 8px" inputmode="decimal">'
                f'<input type="hidden" name="date" value="{_esc(today)}">{bank_pick}'
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
            f"<td>{pay_cell}{_adjust_controls(tenant, kind, d, can_post, credit_accounts, control_codes)}</td></tr>"
        )

    empty = (
        f'<tr><td colspan="9" class="empty">'
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
        + "</p>"
    )

    out = head + summary + prov_legend(_ARAP_PROV) + _card(
        title, table, provenance=Provenance.POSTED)
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
        f'<form method="post" action="/t/{_esc(tenant)}/{_esc(kind)}"'
        + (form_label("Invoice", "+ New → Invoice") if is_ar else form_label("Bill", "+ New → Bill"))
        + '><div class="grid3">'
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
        f'<tr><td colspan="{len(labels) + 2}" class="empty">'
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
    title = f"{'Receivables' if is_ar else 'Payables'} Aging — as of {data.get('as_of')}"
    return _card(title, table, back, provenance=Provenance.POSTED)


def tie_out_line(kind: str, quickbooks_minor: int, books_minor: "int | None") -> str:
    """How QuickBooks' open items compare with the control account in the books.

    During a parallel run the books' A/R (or A/P) is a balance the mirror keeps
    level with QuickBooks, and these items are what QuickBooks says makes it
    up. They should agree; when they don't, the owner should see by how much
    and what usually causes it, not two numbers that silently differ.
    """
    account = "Accounts Receivable" if kind == "invoices" else "Accounts Payable"
    items = "invoices" if kind == "invoices" else "bills"
    if books_minor is None:
        return _banner("warn", f"Couldn't read {account} from your books to compare with "
                               f"QuickBooks' open {items}.")
    diff = quickbooks_minor - books_minor
    if diff == 0:
        return _banner("good", f"Ties out: QuickBooks' open {items} ({money(quickbooks_minor)}) "
                               f"match {account} in your books.")
    more = "more" if diff > 0 else "less"
    return _banner("warn", (
        f"QuickBooks shows {money(quickbooks_minor)} on open {items}; {account} in your "
        f"books is {money(books_minor)}, {money(abs(diff))} {more} on QuickBooks' side. "
        "After a Refresh these usually match. A gap that stays is something in QuickBooks "
        f"that isn't on an open {items[:-1]}: an unapplied payment or credit, or a journal "
        f"entry straight to {account}."))


def render_open_items(
    kind: str,
    rows: "tuple[Mapping[str, object], ...]",
    *,
    as_of: str,
    total_minor: int,
    books_minor: "int | None",
) -> str:
    """QuickBooks' open invoices or bills during a parallel run: read-only, dated,
    labelled imported, with the tie-out to the books above them."""
    is_ar = kind == "invoices"
    title = "Invoices" if is_ar else "Bills"
    who = "Customer" if is_ar else "Vendor"
    head = (f'<h1>{title}</h1><p class="sub">Open in QuickBooks as of {_esc(as_of)}, copied at each '
            "Refresh. While QuickBooks is your book of record, add and change them there.</p>")
    body_rows = "".join(
        "<tr>"
        f'<td>{_esc(r.get("number") or "")}</td>'
        f'<td>{_esc(r.get("party") or "")}</td>'
        f'<td>{_esc(r.get("date") or "")}</td>'
        f'<td>{_esc(r.get("due_date") or "")}</td>'
        f'{_num(r.get("open_minor"))}'
        "</tr>"
        for r in rows
    )
    table = (
        f'<table><thead><tr><th>{"Invoice" if is_ar else "Ref"}</th><th>{who}</th><th>Date</th>'
        '<th>Due</th><th class="num">Open</th></tr></thead>'
        f"<tbody>{body_rows}</tbody>"
        f'<tfoot><tr><td colspan="4">Total open</td>{_num(total_minor)}</tr></tfoot></table>'
    )
    # Every row is a QuickBooks copy, so the label goes once on the card, not
    # on each row.
    return head + tie_out_line(kind, total_minor, books_minor) + _card(
        f"Open in QuickBooks ({len(rows)})", table, provenance=Provenance.IMPORTED)
