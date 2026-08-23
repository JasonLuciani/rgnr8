"""Payroll — what it really costs, and what you still owe.

Most small businesses record payroll as the money that left the bank. That
understates the cost (the employer's own taxes never appear anywhere) and hides
the debt (the withholdings are the employees' money, held in trust until the
deposit is made). A business run that way looks fine right up until the deposit
is due.

So these screens lead with the two numbers that matter and are usually missing:
**what this run cost the business** and **what is still owed**. Net pay — the
number people think of as "payroll" — is shown, but it is the smallest of the
three.

Amounts arrive as integer minor-unit strings and are formatted exactly.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _minor, _num, _seq, money
from .provenance_labels import Provenance
from .provenance_labels import badge as prov_badge
from .provenance_labels import legend as prov_legend

# A posted payroll run is a POSTED ledger fact; once its period is published it is
# SEALED. A draft run (not yet posted) is not shown as a book figure.
_PAYROLL_PROV = (Provenance.POSTED, Provenance.SEALED)


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


_STATUS = {
    "DRAFT": ("Draft", "var(--rg-muted)"),
    "POSTED": ("Posted", "var(--rg-sage)"),
    "VOID": ("Voided", "var(--rg-risk,#b4462f)"),
}


def _status_chip(status: object) -> str:
    label, color = _STATUS.get(str(status), (str(status), "var(--rg-muted)"))
    return f'<span style="color:{color};font-weight:700;font-size:12px">{_esc(label)}</span>'


def _employee_names(employees: Mapping[str, object]) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in _seq(employees.get("employees")):
        if isinstance(e, Mapping):
            out[str(e.get("id"))] = str(e.get("name"))
    return out


def render_payroll_home(
    tenant: str,
    runs: Mapping[str, object],
    liabilities: Mapping[str, object],
    employees: Mapping[str, object],
    *,
    can_post: bool,
    message: str = "",
    error: str = "",
) -> str:
    """The list of runs, what's owed, and the form to enter the next one."""
    rows = []
    for r in _seq(runs.get("runs")):
        if not isinstance(r, Mapping):
            continue
        totals = r.get("totals")
        t = totals if isinstance(totals, Mapping) else {}
        run_id = _esc(r.get("id"))
        actions = ""
        if can_post and str(r.get("status")) == "DRAFT":
            actions = (
                f'<form method="post" action="/t/{_esc(tenant)}/payroll/{run_id}/post" '
                'style="margin:0"><button class="btn" style="padding:4px 12px;margin:0" '
                'type="submit">Post it</button></form>'
            )
        elif can_post and str(r.get("status")) == "POSTED":
            actions = (
                f'<form method="post" action="/t/{_esc(tenant)}/payroll/{run_id}/void" '
                'style="margin:0"><button class="btn ghost" style="padding:4px 10px;margin:0" '
                'type="submit">Void</button></form>'
            )
        rows.append(
            f"<tr><td><strong>{run_id}</strong><br>"
            f'<span class="muted" style="font-size:12px">{_esc(r.get("memo"))}</span></td>'
            f"<td>{_esc(r.get('date'))}</td>"
            f"{_num(t.get('gross_minor'))}{_num(t.get('net_minor'))}"
            f"{_num(t.get('liability_minor'))}{_num(t.get('total_cost_minor'))}"
            f"<td>{_status_chip(r.get('status'))}</td><td>{actions}</td></tr>"
        )
    empty = (
        '<tr><td colspan="8" class="muted" style="text-align:center;padding:24px">'
        "No payroll runs yet.</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Run</th><th>Pay date</th>'
        '<th class="num">Gross</th><th class="num">Net paid</th>'
        '<th class="num">Owed after</th><th class="num">Real cost</th>'
        "<th>Status</th><th></th></tr></thead><tbody>"
        + ("".join(rows) or empty) + "</tbody></table></div>"
    )

    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    owed = _minor(liabilities.get("owed_minor")) or 0
    head += _banner(
        "warn" if owed > 0 else "good",
        f"You owe {money(liabilities.get('owed_minor'))} in payroll liabilities — "
        "withheld taxes and your employer share, held until you deposit them."
        if owed > 0
        else "No outstanding payroll liabilities.",
    )

    out = head + prov_legend(_PAYROLL_PROV) + _card(
        "Payroll runs", table, actions=prov_badge(Provenance.POSTED))
    if owed > 0 and can_post:
        out += _render_remit_form(tenant, liabilities)
    if can_post:
        out += _render_run_form(tenant, employees)
        out += _render_employee_form(tenant, employees)
    return out


def _render_remit_form(tenant: str, liabilities: Mapping[str, object]) -> str:
    owed = _minor(liabilities.get("owed_minor")) or 0
    return _card(
        "Make a payroll tax deposit",
        f'<form method="post" action="/t/{_esc(tenant)}/payroll/remit">'
        '<div class="grid3">'
        '<div><label>Payment date</label><input name="date" placeholder="YYYY-MM-DD"></div>'
        f'<div><label>Amount (you owe {money(liabilities.get("owed_minor"))})</label>'
        f'<input name="amount" value="{owed / 100:.2f}" inputmode="decimal"></div>'
        '<div><label>Paid from</label><input name="bank_code" value="1000"></div>'
        "</div>"
        '<p class="note">This clears the liability and moves the cash. Paying more '
        "than you owe is refused — that's a typo, not a payment.</p>"
        '<button class="btn" type="submit">Record the deposit</button></form>',
    )


