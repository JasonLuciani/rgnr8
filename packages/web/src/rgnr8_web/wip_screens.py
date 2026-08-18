"""The work-in-progress schedule — the page an accountant asks for by name.

A contractor bills on the schedule the customer agreed to and spends on the
schedule the weather agreed to. Left alone, a job 70% built and 40% billed
reports a loss and the same job next month reports a windfall. This page shows
what was actually earned against what was actually billed, per job, and posts
the one entry that makes the balance sheet agree with it.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _minor, _num, _seq, money
from .job_screens import _pct


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


def render_wip_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card("Work in progress unavailable", '<div class="banner warn">The ledger '
                 f"service isn't reachable right now.</div>{extra}")


def render_wip(
    tenant: str,
    schedule: Mapping[str, object],
    through: str,
    *,
    can_post: bool,
    message: str = "",
    error: str = "",
) -> str:
    rows = [r for r in _seq(schedule.get("rows")) if isinstance(r, Mapping)]
    totals = schedule.get("totals") if isinstance(schedule.get("totals"), Mapping) else {}
    assert isinstance(totals, Mapping)

    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    if not rows:
        return head + _card("Work in progress", (
            '<p class="muted">No jobs are running. A job that has not started and one that '
            "is closed are both out of the schedule.</p>"
        ))

    cells = ""
    warnings = ""
    for r in rows:
        cells += (
            f"<tr><td>{_esc(r.get('name'))}"
            f'<br><span class="muted" style="font-size:12px">'
            f'{_esc(str(r.get("cost_method")).lower().replace("_", " "))}</span></td>'
            f"{_num(r.get('contract_minor'))}"
            f"{_num(r.get('estimated_cost_minor'))}"
            f"{_num(r.get('cost_to_date_minor'))}"
            f'<td class="num">{_pct(r.get("percent_complete_ppm"))}</td>'
            f"{_num(r.get('earned_revenue_minor'))}"
            f"{_num(r.get('billed_minor'))}"
            f"{_num(r.get('under_billed_minor'))}"
            f"{_num(r.get('over_billed_minor'))}</tr>"
        )
        if str(r.get("estimate_exceeded")) == "True":
            warnings += _banner(
                "warn",
                f"{r.get('name')}: cost has passed the estimate, so its percentage is capped "
                "at 100%. Until the estimate is revised the schedule cannot say anything "
                "useful about that job.",
            )
        loss = _minor(r.get("projected_loss_minor")) or 0
        if loss:
            warnings += _banner(
                "warn",
                f"{r.get('name')} is expected to lose {money(loss)}. A foreseen loss belongs "
                "in the books as soon as it is foreseen — post it deliberately below.",
            )

    body = (
        "<table><thead><tr><th>Job</th><th class='num'>Contract</th>"
        "<th class='num'>Estimated cost</th><th class='num'>Cost to date</th>"
        "<th class='num'>Complete</th><th class='num'>Earned</th><th class='num'>Billed</th>"
        "<th class='num'>Under-billed</th><th class='num'>Over-billed</th></tr></thead>"
        f"<tbody>{cells}</tbody></table>{warnings}"
        f'<p class="muted">{money(totals.get("earned_revenue_minor"))} earned against '
        f'{money(totals.get("billed_minor"))} billed. '
        f'{money(totals.get("under_billed_minor"))} is work done and not invoiced; '
        f'{money(totals.get("over_billed_minor"))} is money taken for work not yet done.</p>'
    )
    out = head + _card(f"Work in progress{f' through {_esc(through)}' if through else ''}", body)

    if can_post:
        out += _card("Post the adjustment", (
            f'<form method="post" action="/t/{_esc(tenant)}/books/wip" class="grid">'
            '<label>Dated<input name="date" placeholder="YYYY-MM-DD" required></label>'
            '<label>Through<input name="through" placeholder="YYYY-MM-DD"></label>'
            '<label>Losses<select name="include_loss_provision">'
            '<option value="">report them only</option>'
            '<option value="1">post the full provision</option></select></label>'
            '<div style="grid-column:1/-1"><button type="submit">Make the books agree</button>'
            '<span class="muted" style="margin-left:8px">The entry is the difference to the '
            "correct balance, never a fresh accrual, so running it twice changes nothing.</span>"
            "</div></form>"
            '<p class="muted">A loss provision is off by default: “we now think this will cost '
            "more than it pays” is a judgement about the future, not a fact in the ledger.</p>"
        ))
    return out


__all__ = ["render_wip", "render_wip_unavailable"]
