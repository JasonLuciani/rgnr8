"""Sales orders and purchase orders — the two commitments a job runs on.

A sales order is work agreed and not yet billed. A purchase order is money
promised and not yet spent. Neither is a transaction, and neither belongs in the
ledger; both belong on a screen, because between them they are the difference
between a business that knows what is coming and one that finds out.

The purchase side carries the three-way match. Receiving more than was ordered
is refused, billing more than arrived is refused, and a price that differs from
the order has to be accepted deliberately — so the screen shows ordered,
received and billed on the same row, which is the only way a person can see
which of the three disagrees.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _minor, _num, _seq, money
from .job_screens import _milli, _pct


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


def _options(values: list[tuple[str, str]], selected: str = "") -> str:
    return "".join(
        f'<option value="{_esc(v)}"{" selected" if v == selected else ""}>{_esc(label)}</option>'
        for v, label in values
    )


def render_orders_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card("Orders unavailable", '<div class="banner warn">The ledger service '
                 f"isn't reachable right now.</div>{extra}")


# --- sales orders ------------------------------------------------------------

def render_sales_orders(
    tenant: str,
    orders: Mapping[str, object],
    backlog: Mapping[str, object],
    customers: Mapping[str, object],
    jobs: Mapping[str, object],
    *,
    can_edit: bool,
    message: str = "",
    error: str = "",
) -> str:
    rows = [o for o in _seq(orders.get("orders")) if isinstance(o, Mapping)]
    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    remaining = backlog.get("remaining_minor")
    body = (
        f'<p style="font:600 20px/1.2 var(--rg-sans)">{money(remaining)}</p>'
        '<p class="muted">Sold and not yet billed. This number is how a business decides '
        "whether to hire, and it exists nowhere in a general ledger — work agreed is not "
        "revenue.</p>"
    )
    out = head + _card("Backlog", body)

    if rows:
        cells = ""
        for o in rows:
            totals = o.get("totals") if isinstance(o.get("totals"), Mapping) else {}
            assert isinstance(totals, Mapping)
            cells += (
                "<tr>"
                f'<td><a href="/t/{_esc(tenant)}/sales-orders/{_esc(o.get("id"))}">'
                f'{_esc(o.get("id"))}</a>'
                f'<br><span class="muted" style="font-size:12px">{_esc(o.get("customer_id"))}'
                f'{" · " + _esc(o.get("job_id")) if o.get("job_id") else ""}</span></td>'
                f'<td class="muted">{_esc(str(o.get("status")).lower())}</td>'
                f'<td class="muted">{_esc(o.get("requested_date"))}</td>'
                f"{_num(totals.get('ordered_minor'))}"
                f"{_num(totals.get('invoiced_minor'))}"
                f"{_num(totals.get('remaining_minor'))}"
                "</tr>"
            )
        out += _card("Orders", (
            "<table><thead><tr><th>Order</th><th>Status</th><th>Wanted</th>"
            "<th class='num'>Ordered</th><th class='num'>Invoiced</th>"
            f"<th class='num'>Left</th></tr></thead><tbody>{cells}</tbody></table>"
        ))
    else:
        out += _card("Orders", (
            '<p class="muted">No orders. An order is where a commitment lives between '
            "“they said yes” and “we billed them” — invoice the whole job up front and the "
            "books carry revenue for work not yet done.</p>"
        ))

    if can_edit:
        out += _sales_order_form(tenant, customers, jobs)
    return out


def _sales_order_form(
    tenant: str, customers: Mapping[str, object], jobs: Mapping[str, object],
) -> str:
    parties = [p for p in _seq(customers.get("parties")) if isinstance(p, Mapping)]
    customer_options = _options([(str(p.get("id")), str(p.get("name"))) for p in parties])
    job_rows = [j for j in _seq(jobs.get("jobs")) if isinstance(j, Mapping)]
    job_options = _options(
        [("", "—")] + [(str(j.get("id")), str(j.get("name"))) for j in job_rows]
    )
    lines = "".join(
        "<tr>"
        f'<td><input name="desc{i}" placeholder="What"></td>'
        f'<td><input name="qty{i}" placeholder="1"></td>'
        f'<td><input name="price{i}" placeholder="0.00"></td>'
        "</tr>"
        for i in range(1, 5)
    )
    body = (
        f'<form method="post" action="/t/{_esc(tenant)}/sales-orders">'
        '<div class="grid">'
        '<label>Number<input name="id" placeholder="SO-1041"></label>'
        f"<label>Customer<select name=\"customer_id\" required>{customer_options}</select></label>"
        f'<label>Job<select name="job_id">{job_options}</select></label>'
        '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
        '<label>Wanted by<input name="requested_date" placeholder="YYYY-MM-DD"></label>'
        '<label>Notes<input name="memo"></label>'
        "</div>"
        "<table style='margin-top:10px'><thead><tr><th>Line</th><th>Qty</th>"
        f"<th>Price each</th></tr></thead><tbody>{lines}</tbody></table>"
        '<div style="margin-top:8px"><button type="submit">Take the order</button></div>'
        "</form>"
    )
    return _card("Take an order", body)


def render_sales_order(
    tenant: str, order: Mapping[str, object], *, can_edit: bool,
    message: str = "", error: str = "",
) -> str:
    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    lines = [line for line in _seq(order.get("lines")) if isinstance(line, Mapping)]
    totals = order.get("totals") if isinstance(order.get("totals"), Mapping) else {}
    assert isinstance(totals, Mapping)
    cells = "".join(
        f"<tr><td>{_esc(line.get('description') or line.get('line_no'))}</td>"
        f'<td class="num">{_milli(line.get("quantity_milli"))}</td>'
        f'<td class="num">{_milli(line.get("invoiced_milli"))}</td>'
        f'<td class="num">{_milli(line.get("remaining_milli"))}</td>'
        f"{_num(line.get('unit_price_minor'))}"
        f"{_num(line.get('extended_price_minor'))}</tr>"
        for line in lines
    )
    body = (
        f'<p class="muted">{_esc(order.get("customer_id"))} · '
        f'{_esc(str(order.get("status")).lower())}'
        + (f' · for {_esc(order.get("job_id"))}' if order.get("job_id") else "")
        + "</p>"
        "<table><thead><tr><th>Line</th><th class='num'>Ordered</th>"
        "<th class='num'>Invoiced</th><th class='num'>Left</th>"
        "<th class='num'>Price</th><th class='num'>Value</th></tr></thead>"
        f"<tbody>{cells}</tbody></table>"
        f'<p class="muted">{money(totals.get("remaining_minor"))} of this order is still to '
        "bill.</p>"
    )
    out = head + _card(str(order.get("id")), body)
    if can_edit and str(order.get("status")) not in ("CANCELLED", "FULFILLED"):
        rows = "".join(
            f'<label>{_esc(line.get("description") or line.get("line_no"))} '
            f'<input name="qty{_esc(line.get("line_no"))}" placeholder="'
            f'{_milli(line.get("remaining_milli"))} left"></label>'
            for line in lines
        )
        out += _card("Invoice against it", (
            f'<form method="post" action="/t/{_esc(tenant)}/sales-orders/'
            f'{_esc(order.get("id"))}/invoice" class="grid">'
            '<label>Invoice number<input name="id" required></label>'
            '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
            f"{rows}"
            '<div style="grid-column:1/-1"><button type="submit">Raise the invoice</button>'
            '<span class="muted" style="margin-left:8px">Leave a quantity blank to bill all '
            "of what is left on that line.</span></div></form>"
        ))
    return out


# --- purchase orders ---------------------------------------------------------

def render_purchase_orders(
    tenant: str,
    orders: Mapping[str, object],
    committed: Mapping[str, object],
    vendors: Mapping[str, object],
    jobs: Mapping[str, object],
    cost_codes: Mapping[str, object],
    *,
    can_edit: bool,
    message: str = "",
    error: str = "",
) -> str:
    rows = [o for o in _seq(orders.get("purchase_orders")) if isinstance(o, Mapping)]
    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    body = (
        f'<p style="font:600 20px/1.2 var(--rg-sans)">'
        f'{money(committed.get("committed_minor"))}</p>'
        '<p class="muted">Ordered and not yet in the books. A job is not fine because only '
        "half its framing budget has been billed, if the rest is already on a signed "
        "order — this is the number that says so.</p>"
    )
    out = head + _card("Committed", body)

    if rows:
        cells = ""
        for o in rows:
            totals = o.get("totals") if isinstance(o.get("totals"), Mapping) else {}
            assert isinstance(totals, Mapping)
            cells += (
                "<tr>"
                f'<td><a href="/t/{_esc(tenant)}/purchase-orders/{_esc(o.get("id"))}">'
                f'{_esc(o.get("id"))}</a>'
                f'<br><span class="muted" style="font-size:12px">{_esc(o.get("vendor_id"))}'
                f'{" · " + _esc(o.get("job_id")) if o.get("job_id") else ""}</span></td>'
                f'<td class="muted">{_esc(str(o.get("status")).lower())}</td>'
                f'<td class="muted">{_esc(o.get("expected_date"))}</td>'
                f"{_num(totals.get('ordered_minor'))}"
                f"{_num(totals.get('received_minor'))}"
                f"{_num(totals.get('billed_minor'))}"
                f"{_num(totals.get('committed_minor'))}"
                "</tr>"
            )
        out += _card("Purchase orders", (
            "<table><thead><tr><th>Order</th><th>Status</th><th>Expected</th>"
            "<th class='num'>Ordered</th><th class='num'>Received</th>"
            "<th class='num'>Billed</th><th class='num'>Committed</th></tr></thead>"
            f"<tbody>{cells}</tbody></table>"
            '<p class="muted">Ordered, received and billed on the same row: the three-way '
            "match is only a control if a person can see which of the three disagrees.</p>"
        ))
    else:
        out += _card("Purchase orders", (
            '<p class="muted">No purchase orders. A purchase order nobody matches is a PDF; '
            "one that is matched refuses to let you pay for more than turned up.</p>"
        ))

    if can_edit:
        out += _purchase_order_form(tenant, vendors, jobs, cost_codes)
    return out


def _purchase_order_form(
    tenant: str,
    vendors: Mapping[str, object],
    jobs: Mapping[str, object],
    cost_codes: Mapping[str, object],
) -> str:
    parties = [p for p in _seq(vendors.get("parties")) if isinstance(p, Mapping)]
    vendor_options = _options([(str(p.get("id")), str(p.get("name"))) for p in parties])
    job_rows = [j for j in _seq(jobs.get("jobs")) if isinstance(j, Mapping)]
    job_options = _options(
        [("", "—")] + [(str(j.get("id")), str(j.get("name"))) for j in job_rows]
    )
    codes = [c for c in _seq(cost_codes.get("cost_codes")) if isinstance(c, Mapping)]
    code_options = _options(
        [(str(c.get("code")), f"{c.get('code')} — {c.get('name')}") for c in codes]
    )
    lines = "".join(
        "<tr>"
        f'<td><input name="desc{i}" placeholder="What"></td>'
        f'<td><select name="code{i}">{code_options}</select></td>'
        f'<td><input name="qty{i}" placeholder="1"></td>'
        f'<td><input name="price{i}" placeholder="0.00"></td>'
        "</tr>"
        for i in range(1, 5)
    )
    body = (
        f'<form method="post" action="/t/{_esc(tenant)}/purchase-orders">'
        '<div class="grid">'
        '<label>Number<input name="id" placeholder="PO-1041"></label>'
        f"<label>Vendor<select name=\"vendor_id\" required>{vendor_options}</select></label>"
        f'<label>Job<select name="job_id">{job_options}</select></label>'
        '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
        '<label>Expected<input name="expected_date" placeholder="YYYY-MM-DD"></label>'
        '<label>Notes<input name="memo"></label>'
        "</div>"
        "<table style='margin-top:10px'><thead><tr><th>Line</th><th>Cost code</th>"
        f"<th>Qty</th><th>Price each</th></tr></thead><tbody>{lines}</tbody></table>"
        '<div style="margin-top:8px"><button type="submit">Raise the order</button></div>'
        "</form>"
    )
    return _card("Order materials", body)


def render_purchase_order(
    tenant: str,
    detail: Mapping[str, object],
    *,
    can_edit: bool,
    message: str = "",
    error: str = "",
) -> str:
    order = detail.get("purchase_order") if isinstance(detail.get("purchase_order"), Mapping) else {}
    assert isinstance(order, Mapping)
    receipts = [r for r in _seq(detail.get("receipts")) if isinstance(r, Mapping)]
    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    lines = [line for line in _seq(order.get("lines")) if isinstance(line, Mapping)]
    totals = order.get("totals") if isinstance(order.get("totals"), Mapping) else {}
    assert isinstance(totals, Mapping)
    cells = "".join(
        f"<tr><td>{_esc(line.get('description') or line.get('line_no'))}"
        + (f' <span class="muted">{_esc(line.get("cost_code"))}</span>'
           if line.get("cost_code") else "")
        + f'</td><td class="num">{_milli(line.get("quantity_milli"))}</td>'
        f'<td class="num">{_milli(line.get("received_milli"))}</td>'
        f'<td class="num">{_milli(line.get("billed_milli"))}</td>'
        f"{_num(line.get('unit_price_minor'))}"
        f"{_num(line.get('ordered_minor'))}</tr>"
        for line in lines
    )
    accrued = [r for r in receipts if r.get("accrued")]
    note = ""
    if accrued:
        note = (
            f'<p class="muted">{len(accrued)} of the deliveries were accrued, so their cost '
            "is already on the job. The bill will relieve that rather than book it twice, "
            "and any price difference posts as a variance you can see.</p>"
        )
    body = (
        f'<p class="muted">{_esc(order.get("vendor_id"))} · '
        f'{_esc(str(order.get("status")).lower())}'
        + (f' · for {_esc(order.get("job_id"))}' if order.get("job_id") else "")
        + "</p>"
        "<table><thead><tr><th>Line</th><th class='num'>Ordered</th>"
        "<th class='num'>Received</th><th class='num'>Billed</th>"
        "<th class='num'>Price</th><th class='num'>Value</th></tr></thead>"
        f"<tbody>{cells}</tbody></table>{note}"
        f'<p class="muted">{money(totals.get("committed_minor"))} still committed.</p>'
    )
    out = head + _card(str(order.get("id")), body)

    if receipts:
        rows = "".join(
            f'<tr><td class="muted">{_esc(r.get("date"))}</td>'
            f"<td>{_esc(r.get('id'))}</td>"
            f'<td class="muted">{"accrued" if r.get("accrued") else "recorded only"}</td>'
            f'<td class="muted">{_esc(r.get("memo"))}</td></tr>'
            for r in receipts
        )
        out += _card("Deliveries", (
            "<table><thead><tr><th>When</th><th>Receipt</th><th>Booked</th>"
            f"<th>Note</th></tr></thead><tbody>{rows}</tbody></table>"
        ))

    if can_edit and str(order.get("status")) not in ("CANCELLED", "CLOSED"):
        out += _receive_form(tenant, order, lines)
        out += _match_form(tenant, order, lines)
    return out


def _receive_form(
    tenant: str, order: Mapping[str, object], lines: list[Mapping[str, object]],
) -> str:
    rows = "".join(
        f'<label>{_esc(line.get("description") or line.get("line_no"))} '
        f'<input name="qty{_esc(line.get("line_no"))}" placeholder="all that is left"></label>'
        for line in lines
    )
    return _card("It arrived", (
        f'<form method="post" action="/t/{_esc(tenant)}/purchase-orders/'
        f'{_esc(order.get("id"))}/receipts" class="grid">'
        '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
        '<label>Book the cost now<select name="accrue">'
        '<option value="">no — wait for the invoice</option>'
        '<option value="1">yes — accrue it against the job</option></select></label>'
        f"{rows}"
        '<div style="grid-column:1/-1"><button type="submit">Record the delivery</button>'
        '<span class="muted" style="margin-left:8px">Accruing puts the cost on the job the '
        "day it turns up. Without it a job looks cheap until the invoice arrives, then loses "
        "money in one afternoon.</span></div></form>"
    ))


def _match_form(
    tenant: str, order: Mapping[str, object], lines: list[Mapping[str, object]],
) -> str:
    rows = "".join(
        f'<label>{_esc(line.get("description") or line.get("line_no"))} price '
        f'<input name="price{_esc(line.get("line_no"))}" placeholder="'
        f'{money(line.get("unit_price_minor"))}"></label>'
        for line in lines
    )
    return _card("The invoice came", (
        f'<form method="post" action="/t/{_esc(tenant)}/purchase-orders/'
        f'{_esc(order.get("id"))}/bill" class="grid">'
        '<label>Bill number<input name="id" required></label>'
        '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
        '<label>Due<input name="due_date" placeholder="YYYY-MM-DD"></label>'
        f"{rows}"
        '<label>Different price<select name="accept_variance">'
        '<option value="">refuse it</option>'
        '<option value="1">accept the variance</option></select></label>'
        '<div style="grid-column:1/-1"><button type="submit">Match and enter the bill</button>'
        '<span class="muted" style="margin-left:8px">Only what has arrived can be billed. A '
        "price that differs from the order is refused unless you accept it on purpose — that "
        "is the point of the order.</span></div></form>"
    ))


__all__ = [
    "render_orders_unavailable",
    "render_purchase_order",
    "render_purchase_orders",
    "render_sales_order",
    "render_sales_orders",
    "_pct",
    "_minor",
]
