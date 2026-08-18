"""Recurring transactions — rent, subscriptions, retainers.

The obvious design is a scheduler that posts on its own. It is also the wrong
one for a small business's books. A rule that fires unattended keeps paying rent
after the lease ends, keeps invoicing a client who cancelled, and does it
silently. By the time anyone looks, the books hold months of confident fiction.

So this screen leads with **what's due**, and posting is a button. Nobody
retypes rent twelve times; nobody discovers in November that it has been posting
to a closed studio since March.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _minor, _num, _seq, money


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


_FREQUENCY_LABEL = {
    "DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year",
}


def _schedule_words(t: Mapping[str, object]) -> str:
    """"every month", "every 3 months", "every 2 weeks" — not a cron string."""
    unit = _FREQUENCY_LABEL.get(str(t.get("frequency")), str(t.get("frequency")).lower())
    interval = _minor(t.get("interval")) or 1
    every = f"every {unit}" if interval == 1 else f"every {interval} {unit}s"
    since = f" from {_esc(t.get('start_date'))}"
    until = f" until {_esc(t.get('end_date'))}" if t.get("end_date") else ""
    return f"{every}{since}{until}"


def render_recurring(
    tenant: str,
    templates: Mapping[str, object],
    due: Mapping[str, object],
    accounts: Mapping[str, object],
    as_of: str,
    *,
    can_post: bool,
    message: str = "",
    error: str = "",
) -> str:
    """What's due, what's memorized, and the form to memorize another."""
    items = [d for d in _seq(due.get("due")) if isinstance(d, Mapping)]

    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    out = head + _due_card(tenant, items, as_of, can_post)
    out += _templates_card(tenant, templates, can_post)
    if can_post:
        out += _new_form(tenant, accounts)
    return out


def _due_card(tenant: str, items: list[Mapping[str, object]], as_of: str,
              can_post: bool) -> str:
    if not items:
        return _card(
            "Nothing due",
            '<p class="muted">Every memorized transaction is up to date as of '
            f"{_esc(as_of)}.</p>",
        )
    rows = "".join(
        f"<tr><td class='muted'>{_esc(d.get('date'))}</td>"
        f"<td>{_esc(d.get('name'))}<br>"
        f'<span class="muted" style="font-size:12px">{_esc(d.get("memo"))}</span></td>'
        f"{_num(d.get('amount_minor'))}</tr>"
        for d in items
    )
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Date</th><th>What</th>'
        '<th class="num">Amount</th></tr></thead><tbody>' + rows + "</tbody></table></div>"
    )
    action = ""
    if can_post:
        action = (
            f'<form method="post" action="/t/{_esc(tenant)}/books/recurring/run">'
            f'<input type="hidden" name="as_of" value="{_esc(as_of)}">'
            '<p class="note">Nothing posts on its own. These are waiting because '
            "you haven't posted them — which is how you find out a subscription is "
            "still running after you cancelled it.</p>"
            f'<button class="btn" type="submit">Post all {len(items)}</button></form>'
        )
    return _card(f"{len(items)} due as of {_esc(as_of)}", table + action)


def _templates_card(
    tenant: str, templates: Mapping[str, object], can_post: bool
) -> str:
    rows = []
    for t in _seq(templates.get("recurring")):
        if not isinstance(t, Mapping):
            continue
        template_id = _esc(t.get("id"))
        total = sum(
            _minor(l.get("amount_minor")) or 0
            for l in _seq(t.get("lines"))
            if isinstance(l, Mapping) and l.get("side") == "DEBIT"
        )
        state = (
            '<span class="muted" style="font-size:12px">paused</span>'
            if not t.get("active")
            else '<span style="color:var(--rg-sage);font-weight:700;font-size:12px">on</span>'
        )
        remove = ""
        if can_post:
            remove = (
                f'<form method="post" action="/t/{_esc(tenant)}/books/recurring/'
                f'{template_id}/delete" style="margin:0">'
                '<button class="btn ghost" style="padding:4px 10px;margin:0" '
                'type="submit">Remove</button></form>'
            )
        last = t.get("last_posted") or "never"
        rows.append(
            f"<tr><td><strong>{_esc(t.get('name'))}</strong><br>"
            f'<span class="muted" style="font-size:12px">{_schedule_words(t)}</span></td>'
            f"{_num(str(total))}"
            f"<td class='muted'>{_esc(last)}</td><td>{state}</td><td>{remove}</td></tr>"
        )
    empty = (
        '<tr><td colspan="5" class="muted" style="text-align:center;padding:24px">'
        "Nothing memorized yet.</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr><th>What</th>'
        '<th class="num">Amount</th><th>Last posted</th><th></th><th></th>'
        "</tr></thead><tbody>" + ("".join(rows) or empty) + "</tbody></table></div>"
    )
    back = f'<a class="btn-link" href="/t/{_esc(tenant)}/books">Back to books</a>'
    return _card("Memorized transactions", table, back)


def _new_form(tenant: str, accounts: Mapping[str, object]) -> str:
    options = "".join(
        f'<option value="{_esc(a.get("code"))}">{_esc(a.get("code"))} — '
        f'{_esc(a.get("name"))}</option>'
        for a in _seq(accounts.get("accounts"))
        if isinstance(a, Mapping)
    )
    if not options:
        return _card("Memorize a transaction",
                     '<p class="muted">Set up a chart of accounts first.</p>')
    lines = "".join(
        '<div class="grid3">'
        f'<div><label>Account</label><select name="code{i}">{options}</select></div>'
        f'<div><label>Debit</label><input name="debit{i}" placeholder="0.00" '
        'inputmode="decimal"></div>'
        f'<div><label>Credit</label><input name="credit{i}" placeholder="0.00" '
        'inputmode="decimal"></div></div>'
        for i in range(1, 5)
    )
    return _card(
        "Memorize a transaction",
        f'<form method="post" action="/t/{_esc(tenant)}/books/recurring">'
        '<div class="grid3">'
        '<div><label>Name</label><input name="name" placeholder="Studio rent"></div>'
        '<div><label>How often</label><select name="frequency">'
        '<option value="MONTHLY">Monthly</option>'
        '<option value="WEEKLY">Weekly</option>'
        '<option value="YEARLY">Yearly</option>'
        '<option value="DAILY">Daily</option></select></div>'
        '<div><label>Every N periods</label><input name="interval" value="1"></div>'
        "</div>"
        '<div class="grid3">'
        '<div><label>Starting</label><input name="start_date" placeholder="YYYY-MM-DD"></div>'
        '<div><label>Ending (optional)</label>'
        '<input name="end_date" placeholder="YYYY-MM-DD"></div>'
        '<div><label>Memo</label><input name="memo" placeholder="Riverside Properties"></div>'
        "</div>"
        + lines
        + '<p class="note">Debits must equal credits — a template that can never '
        "balance is refused now rather than failing quietly on the first of the "
        "month. Set an end date when you know one: it is the difference between "
        "rent that stops with the lease and rent that posts forever.</p>"
        '<button class="btn" type="submit">Memorize it</button></form>',
    )


def render_run_result(tenant: str, result: Mapping[str, object], as_of: str) -> str:
    """What posted, and — more importantly — what didn't and why."""
    posted = _minor(result.get("posted")) or 0
    failures = [f for f in _seq(result.get("failures")) if isinstance(f, Mapping)]

    head = _banner(
        "good" if not failures else "warn",
        f"Posted {posted}." + (
            f" {len(failures)} couldn't post and are still waiting."
            if failures else ""
        ),
    )
    blocks = ""
    if failures:
        rows = "".join(
            f"<tr><td class='muted'>{_esc(f.get('date'))}</td>"
            f"<td>{_esc(f.get('id'))}</td><td>{_esc(f.get('error'))}</td></tr>"
            for f in failures
        )
        blocks += _card(
            "Not posted",
            '<div class="table-scroll"><table><thead><tr><th>Date</th><th>What</th>'
            "<th>Why not</th></tr></thead><tbody>" + rows + "</tbody></table></div>"
            + '<p class="note">These are still listed as due, so nothing is lost. '
            "A closed month is the usual reason — reopen it or date the entry into "
            "an open one.</p>",
        )
    back = (
        f'<a class="btn-link" href="/t/{_esc(tenant)}/books/recurring?as_of={_esc(as_of)}">'
        "Back to recurring</a>"
    )
    return head + blocks + _card("Next", f'<p class="note">{back}</p>')


def render_recurring_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card(
        "Recurring transactions unavailable",
        _banner("warn", "The ledger service isn't reachable right now.") + extra,
    )