def _render_run_form(tenant: str, employees: Mapping[str, object]) -> str:
    people = [e for e in _seq(employees.get("employees")) if isinstance(e, Mapping)]
    if not people:
        return _card(
            "Run payroll",
            '<p class="muted">Add an employee first.</p>',
        )
    blocks = []
    for i, e in enumerate(people, start=1):
        blocks.append(
            '<div class="grid4">'
            f'<div><label>{_esc(e.get("name"))}</label>'
            f'<input type="hidden" name="employee{i}" value="{_esc(e.get("id"))}">'
            f'<input name="gross{i}" placeholder="Gross pay" inputmode="decimal"></div>'
            f'<div><label>Taxes withheld</label>'
            f'<input name="taxes{i}" placeholder="0.00" inputmode="decimal"></div>'
            f'<div><label>Other deductions</label>'
            f'<input name="deductions{i}" placeholder="0.00" inputmode="decimal"></div>'
            '<div><label>Net</label><input value="calculated" disabled></div>'
            "</div>"
        )
    return _card(
        "Run payroll",
        f'<form method="post" action="/t/{_esc(tenant)}/payroll">'
        '<div class="grid3">'
        '<div><label>Run name</label><input name="id" placeholder="PR-2026-08-15"></div>'
        '<div><label>Pay date</label><input name="date" placeholder="YYYY-MM-DD"></div>'
        '<div><label>Note</label><input name="memo" placeholder="August 1–15"></div>'
        "</div>"
        + "".join(blocks)
        + '<div class="grid2">'
        "<div><label>Your employer payroll taxes for this run</label>"
        '<input name="employer_taxes" placeholder="0.00" inputmode="decimal"></div>'
        '<div><label>Paid from</label><input name="bank_code" value="1000"></div>'
        "</div>"
        '<p class="note">Leave an employee\'s gross blank to skip them this run. Net pay '
        "is calculated for you — gross less withholdings and deductions. Nothing posts "
        "to the books until you review the draft and post it.</p>"
        '<button class="btn" type="submit">Create the draft</button></form>',
    )


def _render_employee_form(tenant: str, employees: Mapping[str, object]) -> str:
    names = [
        _esc(e.get("name")) for e in _seq(employees.get("employees")) if isinstance(e, Mapping)
    ]
    current = (
        f'<p class="muted">{", ".join(names)}</p>' if names
        else '<p class="muted">Nobody on the payroll yet.</p>'
    )
    return _card(
        "Employees",
        current
        + f'<form method="post" action="/t/{_esc(tenant)}/payroll/employees">'
        '<div class="grid2">'
        '<div><label>Name</label><input name="name" placeholder="Ada Reyes"></div>'
        "<div><label></label>"
        '<button class="btn" type="submit">Add employee</button></div>'
        "</div></form>",
    )


def render_payroll_run(
    tenant: str, run: Mapping[str, object], employees: Mapping[str, object],
) -> str:
    """One run in detail — per-employee, with the three totals spelled out."""
    names = _employee_names(employees)
    rows = []
    for line in _seq(run.get("lines")):
        if not isinstance(line, Mapping):
            continue
        who = names.get(str(line.get("employee_id")), str(line.get("employee_id")))
        rows.append(
            f"<tr><td>{_esc(who)}</td>{_num(line.get('gross_minor'))}"
            f"{_num(line.get('employee_taxes_minor'))}{_num(line.get('deductions_minor'))}"
            f"{_num(line.get('net_minor'))}</tr>"
        )
    totals = run.get("totals")
    t = totals if isinstance(totals, Mapping) else {}
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Employee</th>'
        '<th class="num">Gross</th><th class="num">Taxes withheld</th>'
        '<th class="num">Deductions</th><th class="num">Net pay</th>'
        "</tr></thead><tbody>" + "".join(rows)
        + '<tr style="font-weight:700"><td>Totals</td>'
        + _num(t.get("gross_minor")) + _num(t.get("employee_taxes_minor"))
        + _num(t.get("deductions_minor")) + _num(t.get("net_minor"))
        + "</tr></tbody></table></div>"
    )
    explain = (
        '<div class="grid3" style="gap:8px 24px;margin-top:12px">'
        f'<div><span class="muted">Net pay leaving the bank</span><br>'
        f'<strong>{money(t.get("net_minor"))}</strong></div>'
        f'<div><span class="muted">Still owed afterwards</span><br>'
        f'<strong>{money(t.get("liability_minor"))}</strong></div>'
        f'<div><span class="muted">What this run actually cost</span><br>'
        f'<strong style="font-size:18px">{money(t.get("total_cost_minor"))}</strong></div>'
        "</div>"
        '<p class="note">The cost is gross pay plus your employer taxes '
        f'({money(t.get("employer_taxes_minor"))}) — more than the cash that leaves, '
        "because the withholdings and your share sit as a liability until you deposit them.</p>"
    )
    entry = (
        f'<p class="note">Journal entry {_esc(run.get("entry_id"))}</p>'
        if run.get("entry_id") else ""
    )
    back = f'<a class="btn-link" href="/t/{_esc(tenant)}/payroll">Back to payroll</a>'
    title = f"{_esc(run.get('id'))} — {_esc(run.get('date'))} · {_STATUS.get(str(run.get('status')), (str(run.get('status')), ''))[0]}"
    return _card(title, table + explain + entry, back)


def render_payroll_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card(
        "Payroll unavailable",
        _banner("warn", "The ledger service isn't reachable right now.") + extra,
    )
