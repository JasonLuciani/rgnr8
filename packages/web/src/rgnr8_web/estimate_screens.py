"""Estimates — where the margin is actually decided.

Most quoting software stores a list of prices. That throws away the half of the
document that matters: every line has a **cost** and a **price**, and the gap
between them is the business. Store only the price and, when the job runs, there
is nothing to compare the actual cost against.

So the builder asks for cost and markup, shows the price it implies, and shows
**margin beside markup** on every line and on the total — because they are not
the same number, and confusing them is the most expensive arithmetic error in
the trades. Cost 10,000 marked up 20% prices at 12,000, which is a 16.7% margin.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _minor, _num, _seq, money
from .job_screens import _milli, _pct, _plain


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


_STATUS_LABEL = {
    "DRAFT": "draft",
    "SENT": "sent",
    "ACCEPTED": "accepted",
    "DECLINED": "declined",
    "EXPIRED": "expired",
    "SUPERSEDED": "superseded",
}


def render_estimates_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card("Estimates unavailable", '<div class="banner warn">The ledger service '
                 f"isn't reachable right now.</div>{extra}")


def render_estimates(
    tenant: str,
    estimates: Mapping[str, object],
    customers: Mapping[str, object],
    cost_codes: Mapping[str, object],
    accounts: Mapping[str, object],
    *,
    can_edit: bool,
    message: str = "",
    error: str = "",
) -> str:
    rows = [e for e in _seq(estimates.get("estimates")) if isinstance(e, Mapping)]
    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    if not rows:
        body = (
            '<p class="muted">No estimates yet. An estimate here carries the cost as well '
            "as the price, so accepting one can seed the job's budget instead of asking "
            "you to type it a second time.</p>"
        )
    else:
        cells = ""
        for e in rows:
            totals = e.get("totals") if isinstance(e.get("totals"), Mapping) else {}
            assert isinstance(totals, Mapping)
            revision = _minor(e.get("revision")) or 1
            cells += (
                "<tr>"
                f'<td><a href="/t/{_esc(tenant)}/estimates/{_esc(e.get("id"))}">'
                f'{_esc(e.get("id"))}</a>'
                + (f' <span class="muted">rev {revision}</span>' if revision > 1 else "")
                + f'<br><span class="muted" style="font-size:12px">{_esc(e.get("customer_id"))}'
                f' · {_esc(e.get("date"))}</span></td>'
                f'<td class="muted">{_esc(_STATUS_LABEL.get(str(e.get("status")), e.get("status")))}</td>'
                f"{_num(totals.get('cost_minor'))}"
                f"{_num(totals.get('price_minor'))}"
                f'<td class="num">{_pct(totals.get("margin_ppm"))}</td>'
                "</tr>"
            )
        body = (
            "<table><thead><tr><th>Estimate</th><th>Status</th><th class='num'>Cost</th>"
            "<th class='num'>Price</th><th class='num'>Margin</th></tr></thead>"
            f"<tbody>{cells}</tbody></table>"
        )

    out = head + _card(f"Estimates ({len(rows)})", body)
    if can_edit:
        out += _builder(tenant, customers, cost_codes, accounts)
    return out


def _options(values: list[tuple[str, str]], selected: str = "") -> str:
    return "".join(
        f'<option value="{_esc(v)}"{" selected" if v == selected else ""}>{_esc(label)}</option>'
        for v, label in values
    )


def _builder(
    tenant: str,
    customers: Mapping[str, object],
    cost_codes: Mapping[str, object],
    accounts: Mapping[str, object],
) -> str:
    parties = [p for p in _seq(customers.get("parties")) if isinstance(p, Mapping)]
    customer_options = _options([(str(p.get("id")), str(p.get("name"))) for p in parties])
    codes = [c for c in _seq(cost_codes.get("cost_codes")) if isinstance(c, Mapping)]
    code_options = _options(
        [("", "—")] + [(str(c.get("code")), f"{c.get('code')} — {c.get('name')}") for c in codes]
    )
    revenue = [
        a for a in _seq(accounts.get("accounts"))
        if isinstance(a, Mapping) and str(a.get("type")) == "REVENUE"
    ]
    revenue_options = _options(
        [(str(a.get("code")), f"{a.get('code')} — {a.get('name')}") for a in revenue]
    )

    lines = ""
    for i in range(1, 6):
        lines += (
            "<tr>"
            f'<td><input name="desc{i}" placeholder="What it is"></td>'
            f'<td><select name="code{i}">{code_options}</select></td>'
            f'<td><input name="qty{i}" placeholder="1"></td>'
            f'<td><input name="cost{i}" placeholder="0.00"></td>'
            f'<td><input name="markup{i}" placeholder="20"></td>'
            f'<td><input name="price{i}" placeholder="or price it directly"></td>'
            f'<td><select name="account{i}">{revenue_options}</select></td>'
            "</tr>"
        )

    body = (
        f'<form method="post" action="/t/{_esc(tenant)}/estimates">'
        '<div class="grid">'
        '<label>Number<input name="id" placeholder="EST-1041"></label>'
        f"<label>Customer<select name=\"customer_id\" required>{customer_options}</select></label>"
        '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
        '<label>Good until<input name="expiry_date" placeholder="YYYY-MM-DD"></label>'
        '<label>Sales tax %<input name="tax_rate" placeholder="8.25"></label>'
        '<label>Description<input name="memo" placeholder="Harper kitchen remodel"></label>'
        "</div>"
        "<table style='margin-top:10px'><thead><tr><th>Line</th><th>Cost code</th>"
        "<th>Qty</th><th>Unit cost</th><th>Markup %</th><th>Unit price</th>"
        "<th>Bills to</th></tr></thead>"
        f"<tbody>{lines}</tbody></table>"
        '<div style="margin-top:8px"><button type="submit">Save the estimate</button></div>'
        "</form>"
        '<p class="muted" style="margin-top:8px">Type a markup and the price follows; type '
        "a price and the markup is worked out from it. Quantities take decimals — 2.5 days "
        "is 2.5, and it stays exact.</p>"
    )
    return _card("Build an estimate", body)


def render_estimate(
    tenant: str,
    estimate: Mapping[str, object],
    jobs: Mapping[str, object],
    *,
    can_edit: bool,
    message: str = "",
    error: str = "",
) -> str:
    """One estimate: the cost side, the price side, and what can happen next."""
    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    totals = estimate.get("totals") if isinstance(estimate.get("totals"), Mapping) else {}
    assert isinstance(totals, Mapping)
    lines = [line for line in _seq(estimate.get("lines")) if isinstance(line, Mapping)]

    cells = "".join(
        f"<tr><td>{_esc(line.get('description'))}"
        + (f' <span class="muted">{_esc(line.get("cost_code"))}</span>'
           if line.get("cost_code") else "")
        + f'</td><td class="num">{_milli(line.get("quantity_milli"))}</td>'
        f"{_num(line.get('unit_cost_minor'))}"
        f"{_num(line.get('extended_cost_minor'))}"
        f'<td class="num">{_pct(line.get("markup_ppm"))}</td>'
        f"{_num(line.get('extended_price_minor'))}"
        f'<td class="num">{_pct(line.get("margin_ppm"))}</td></tr>'
        for line in lines
    )
    cells += (
        "<tr><td><strong>Total</strong></td><td></td><td></td>"
        f"{_num(totals.get('cost_minor'))}"
        f'<td class="num">{_pct(totals.get("markup_ppm"))}</td>'
        f"{_num(totals.get('price_minor'))}"
        f'<td class="num">{_pct(totals.get("margin_ppm"))}</td></tr>'
    )
    tax = _minor(totals.get("tax_minor")) or 0
    tax_note = (
        f'<p class="muted">Plus {money(tax)} of sales tax — the state\'s money, held as a '
        f"liability, never revenue. {money(totals.get('total_minor'))} in total.</p>"
        if tax else ""
    )

    status = str(estimate.get("status"))
    revision = _minor(estimate.get("revision")) or 1
    subtitle = (
        f'<p class="muted">{_esc(estimate.get("customer_id"))} · {_esc(estimate.get("date"))}'
        f' · {_esc(_STATUS_LABEL.get(status, status))}'
        + (f" · revision {revision}" if revision > 1 else "")
        + (f' · expires {_esc(estimate.get("expiry_date"))}'
           if estimate.get("expiry_date") else "")
        + "</p>"
    )
    body = (
        subtitle
        + "<table><thead><tr><th>Line</th><th class='num'>Qty</th>"
        "<th class='num'>Unit cost</th><th class='num'>Cost</th><th class='num'>Markup</th>"
        "<th class='num'>Price</th><th class='num'>Margin</th></tr></thead>"
        f"<tbody>{cells}</tbody></table>{tax_note}"
        '<p class="muted">Markup is what you add to cost; margin is the share of the price '
        "that is not cost. They are never equal, and planning around the wrong one is how a "
        "job that looked like 20% comes in at 16.7%.</p>"
    )

    out = head + _card(str(estimate.get("id")), body)
    if can_edit:
        out += _next_steps(tenant, estimate, jobs)
    return out


def _next_steps(
    tenant: str, estimate: Mapping[str, object], jobs: Mapping[str, object],
) -> str:
    estimate_id = str(estimate.get("id"))
    status = str(estimate.get("status"))
    base = f"/t/{_esc(tenant)}/estimates/{_esc(estimate_id)}"
    forms = ""

    if status in ("DRAFT", "SENT"):
        job_rows = [j for j in _seq(jobs.get("jobs")) if isinstance(j, Mapping)]
        job_options = _options(
            [("", "start a new job")]
            + [(str(j.get("id")), str(j.get("name"))) for j in job_rows]
        )
        if status == "DRAFT":
            forms += (
                f'<form method="post" action="{base}/status" style="display:inline">'
                '<input type="hidden" name="status" value="SENT">'
                "<button type=\"submit\">Mark it sent</button></form> "
            )
        forms += (
            f'<form method="post" action="{base}/accept" class="grid">'
            f'<label>Onto<select name="job_id">{job_options}</select></label>'
            '<label>Job name<input name="job_name" placeholder="Harper kitchen remodel"></label>'
            '<label>Billed how<select name="billing_method">'
            '<option value="PROGRESS">progress billing</option>'
            '<option value="FIXED_MILESTONES">fixed milestones</option>'
            '<option value="TIME_AND_MATERIALS">time &amp; materials</option>'
            "</select></label>"
            '<label>Starts<input name="start_date" placeholder="YYYY-MM-DD"></label>'
            '<label>Retainage %<input name="retainage" placeholder="10"></label>'
            '<div style="grid-column:1/-1"><button type="submit">They accepted it</button>'
            '<span class="muted" style="margin-left:8px">Accepting seeds the job budget from '
            "the cost lines, so nobody types it twice.</span></div></form>"
            f'<form method="post" action="{base}/status" class="grid">'
            '<input type="hidden" name="status" value="DECLINED">'
            '<div style="grid-column:1/-1"><button type="submit">They declined</button></div>'
            "</form>"
        )
    if status == "ACCEPTED":
        forms += (
            f'<form method="post" action="{base}/invoice" class="grid">'
            '<label>Invoice number<input name="id" required></label>'
            '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
            '<div style="grid-column:1/-1"><button type="submit">Invoice the whole thing</button>'
            '<span class="muted" style="margin-left:8px">For a job billed in pieces, bill it '
            "from the job instead.</span></div></form>"
        )
    if status != "SUPERSEDED":
        forms += (
            f'<form method="post" action="{base}/revise" class="grid">'
            '<label>Why<input name="memo" placeholder="Upgraded cabinets"></label>'
            '<div style="grid-column:1/-1"><button type="submit">Start a revision</button>'
            '<span class="muted" style="margin-left:8px">The customer has seen this one. A '
            "revision supersedes it; both stay on the record.</span></div></form>"
        )
    return _card("What happens next", forms)


def render_pipeline_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card("Pipeline unavailable", '<div class="banner warn">The ledger service '
                 f"isn't reachable right now.</div>{extra}")


def render_pipeline(
    tenant: str,
    pipeline: Mapping[str, object],
    leads: Mapping[str, object],
    *,
    can_edit: bool,
    message: str = "",
    error: str = "",
) -> str:
    """Leads, opportunities, and what the pipeline is worth — twice over."""
    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    totals = pipeline.get("totals") if isinstance(pipeline.get("totals"), Mapping) else {}
    assert isinstance(totals, Mapping)
    stages = [s for s in _seq(pipeline.get("stages")) if isinstance(s, Mapping)]
    open_stages = [s for s in stages if str(s.get("stage")) not in ("WON", "LOST")]

    tiles = "".join(
        f'<div style="min-width:120px"><div class="muted" style="font-size:12px">'
        f'{_esc(str(s.get("stage")).lower())} ({_esc(s.get("count"))})</div>'
        f'<div style="font:600 16px/1.2 var(--rg-sans)">{money(s.get("value_minor"))}</div>'
        f'<div class="muted" style="font-size:12px">{money(s.get("weighted_value_minor"))} '
        "weighted</div></div>"
        for s in open_stages
    )
    body = (
        f'<div style="display:flex;gap:20px;flex-wrap:wrap;margin-bottom:12px">{tiles}</div>'
        f'<p class="muted">{money(totals.get("open_value_minor"))} is on the table; '
        f'{money(totals.get("open_weighted_minor"))} is what to plan around. They answer '
        "different questions, and reading one as the other means hiring too early or "
        "turning work away.</p>"
        f'<p class="muted">Won {_esc(totals.get("won_count"))} · lost '
        f'{_esc(totals.get("lost_count"))} · win rate {_pct(totals.get("win_rate_ppm"))}.</p>'
    )
    out = head + _card("Pipeline", body)

    opportunities = [
        o for o in _seq(pipeline.get("opportunities")) if isinstance(o, Mapping)
    ]
    if opportunities:
        cells = "".join(
            f"<tr><td>{_esc(o.get('name'))}"
            f'<br><span class="muted" style="font-size:12px">'
            f'{_esc(o.get("customer_id") or o.get("lead_id"))}'
            f'{" · " + _esc(o.get("owner")) if o.get("owner") else ""}</span></td>'
            f'<td class="muted">{_esc(str(o.get("stage")).lower())}</td>'
            f"{_num(o.get('value_minor'))}"
            f'<td class="num">{_pct(o.get("probability_ppm"))}</td>'
            f"{_num(o.get('weighted_value_minor'))}"
            f'<td class="muted">{_esc(o.get("expected_close_date"))}</td>'
            + (f"<td>{_actions(tenant, o)}</td>" if can_edit else "<td></td>")
            + "</tr>"
            for o in opportunities
        )
        out += _card("Opportunities", (
            "<table><thead><tr><th>Opportunity</th><th>Stage</th><th class='num'>Value</th>"
            "<th class='num'>Odds</th><th class='num'>Weighted</th><th>Expected</th>"
            f"<th></th></tr></thead><tbody>{cells}</tbody></table>"
        ))

    reasons = [r for r in _seq(pipeline.get("lost_reasons")) if isinstance(r, Mapping)]
    if reasons:
        items = "".join(
            f"<li>{_esc(r.get('reason'))} — {_esc(r.get('count'))}</li>" for r in reasons
        )
        out += _card("Why work was lost", (
            f"<ul>{items}</ul>"
            '<p class="muted">The pattern in these is the most useful thing the pipeline '
            "produces, which is why a reason is required to close one as lost.</p>"
        ))

    lead_rows = [
        line for line in _seq(leads.get("leads"))
        if isinstance(line, Mapping) and str(line.get("status")) != "CONVERTED"
    ]
    if lead_rows:
        cells = "".join(
            f"<tr><td>{_esc(line.get('name'))}"
            + (f'<br><span class="muted" style="font-size:12px">{_esc(line.get("company"))}</span>'
               if line.get("company") else "")
            + f'</td><td class="muted">{_esc(str(line.get("status")).lower())}</td>'
            f'<td class="muted">{_esc(line.get("source"))}</td>'
            + (
                f'<td><form method="post" action="/t/{_esc(tenant)}/leads/'
                f'{_esc(line.get("id"))}/convert" style="display:inline">'
                '<button type="submit">Make them a customer</button></form></td>'
                if can_edit else "<td></td>"
            )
            + "</tr>"
            for line in lead_rows
        )
        out += _card("Leads", (
            "<table><thead><tr><th>Lead</th><th>Status</th><th>Source</th>"
            f"<th></th></tr></thead><tbody>{cells}</tbody></table>"
        ))

    if can_edit:
        out += _pipeline_forms(tenant, leads)
    return out


def _actions(tenant: str, opportunity: Mapping[str, object]) -> str:
    base = f"/t/{_esc(tenant)}/opportunities/{_esc(opportunity.get('id'))}"
    if str(opportunity.get("stage")) in ("WON", "LOST"):
        # A close is sticky in both directions — the only way back into the
        # pipeline is a deliberate, logged reopen, never an in-place stage edit.
        return (
            f'<form method="post" action="{base}/reopen" style="display:inline">'
            '<button type="submit">Reopen</button></form>'
        )
    return (
        f'<form method="post" action="{base}/win" style="display:inline">'
        '<button type="submit">Won</button></form> '
        f'<form method="post" action="{base}/lose" style="display:inline">'
        '<input name="reason" placeholder="why?" required style="width:120px">'
        '<button type="submit">Lost</button></form>'
    )


def _pipeline_forms(tenant: str, leads: Mapping[str, object]) -> str:
    lead_rows = [line for line in _seq(leads.get("leads")) if isinstance(line, Mapping)]
    lead_options = _options(
        [("", "—")] + [(str(line.get("id")), str(line.get("name"))) for line in lead_rows]
    )
    body = (
        f'<form method="post" action="/t/{_esc(tenant)}/leads" class="grid">'
        '<label>Name<input name="name" required></label>'
        '<label>Company<input name="company"></label>'
        '<label>Email<input name="email"></label>'
        '<label>Phone<input name="phone"></label>'
        '<label>Where from<input name="source" placeholder="Referral"></label>'
        '<label>Owner<input name="owner"></label>'
        '<div style="grid-column:1/-1"><button type="submit">Add the lead</button></div>'
        "</form>"
        f'<form method="post" action="/t/{_esc(tenant)}/opportunities" class="grid" '
        'style="margin-top:12px">'
        '<label>What<input name="name" required placeholder="Kitchen remodel"></label>'
        f'<label>Lead<select name="lead_id">{lead_options}</select></label>'
        '<label>Customer<input name="customer_id" placeholder="or an existing customer"></label>'
        '<label>Worth<input name="value" placeholder="85,000.00"></label>'
        '<label>Stage<select name="stage">'
        '<option value="NEW">new</option><option value="QUALIFIED">qualified</option>'
        '<option value="PROPOSAL">proposal</option>'
        '<option value="NEGOTIATION">negotiation</option></select></label>'
        '<label>Expected<input name="expected_close_date" placeholder="YYYY-MM-DD"></label>'
        '<div style="grid-column:1/-1"><button type="submit">Open the opportunity</button>'
        '<span class="muted" style="margin-left:8px">Nothing here touches the books. The '
        "money appears when the job is billed.</span></div></form>"
    )
    return _card("Add to the pipeline", body)


__all__ = [
    "render_estimate",
    "render_estimates",
    "render_estimates_unavailable",
    "render_pipeline",
    "render_pipeline_unavailable",
    "_plain",
]
