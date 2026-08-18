"""Jobs — the screen a contractor actually lives on.

A P&L says the business spent $412,000 on materials this year. Nobody running
work asks that. They ask whether the Harper kitchen is making money, how much of
the framing budget is left, and whether they have billed ahead of what they have
built. So this page is built around one job at a time, and it puts the four
numbers that answer those questions above everything else:

* what it will cost if the current estimate holds,
* what has been **spent**,
* what has been **committed** — ordered on a signed purchase order and not yet
  billed, which is the number that stops a job going over budget invisibly,
* and what has been **billed** against what has been **earned**.

Budget, spent and committed are shown together per cost code, because a framing
budget that is 60% spent is not 40% remaining if the rest is already on order.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _minor, _num, _seq, money


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


def _pct(ppm: object) -> str:
    n = _minor(ppm)
    if n is None:
        return "—"
    return f"{n / 10_000:.1f}%"


def _milli(v: object) -> str:
    """Thousandths as a readable quantity: 8000 → "8", 2500 → "2.5"."""
    n = _minor(v)
    if n is None:
        return "—"
    whole, frac = divmod(abs(n), 1000)
    sign = "-" if n < 0 else ""
    if frac == 0:
        return f"{sign}{whole:,}"
    return f"{sign}{whole:,}.{str(frac).rstrip('0').ljust(1, '0')}"


_STATUS_LABEL = {
    "ESTIMATING": "estimating",
    "ACTIVE": "active",
    "ON_HOLD": "on hold",
    "COMPLETE": "complete",
    "CLOSED": "closed",
}
_METHOD_LABEL = {
    "TIME_AND_MATERIALS": "time & materials",
    "PROGRESS": "progress billing",
    "FIXED_MILESTONES": "fixed milestones",
    "COST_PLUS": "cost plus",
}


def render_jobs_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card("Jobs unavailable", '<div class="banner warn">The ledger service '
                 f"isn't reachable right now.</div>{extra}")


def render_jobs(
    tenant: str,
    jobs: Mapping[str, object],
    customers: Mapping[str, object],
    accounts: Mapping[str, object],
    *,
    can_edit: bool,
    message: str = "",
    error: str = "",
) -> str:
    """Every job on one line, and the form to start another."""
    rows = [j for j in _seq(jobs.get("jobs")) if isinstance(j, Mapping)]
    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    if not rows:
        body = (
            '<p class="muted">No jobs yet. A job is a piece of work under a customer '
            "with a contract value and a life of its own — it is what turns a pile of "
            "costs into an answer about whether the work made money.</p>"
        )
    else:
        cells = "".join(
            "<tr>"
            f'<td><a href="/t/{_esc(tenant)}/jobs/{_esc(j.get("id"))}">{_esc(j.get("name"))}</a>'
            f'<br><span class="muted" style="font-size:12px">'
            f'{_esc(j.get("customer_id"))} · {_esc(_METHOD_LABEL.get(str(j.get("billing_method")), ""))}'
            "</span></td>"
            f'<td class="muted">{_esc(_STATUS_LABEL.get(str(j.get("status")), j.get("status")))}</td>'
            f"{_num(j.get('contract_minor'))}"
            f"{_num(j.get('cost_to_date_minor'))}"
            f"{_num(j.get('revenue_to_date_minor'))}"
            f'<td class="num">{_pct(j.get("percent_spent_ppm"))}</td>'
            "</tr>"
            for j in rows
        )
        body = (
            "<table><thead><tr><th>Job</th><th>Status</th><th class='num'>Contract</th>"
            "<th class='num'>Spent</th><th class='num'>Billed</th>"
            "<th class='num'>Spent vs estimate</th></tr></thead>"
            f"<tbody>{cells}</tbody></table>"
        )

    out = head + _card(f"Jobs ({len(rows)})", body)
    if can_edit:
        out += _new_job_form(tenant, customers, accounts)
    return out


def _options(values: list[tuple[str, str]], selected: str = "") -> str:
    return "".join(
        f'<option value="{_esc(v)}"{" selected" if v == selected else ""}>{_esc(label)}</option>'
        for v, label in values
    )


def _new_job_form(
    tenant: str, customers: Mapping[str, object], accounts: Mapping[str, object],
) -> str:
    parties = [p for p in _seq(customers.get("parties")) if isinstance(p, Mapping)]
    customer_options = _options([(str(p.get("id")), str(p.get("name"))) for p in parties])
    revenue = [
        a for a in _seq(accounts.get("accounts"))
        if isinstance(a, Mapping) and str(a.get("type")) == "REVENUE"
    ]
    revenue_options = _options(
        [(str(a.get("code")), f"{a.get('code')} — {a.get('name')}") for a in revenue]
    )
    method_options = _options(
        [(k, v) for k, v in _METHOD_LABEL.items()], "TIME_AND_MATERIALS"
    )
    body = (
        f'<form method="post" action="/t/{_esc(tenant)}/jobs" class="grid">'
        '<label>Name<input name="name" required placeholder="Harper kitchen remodel"></label>'
        f"<label>Customer<select name=\"customer_id\" required>{customer_options}</select></label>"
        f'<label>How it gets billed<select name="billing_method">{method_options}</select></label>'
        '<label>Contract value<input name="contract" placeholder="85,000.00"></label>'
        '<label>Retainage %<input name="retainage" placeholder="10"></label>'
        f'<label>Revenue account<select name="revenue_account_code">{revenue_options}</select></label>'
        '<label>Starts<input name="start_date" placeholder="YYYY-MM-DD"></label>'
        '<label>Ends<input name="end_date" placeholder="YYYY-MM-DD"></label>'
        '<label style="grid-column:1/-1">Notes<input name="memo"></label>'
        '<div style="grid-column:1/-1"><button type="submit">Open the job</button></div>'
        "</form>"
        '<p class="muted" style="margin-top:8px">A time-and-materials job can start with '
        "no contract value; a progress-billed or fixed-price one cannot, because there "
        "would be nothing to bill against.</p>"
    )
    return _card("Start a job", body)


def render_job(
    tenant: str,
    detail: Mapping[str, object],
    cost: Mapping[str, object],
    work_orders: Mapping[str, object],
    billing: Mapping[str, object],
    wip_row: Mapping[str, object] | None,
    cost_codes: Mapping[str, object],
    *,
    can_edit: bool,
    message: str = "",
    error: str = "",
) -> str:
    """One job: where it stands, what it cost, what is left to bill."""
    job = detail.get("job") if isinstance(detail.get("job"), Mapping) else {}
    assert isinstance(job, Mapping)
    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    out = head + _headline(tenant, job, cost, wip_row)
    out += _cost_card(cost)
    out += _work_order_card(tenant, work_orders, can_edit)
    out += _billing_card(tenant, job, billing, can_edit)
    if can_edit:
        out += _budget_form(tenant, job, cost, cost_codes)
    return out


def _headline(
    tenant: str,
    job: Mapping[str, object],
    cost: Mapping[str, object],
    wip_row: Mapping[str, object] | None,
) -> str:
    totals = cost.get("totals") if isinstance(cost.get("totals"), Mapping) else {}
    assert isinstance(totals, Mapping)

    figures = [
        ("Contract", money(totals.get("contract_minor"))),
        ("Estimated cost", money(totals.get("revised_cost_minor"))),
        ("Spent", money(totals.get("actual_cost_minor"))),
        ("Committed", money(totals.get("committed_minor"))),
        ("Left to spend", money(totals.get("remaining_minor"))),
        ("Projected margin", money(totals.get("projected_margin_minor"))),
    ]
    if wip_row:
        figures.append(("Earned", money(wip_row.get("earned_revenue_minor"))))
        figures.append(("Billed", money(wip_row.get("billed_minor"))))

    tiles = "".join(
        f'<div style="min-width:130px"><div class="muted" style="font-size:12px">{_esc(label)}</div>'
        f'<div style="font:600 18px/1.2 var(--rg-sans)">{_esc(value)}</div></div>'
        for label, value in figures
    )
    note = ""
    if wip_row:
        under = _minor(wip_row.get("under_billed_minor")) or 0
        over = _minor(wip_row.get("over_billed_minor")) or 0
        if under:
            note = (
                f'<p class="muted">{money(under)} of work has been done and not yet '
                "billed. That is an asset, and it is also a phone call.</p>"
            )
        elif over:
            note = (
                f'<p class="muted">{money(over)} has been billed ahead of the work. '
                "That is money owed in labour, not profit.</p>"
            )
        if str(wip_row.get("estimate_exceeded")) == "True":
            note += (
                '<div class="banner warn">Cost has passed the estimate, so percent '
                "complete is capped at 100%. Revise the estimate — the schedule cannot "
                "tell you anything useful until it reflects what you now think.</div>"
            )
        loss = _minor(wip_row.get("projected_loss_minor")) or 0
        if loss:
            note += (
                f'<div class="banner warn">This job is expected to lose {money(loss)}. '
                "A foreseen loss belongs in the books as soon as it is foreseen.</div>"
            )

    body = (
        f'<div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:12px">{tiles}</div>'
        f"{note}"
        f'<p class="muted">Spent against the current estimate: '
        f'{_pct(totals.get("percent_spent_ppm"))}.</p>'
    )
    actions = (
        f'<a class="rg-btn" href="/t/{_esc(tenant)}/jobs">All jobs</a>'
    )
    return _card(f"{job.get('name')}", body, actions)


def _cost_card(cost: Mapping[str, object]) -> str:
    rows = [r for r in _seq(cost.get("rows")) if isinstance(r, Mapping)]
    if not rows:
        return _card(
            "Cost",
            '<p class="muted">Nothing has been costed to this job yet. Costs arrive by '
            "coding a bill, a bank line, a work order or a purchase to the job.</p>",
        )
    cells = ""
    for r in rows:
        warn = ' style="color:var(--rg-warn)"' if str(r.get("over_budget")) == "True" else ""
        cells += (
            f"<tr{warn}><td>{_esc(r.get('cost_code'))}"
            f'<br><span class="muted" style="font-size:12px">{_esc(r.get("name"))}</span></td>'
            f"{_num(r.get('budget_cost_minor'))}"
            f"{_num(r.get('revised_cost_minor'))}"
            f"{_num(r.get('actual_cost_minor'))}"
            f"{_num(r.get('committed_minor'))}"
            f"{_num(r.get('remaining_minor'))}"
            f'<td class="num">{_pct(r.get("percent_spent_ppm"))}</td></tr>'
        )
    totals = cost.get("totals") if isinstance(cost.get("totals"), Mapping) else {}
    assert isinstance(totals, Mapping)
    cells += (
        "<tr><td><strong>Total</strong></td>"
        f"{_num(totals.get('budget_cost_minor'))}{_num(totals.get('revised_cost_minor'))}"
        f"{_num(totals.get('actual_cost_minor'))}{_num(totals.get('committed_minor'))}"
        f"{_num(totals.get('remaining_minor'))}"
        f'<td class="num">{_pct(totals.get("percent_spent_ppm"))}</td></tr>'
    )
    extra = ""
    unbudgeted = [str(u) for u in _seq(cost.get("unbudgeted"))]
    if unbudgeted:
        extra += (
            f'<p class="muted">Spent in {", ".join(_esc(u) for u in unbudgeted)} with no '
            "budget line. Usually the interesting ones.</p>"
        )
    uncoded = _minor(cost.get("uncoded_minor")) or 0
    if uncoded:
        extra += (
            f'<p class="muted">{money(uncoded)} is on this job with no cost code. It still '
            "counts against the job; it just cannot be compared to anything.</p>"
        )
    body = (
        "<table><thead><tr><th>Code</th><th class='num'>Bid</th>"
        "<th class='num'>Estimate now</th><th class='num'>Spent</th>"
        "<th class='num'>Committed</th><th class='num'>Left</th>"
        "<th class='num'>Spent</th></tr></thead>"
        f"<tbody>{cells}</tbody></table>{extra}"
    )
    return _card("Budget vs actual", body)


def _work_order_card(
    tenant: str, work_orders: Mapping[str, object], can_edit: bool,
) -> str:
    rows = [w for w in _seq(work_orders.get("work_orders")) if isinstance(w, Mapping)]
    totals = work_orders.get("totals") if isinstance(work_orders.get("totals"), Mapping) else {}
    assert isinstance(totals, Mapping)
    if not rows:
        body = (
            '<p class="muted">No work orders. A work order is the piece of work somebody '
            "actually does on a day — the hours booked to it cost the job and bill the "
            "customer without being typed twice.</p>"
        )
    else:
        cells = "".join(
            "<tr>"
            f'<td><a href="/t/{_esc(tenant)}/work-orders/{_esc(w.get("id"))}">'
            f'{_esc(w.get("title"))}</a>'
            f'<br><span class="muted" style="font-size:12px">{_esc(w.get("scheduled_date"))}'
            f'{" · " + _esc(w.get("assignee")) if w.get("assignee") else ""}</span></td>'
            f'<td class="muted">{_esc(str(w.get("status")).lower().replace("_", " "))}</td>'
            f'<td class="num">{_milli((w.get("totals") or {}).get("hours_milli") if isinstance(w.get("totals"), Mapping) else 0)}</td>'
            f"{_num((w.get('totals') or {}).get('cost_minor') if isinstance(w.get('totals'), Mapping) else 0)}"
            f"{_num((w.get('totals') or {}).get('unbilled_minor') if isinstance(w.get('totals'), Mapping) else 0)}"
            "</tr>"
            for w in rows
        )
        body = (
            "<table><thead><tr><th>Work order</th><th>Status</th><th class='num'>Hours</th>"
            "<th class='num'>Cost</th><th class='num'>Unbilled</th></tr></thead>"
            f"<tbody>{cells}</tbody></table>"
            f'<p class="muted">{_esc(totals.get("open"))} still open · '
            f'{_milli(totals.get("hours_milli"))} hours · {money(totals.get("cost_minor"))} of '
            f'cost · {money(totals.get("unbilled_minor"))} billable and not yet invoiced.</p>'
        )
    actions = (
        f'<a class="rg-btn" href="/t/{_esc(tenant)}/work-orders?job_id='
        f'{_esc(work_orders.get("job_id"))}">Open work orders</a>' if can_edit else ""
    )
    return _card("Work orders", body, actions)


def _billing_card(
    tenant: str, job: Mapping[str, object], billing: Mapping[str, object], can_edit: bool,
) -> str:
    totals = billing.get("totals") if isinstance(billing.get("totals"), Mapping) else {}
    assert isinstance(totals, Mapping)
    schedule = [s for s in _seq(billing.get("schedule")) if isinstance(s, Mapping)]
    milestones = [m for m in _seq(billing.get("milestones")) if isinstance(m, Mapping)]
    unbilled = [u for u in _seq(billing.get("unbilled_work")) if isinstance(u, Mapping)]

    body = ""
    if schedule:
        cells = "".join(
            f"<tr><td>{_esc(s.get('description') or s.get('line_no'))}</td>"
            f"{_num(s.get('scheduled_value_minor'))}{_num(s.get('billed_minor'))}"
            f"{_num(s.get('remaining_minor'))}"
            f'<td class="num">{_pct(s.get("percent_billed_ppm"))}</td></tr>'
            for s in schedule
        )
        body += (
            "<h3 style='font:600 13px/1.2 var(--rg-sans);margin:0 0 6px'>Schedule of values</h3>"
            "<table><thead><tr><th>Line</th><th class='num'>Scheduled</th>"
            "<th class='num'>Billed</th><th class='num'>Left</th>"
            "<th class='num'>Billed</th></tr></thead>"
            f"<tbody>{cells}</tbody></table>"
        )
    if milestones:
        cells = "".join(
            f"<tr><td>{_esc(m.get('name'))}</td>"
            f'<td class="muted">{_esc(m.get("due_date"))}</td>'
            f"{_num(m.get('amount_minor'))}"
            f'<td class="muted">{_esc(str(m.get("status")).lower())}</td></tr>'
            for m in milestones
        )
        body += (
            "<h3 style='font:600 13px/1.2 var(--rg-sans);margin:12px 0 6px'>Milestones</h3>"
            "<table><thead><tr><th>Milestone</th><th>Due</th><th class='num'>Amount</th>"
            f"<th>Status</th></tr></thead><tbody>{cells}</tbody></table>"
        )
    if unbilled:
        cells = "".join(
            f'<tr><td class="muted">{_esc(u.get("date"))}</td>'
            f"<td>{_esc(u.get('description'))}</td>"
            f'<td class="num">{_milli(u.get("quantity_milli"))}</td>'
            f"{_num(u.get('bill_minor'))}</tr>"
            for u in unbilled
        )
        body += (
            "<h3 style='font:600 13px/1.2 var(--rg-sans);margin:12px 0 6px'>"
            "Done and not billed</h3>"
            "<table><thead><tr><th>Date</th><th>What</th><th class='num'>Qty</th>"
            f"<th class='num'>Bills at</th></tr></thead><tbody>{cells}</tbody></table>"
        )
    if not body:
        body = (
            '<p class="muted">Nothing to bill against yet. A progress-billed job needs a '
            "schedule of values; a fixed-price one needs milestones; a time-and-materials "
            "one bills whatever hours come back on its work orders.</p>"
        )

    retainage = _minor(totals.get("retainage_held_minor")) or 0
    deposit = _minor(totals.get("deposit_held_minor")) or 0
    notes = ""
    if retainage:
        notes += (
            f'<p class="muted">{money(retainage)} is held as retainage — earned, and not '
            "collectible until the job is signed off. It is deliberately not in accounts "
            "receivable, so the aging report stays true.</p>"
        )
    if deposit:
        notes += (
            f'<p class="muted">{money(deposit)} of deposit is held against this job. It is '
            "a liability until an invoice draws it down.</p>"
        )
    body += notes

    if can_edit:
        body += _billing_actions(tenant, job, billing)
    return _card("Billing", body)


def _billing_actions(
    tenant: str, job: Mapping[str, object], billing: Mapping[str, object],
) -> str:
    job_id = str(job.get("id"))
    method = str(job.get("billing_method"))
    base = f"/t/{_esc(tenant)}/jobs/{_esc(job_id)}"
    schedule = [s for s in _seq(billing.get("schedule")) if isinstance(s, Mapping)]
    milestones = [
        m for m in _seq(billing.get("milestones"))
        if isinstance(m, Mapping) and str(m.get("status")) == "PENDING"
    ]

    forms = ""
    if method == "PROGRESS" and schedule:
        lines = "".join(
            f'<label>{_esc(s.get("description") or s.get("line_no"))} '
            f'<input name="pct{_esc(s.get("line_no"))}" placeholder="% complete"></label>'
            for s in schedule
        )
        forms += (
            f'<form method="post" action="{base}/bill/progress" class="grid">'
            '<label>Invoice number<input name="id" required></label>'
            '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
            f"{lines}"
            '<div style="grid-column:1/-1"><button type="submit">Raise the application</button>'
            '<span class="muted" style="margin-left:8px">Percent complete is cumulative — '
            "what has been earned to date, not this period.</span></div></form>"
        )
    if method == "FIXED_MILESTONES" and milestones:
        options = _options(
            [(str(m.get("id")), f"{m.get('name')} — {money(m.get('amount_minor'))}")
             for m in milestones]
        )
        forms += (
            f'<form method="post" action="{base}/bill/milestone" class="grid">'
            '<label>Invoice number<input name="id" required></label>'
            '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
            f'<label>Milestone<select name="milestone_id">{options}</select></label>'
            '<div style="grid-column:1/-1"><button type="submit">Bill it</button></div></form>'
        )
    if _seq(billing.get("unbilled_work")):
        forms += (
            f'<form method="post" action="{base}/bill/time-and-materials" class="grid">'
            '<label>Invoice number<input name="id" required></label>'
            '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
            '<label>Up to<input name="through" placeholder="YYYY-MM-DD"></label>'
            '<label>One line per<select name="summarize">'
            '<option value="">visit</option><option value="1">work order</option>'
            "</select></label>"
            '<div style="grid-column:1/-1"><button type="submit">Bill the time</button>'
            '<span class="muted" style="margin-left:8px">A customer querying a T&amp;M '
            "invoice is asking what you did on the 14th.</span></div></form>"
        )
    forms += (
        f'<form method="post" action="{base}/deposits" class="grid">'
        '<label>Deposit taken<input name="amount" placeholder="20,000.00" required></label>'
        '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
        '<div style="grid-column:1/-1"><button type="submit">Record the deposit</button>'
        '<span class="muted" style="margin-left:8px">Held as a liability until an invoice '
        "draws it down — it is not income yet.</span></div></form>"
    )
    return f'<div style="margin-top:12px">{forms}</div>'


def _budget_form(
    tenant: str,
    job: Mapping[str, object],
    cost: Mapping[str, object],
    cost_codes: Mapping[str, object],
) -> str:
    codes = [c for c in _seq(cost_codes.get("cost_codes")) if isinstance(c, Mapping)]
    existing = {
        str(r.get("cost_code")): r
        for r in _seq(cost.get("rows")) if isinstance(r, Mapping)
    }
    rows = ""
    for c in codes:
        code = str(c.get("code"))
        row = existing.get(code, {})
        bid = row.get("budget_cost_minor") if isinstance(row, Mapping) else None
        revised = row.get("revised_cost_minor") if isinstance(row, Mapping) else None
        rows += (
            f"<tr><td>{_esc(code)} <span class='muted'>{_esc(c.get('name'))}</span></td>"
            f'<td><input name="bid_{_esc(code)}" value="{_esc(_plain(bid))}" '
            'placeholder="0.00"></td>'
            f'<td><input name="revised_{_esc(code)}" value="{_esc(_plain(revised))}" '
            'placeholder="same as the bid"></td></tr>'
        )
    body = (
        f'<form method="post" action="/t/{_esc(tenant)}/jobs/{_esc(job.get("id"))}/budget">'
        "<table><thead><tr><th>Cost code</th><th>What we bid</th>"
        "<th>What we now think</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        '<div style="margin-top:8px"><button type="submit">Save the budget</button></div>'
        "</form>"
        '<p class="muted" style="margin-top:8px">The bid stays put when you revise the '
        "estimate. That is the whole point: “we are over what we bid” is a sentence you "
        "can only say if both numbers survive.</p>"
    )
    return _card("Budget", body)


def _plain(minor: object) -> str:
    """A minor-unit value as a plain editable amount, with no currency symbol."""
    n = _minor(minor)
    if n is None or n == 0:
        return ""
    whole, frac = divmod(abs(n), 100)
    return f"{'-' if n < 0 else ''}{whole}.{frac:02d}"
