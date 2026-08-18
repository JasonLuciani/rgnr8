"""Work orders — the day's work, and the hours that come back from it.

A job spans months; nobody works on a job. They work on "go back Tuesday and
hang the doors". This screen is that list, and the form that turns the hours
into three things at once: a cost on the job, a line on a time-and-materials
invoice, and a record of what the crew did.

The screen is explicit about what a time entry actually posts, because the
obvious reading is wrong. It does not book a second wage — the wage is already
in the books through payroll. It moves that cost out of an undifferentiated
payroll line and onto the job that consumed it.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _minor, _num, _seq, money
from .job_screens import _milli


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


def _options(values: list[tuple[str, str]], selected: str = "") -> str:
    return "".join(
        f'<option value="{_esc(v)}"{" selected" if v == selected else ""}>{_esc(label)}</option>'
        for v, label in values
    )


_STATUS = {
    "DRAFT": "draft", "SCHEDULED": "scheduled", "IN_PROGRESS": "in progress",
    "COMPLETE": "complete", "CANCELLED": "cancelled",
}


def render_work_orders_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card("Work orders unavailable", '<div class="banner warn">The ledger service '
                 f"isn't reachable right now.</div>{extra}")


def render_work_orders(
    tenant: str,
    orders: Mapping[str, object],
    jobs: Mapping[str, object],
    employees: Mapping[str, object],
    *,
    can_edit: bool,
    job_filter: str = "",
    message: str = "",
    error: str = "",
) -> str:
    rows = [w for w in _seq(orders.get("work_orders")) if isinstance(w, Mapping)]
    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    open_rows = [w for w in rows if str(w.get("status")) in ("DRAFT", "SCHEDULED", "IN_PROGRESS")]
    done_rows = [w for w in rows if w not in open_rows]

    body = _table(tenant, open_rows) if open_rows else (
        '<p class="muted">Nothing scheduled. A work order is a day of work under a job — '
        "the hours booked to it cost the job and bill the customer without being typed "
        "twice.</p>"
    )
    out = head + _card(f"To do ({len(open_rows)})", body)
    if done_rows:
        out += _card(f"Done ({len(done_rows)})", _table(tenant, done_rows))
    if can_edit:
        out += _new_form(tenant, jobs, employees, job_filter)
    return out


def _table(tenant: str, rows: list[Mapping[str, object]]) -> str:
    cells = ""
    for w in rows:
        totals = w.get("totals") if isinstance(w.get("totals"), Mapping) else {}
        assert isinstance(totals, Mapping)
        cells += (
            "<tr>"
            f'<td><a href="/t/{_esc(tenant)}/work-orders/{_esc(w.get("id"))}">'
            f'{_esc(w.get("title"))}</a>'
            f'<br><span class="muted" style="font-size:12px">{_esc(w.get("job_id"))}'
            f'{" · " + _esc(w.get("assignee")) if w.get("assignee") else ""}</span></td>'
            f'<td class="muted">{_esc(w.get("scheduled_date"))}</td>'
            f'<td class="muted">{_esc(_STATUS.get(str(w.get("status")), w.get("status")))}</td>'
            f'<td class="num">{_milli(totals.get("hours_milli"))}</td>'
            f"{_num(totals.get('cost_minor'))}"
            f"{_num(totals.get('unbilled_minor'))}"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Work order</th><th>When</th><th>Status</th>"
        "<th class='num'>Hours</th><th class='num'>Cost</th>"
        f"<th class='num'>Unbilled</th></tr></thead><tbody>{cells}</tbody></table>"
    )


def _new_form(
    tenant: str, jobs: Mapping[str, object], employees: Mapping[str, object], job_filter: str,
) -> str:
    job_rows = [j for j in _seq(jobs.get("jobs")) if isinstance(j, Mapping)]
    job_options = _options(
        [(str(j.get("id")), str(j.get("name"))) for j in job_rows], job_filter,
    )
    staff = [e for e in _seq(employees.get("employees")) if isinstance(e, Mapping)]
    staff_options = _options(
        [("", "—")] + [(str(e.get("id")), str(e.get("name"))) for e in staff]
    )
    body = (
        f'<form method="post" action="/t/{_esc(tenant)}/work-orders" class="grid">'
        '<label>What<input name="title" required placeholder="Hang the doors"></label>'
        f'<label>Job<select name="job_id" required>{job_options}</select></label>'
        '<label>When<input name="scheduled_date" placeholder="YYYY-MM-DD"></label>'
        f'<label>Who<select name="assignee_id">{staff_options}</select></label>'
        '<label style="grid-column:1/-1">Notes<input name="description"></label>'
        '<div style="grid-column:1/-1"><button type="submit">Schedule it</button></div>'
        "</form>"
    )
    return _card("Schedule work", body)


def render_work_order(
    tenant: str,
    order: Mapping[str, object],
    cost_codes: Mapping[str, object],
    employees: Mapping[str, object],
    *,
    can_edit: bool,
    message: str = "",
    error: str = "",
) -> str:
    """One work order: what was booked to it, and the form to book more."""
    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    totals = order.get("totals") if isinstance(order.get("totals"), Mapping) else {}
    assert isinstance(totals, Mapping)
    entries = [e for e in _seq(order.get("entries")) if isinstance(e, Mapping)]

    subtitle = (
        f'<p class="muted">{_esc(order.get("job_id"))} · '
        f'{_esc(_STATUS.get(str(order.get("status")), order.get("status")))}'
        + (f' · {_esc(order.get("scheduled_date"))}' if order.get("scheduled_date") else "")
        + (f' · {_esc(order.get("assignee"))}' if order.get("assignee") else "")
        + "</p>"
    )
    if entries:
        cells = ""
        for e in entries:
            unposted = "" if e.get("posted") else (
                ' <span class="muted">(not in the books)</span>'
            )
            cells += (
                f'<tr><td class="muted">{_esc(e.get("date"))}</td>'
                f"<td>{_esc(e.get('description') or e.get('kind'))}{unposted}</td>"
                f'<td class="muted">{_esc(e.get("cost_code"))}</td>'
                f'<td class="num">{_milli(e.get("quantity_milli"))}</td>'
                f"{_num(e.get('extended_cost_minor'))}"
                f"{_num(e.get('extended_bill_minor'))}"
                f'<td class="muted">{"billed" if e.get("invoice_id") else ""}</td></tr>'
            )
        table = (
            "<table><thead><tr><th>Date</th><th>What</th><th>Code</th>"
            "<th class='num'>Qty</th><th class='num'>Cost</th><th class='num'>Bills at</th>"
            f"<th></th></tr></thead><tbody>{cells}</tbody></table>"
        )
    else:
        table = '<p class="muted">Nothing booked to it yet.</p>'

    figures = (
        f'<p class="muted">{_milli(totals.get("hours_milli"))} hours · '
        f'{money(totals.get("cost_minor"))} of cost · '
        f'{money(totals.get("billable_minor"))} billable · '
        f'{money(totals.get("unbilled_minor"))} not yet invoiced · '
        f'margin {money(totals.get("margin_minor"))}.</p>'
    )
    body = subtitle + table + _quantity_note(entries) + figures
    out = head + _card(str(order.get("title")), body, (
        f'<a class="rg-btn" href="/t/{_esc(tenant)}/jobs/{_esc(order.get("job_id"))}">'
        "Back to the job</a>"
    ))
    if can_edit and str(order.get("status")) != "CANCELLED":
        out += _entry_form(tenant, order, cost_codes, employees)
        out += _complete_form(tenant, order)
    return out


def _entry_form(
    tenant: str,
    order: Mapping[str, object],
    cost_codes: Mapping[str, object],
    employees: Mapping[str, object],
) -> str:
    codes = [c for c in _seq(cost_codes.get("cost_codes")) if isinstance(c, Mapping)]
    code_options = _options(
        [(str(c.get("code")), f"{c.get('code')} — {c.get('name')}") for c in codes]
    )
    staff = [e for e in _seq(employees.get("employees")) if isinstance(e, Mapping)]
    staff_options = _options(
        [("", "—")] + [(str(e.get("id")), str(e.get("name"))) for e in staff]
    )
    body = (
        f'<form method="post" action="/t/{_esc(tenant)}/work-orders/'
        f'{_esc(order.get("id"))}/entries" class="grid">'
        '<label>What kind<select name="kind">'
        '<option value="LABOR">hours</option><option value="MATERIAL">material</option>'
        '<option value="EQUIPMENT">equipment</option><option value="OTHER">other</option>'
        "</select></label>"
        '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
        f'<label>Who<select name="employee_id">{staff_options}</select></label>'
        f'<label>Cost code<select name="cost_code">{code_options}</select></label>'
        '<label>How much<input name="quantity" placeholder="8" required></label>'
        '<label>Cost each<input name="unit_cost" placeholder="from the employee"></label>'
        '<label>Bills at<input name="unit_bill" placeholder="from the employee"></label>'
        '<label>Billable<select name="billable">'
        '<option value="1">yes</option><option value="">no — our mistake</option>'
        "</select></label>"
        '<label style="grid-column:1/-1">What was done<input name="description"></label>'
        '<div style="grid-column:1/-1"><button type="submit">Book it</button></div>'
        "</form>"
        '<p class="muted" style="margin-top:8px">Hours do not book a second wage. Payroll '
        "already did that; this moves the cost out of an undifferentiated payroll line and "
        "onto the job that used it. An hour is costed at what it really costs — wage plus "
        "taxes and insurance — because a job costed at the bare wage is costed at about 70% "
        "of the truth.</p>"
    )
    return _card("Book time or materials", body)


def _complete_form(tenant: str, order: Mapping[str, object]) -> str:
    if str(order.get("status")) == "COMPLETE":
        return _card("Done", (
            f'<p class="muted">Completed {_esc(order.get("completed_date"))}. Its costs are '
            "unaffected by that — they stay where they were booked.</p>"
        ))
    return _card("Done?", (
        f'<form method="post" action="/t/{_esc(tenant)}/work-orders/'
        f'{_esc(order.get("id"))}/complete" class="grid">'
        '<label>Finished on<input name="date" placeholder="YYYY-MM-DD" required></label>'
        '<div style="grid-column:1/-1"><button type="submit">Mark it complete</button></div>'
        "</form>"
    ))


def _quantity_note(entries: list[Mapping[str, object]]) -> str:
    unposted = [e for e in entries if not e.get("posted")]
    if not unposted:
        return ""
    return (
        f'<p class="muted">{len(unposted)} of these are recorded but not in the books, '
        "because there was nowhere honest to take the cost from. Say which account it comes "
        "from, or enter it as a bill coded to the job.</p>"
    )


__all__ = [
    "render_work_order",
    "render_work_orders",
    "render_work_orders_unavailable",
    "_minor",
    "_quantity_note",
]
