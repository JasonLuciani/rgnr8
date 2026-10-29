"""Cutover: moving the book of record from QuickBooks to RGNR8.

One page with the whole process: pick the month-end, read the report (checks,
the tie-out of QuickBooks' open items to A/R and A/P, the invoices and bills
that will be created), sign it, run it, and, until RGNR8 records anything of
its own, back it out. The ledger enforces every rule; this page explains them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .books_screens import _banner, _card, _esc, _num, _seq


def _tie_out(rows: Sequence[Mapping[str, object]]) -> str:
    body = "".join(
        f"<tr><td>{_esc(r.get('control'))}</td>{_num(r.get('books_minor'))}"
        f"{_num(r.get('open_items_minor'))}{_num(r.get('difference_minor'))}</tr>"
        for r in rows
    )
    return ('<table><thead><tr><th>Account</th><th class="num">In the books</th>'
            '<th class="num">Open in QuickBooks</th><th class="num">Difference</th></tr></thead>'
            f"<tbody>{body}</tbody></table>")


def _documents(docs: Sequence[Mapping[str, object]]) -> str:
    if not docs:
        return '<p class="muted">Nothing is open in QuickBooks, so no invoices or bills are created.</p>'
    body = "".join(
        f"<tr><td>{'Invoice' if d.get('kind') == 'invoice' else 'Bill'}</td><td>{_esc(d.get('id'))}</td>"
        f"<td>{_esc(d.get('party'))}</td><td>{_esc(d.get('due_date'))}</td>{_num(d.get('open_minor'))}</tr>"
        for d in docs
    )
    return ('<div class="table-scroll"><table><thead><tr><th></th><th>Number</th><th>Customer / vendor</th>'
            '<th>Due</th><th class="num">Open</th></tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def render_cutover(
    tenant: str,
    *,
    status: Mapping[str, object],
    plan: "Mapping[str, object] | None",
    cutover_date: str,
    message: str = "",
    error: str = "",
) -> str:
    t = _esc(tenant)
    out = ""
    if message:
        out += _banner("good", message)
    if error:
        out += _banner("warn", error)
    record = status.get("record")
    if isinstance(record, Mapping) and record.get("state") == "live":
        blocker = str(status.get("backout_blocker") or "")
        back = (
            f'<form method="post" action="/t/{t}/connect/cutover/backout">'
            '<p>Until RGNR8 records anything of its own, this can be undone: QuickBooks becomes '
            "the book of record again and the invoices and bills created at cutover are removed.</p>"
            '<button class="btn ghost" type="submit">Back out the cutover</button></form>'
            if not blocker else
            f'<p class="muted">It can no longer be backed out: {_esc(blocker)}.</p>'
        )
        return out + _card("RGNR8 is your book of record", (
            f"<p>Cut over at the end of {_esc(record.get('cutover_date'))}, signed off by "
            f"<strong>{_esc(record.get('signed_by'))}</strong>. Report fingerprint "
            f"<code>{_esc(str(record.get('report_hash'))[:12])}</code>.</p>" + back))

    out += _card("Move to RGNR8", (
        "<p>Cutover makes RGNR8 the book of record from the start of the next month. It happens "
        "at a month-end only, after a Refresh that brings the books level through that date, "
        "and once your accountant has signed the report below.</p>"
        f'<form method="get" action="/t/{t}/connect/cutover" class="row">'
        f'<input name="date" type="date" value="{_esc(cutover_date)}" style="width:auto" '
        'aria-label="Cutover date (a month-end)">'
        '<button class="btn" type="submit">Prepare the cutover report</button></form>'))
    if plan is None:
        return out

    blockers = [str(b) for b in _seq(plan.get("blockers"))]
    tie = [r for r in _seq(plan.get("tie_out")) if isinstance(r, Mapping)]
    docs = [d for d in _seq(plan.get("documents")) if isinstance(d, Mapping)]
    differs = any(str(r.get("difference_minor")) not in ("0", "") for r in tie)
    checks = (
        _banner("good", "Every check passes.") if not blockers else
        _banner("warn", "This cutover can't go ahead yet:")
        + "<ul>" + "".join(f"<li>{_esc(b)}</li>" for b in blockers) + "</ul>"
    )
    report = (
        checks
        + f"<h3>Tie-out at {_esc(plan.get('cutover_date'))}</h3>" + _tie_out(tie)
        + (_banner("warn", "A/R or A/P doesn't match what QuickBooks has open. The signer has to "
                           "accept the difference shown here.") if differs else "")
        + f"<h3>Invoices and bills created (from QuickBooks, {_esc(plan.get('open_items_as_of'))})</h3>"
        + _documents(docs)
        + '<p class="muted">They are recorded, not posted: their amounts are already in A/R and A/P.</p>'
        + f'<p class="muted small">Report fingerprint <code>{_esc(plan.get("report_hash"))}</code></p>'
    )
    out += _card("Cutover report", report)
    if blockers:
        return out
    accept = ('<label class="rg-check"><input type="checkbox" name="accept_differences" required>'
              "<span>I accept the differences shown in the tie-out.</span></label>" if differs else "")
    out += _card("Sign off and cut over", (
        f'<form method="post" action="/t/{t}/connect/cutover">'
        f'<input type="hidden" name="cutover_date" value="{_esc(plan.get("cutover_date"))}">'
        f'<input type="hidden" name="report_hash" value="{_esc(plan.get("report_hash"))}">'
        '<label for="signed_by">Signed off by (your accountant)</label>'
        '<input id="signed_by" name="signed_by" required autocomplete="name">'
        '<label class="rg-check"><input type="checkbox" name="reviewed" required>'
        "<span>The person named here has reviewed this report.</span></label>"
        f"{accept}"
        '<button class="btn" type="submit">Cut over to RGNR8</button></form>'))
    return out
