"""The bank feed review inbox — where a bank line becomes an accounting fact.

This is the screen an owner or bookkeeper lives in. Each line the bank sent sits
here until somebody says what it was: **accept** it into an account, **match** it
to the invoice or bill it settles, or **exclude** it as not a business event.
Nothing posts to the ledger until one of those happens.

Every suggestion shows *why* it's suggested — a rule you wrote, or the fact that
you categorized this vendor the same way five times before — and how confident
that makes it. A suggestion with no basis says so rather than guessing, because a
plausible-looking wrong category is worse than an obvious blank.

Amounts arrive as integer minor-unit strings and are formatted exactly.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _minor, _seq, money


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


_SOURCE_LABEL = {
    "rule": "your rule",
    "learned": "your history",
    "none": "",
}


def _confidence_chip(confidence: float, source: str) -> str:
    if not source or source == "none":
        return ""
    pct = round(max(0.0, min(1.0, confidence)) * 100)
    color = "var(--rg-sage)" if pct >= 90 else "var(--rg-watch,#b8860b)"
    label = _SOURCE_LABEL.get(source, source)
    return (
        f'<span class="chip" style="padding:2px 9px;font-size:11px;color:{color}">'
        f"{pct}% · from {_esc(label)}</span>"
    )


def _account_options(accounts: Mapping[str, object], selected: str,
                     *, exclude: str = "") -> str:
    out = ['<option value="">— choose an account —</option>']
    for a in _seq(accounts.get("accounts")):
        if not isinstance(a, Mapping):
            continue
        code = str(a.get("code"))
        if code == exclude:
            continue  # a line can never be categorized to its own bank account
        sel = " selected" if code == selected else ""
        out.append(
            f'<option value="{_esc(code)}"{sel}>{_esc(code)} — {_esc(a.get("name"))}</option>'
        )
    return "".join(out)


def _match_form(tenant: str, item: Mapping[str, object]) -> str:
    matches = _seq(item.get("matches"))
    if not matches:
        return ""
    options = []
    for m in matches:
        if not isinstance(m, Mapping):
            continue
        kind = str(m.get("kind"))
        if kind == "transfer":
            # A transfer's other leg isn't a document to pay; it's the same money
            # seen twice. Surfacing it as a hint is honest; offering to "pay" it
            # would not be.
            continue
        fit = " (exact)" if m.get("fit") == "exact" else " (part payment)"
        options.append(
            f'<option value="{_esc(kind)}:{_esc(m.get("id"))}">'
            f'{_esc(m.get("id"))} — {_esc(m.get("label"))} · open {money(m.get("openMinor"))}'
            f"{fit}</option>"
        )
    hints = [
        f'<p class="note">Looks like the other side of a transfer: {_esc(m.get("label"))}. '
        "Categorize both legs to the same transfer account rather than to income or a cost.</p>"
        for m in matches
        if isinstance(m, Mapping) and m.get("kind") == "transfer"
    ]
    if not options:
        return "".join(hints)
    return (
        f'<form method="post" action="/t/{_esc(tenant)}/inbox/{_esc(item.get("id"))}/match" '
        'style="display:flex;gap:6px;align-items:center;margin:6px 0 0;flex-wrap:wrap">'
        f'<span class="muted" style="font-size:12px">or settle</span>'
        f'<select name="match">{"".join(options)}</select>'
        '<button class="btn" style="padding:4px 12px;margin:0" type="submit">Match</button>'
        "</form>" + "".join(hints)
    )


def _row(tenant: str, item: Mapping[str, object], accounts: Mapping[str, object],
         can_post: bool) -> str:
    amount = _minor(item.get("amount_minor")) or 0
    cls = "num neg" if amount < 0 else "num"
    txn_id = str(item.get("id"))
    suggestion = item.get("suggestion")
    sug = suggestion if isinstance(suggestion, Mapping) else {}
    sug_code = str(sug.get("account_code") or "")

    desc = _esc(item.get("description")) or '<span class="muted">—</span>'
    party = str(item.get("counterparty") or "")
    if party:
        desc += f'<br><span class="muted" style="font-size:12px">{_esc(party)}</span>'

    if not can_post:
        action = _esc(sug.get("account_name") or "") or '<span class="muted">—</span>'
        return (
            f"<tr><td class='muted'>{_esc(item.get('date'))}</td><td>{desc}</td>"
            f"<td class='{cls}'>{money(item.get('amount_minor'))}</td>"
            f"<td>{action}</td><td></td></tr>"
        )

    why = (
        f'<div style="margin-top:4px">{_confidence_chip(float(sug.get("confidence") or 0), str(sug.get("source") or ""))}'
        f' <span class="muted" style="font-size:12px">{_esc(sug.get("reason"))}</span></div>'
    )
    accept = (
        f'<form method="post" action="/t/{_esc(tenant)}/inbox/{_esc(txn_id)}/accept" '
        'style="display:flex;gap:6px;align-items:center;margin:0;flex-wrap:wrap">'
        f'<select name="category_code">'
        f'{_account_options(accounts, sug_code, exclude=str(item.get("account_code")))}</select>'
        '<button class="btn" style="padding:4px 12px;margin:0" type="submit">Accept</button>'
        "</form>"
    )
    exclude = (
        f'<form method="post" action="/t/{_esc(tenant)}/inbox/{_esc(txn_id)}/exclude" '
        'style="margin:0">'
        '<input type="hidden" name="reason" value="Not a business transaction">'
        '<button class="btn ghost" style="padding:4px 10px;margin:0" type="submit" '
        'title="Not a business transaction — nothing will be posted">Exclude</button></form>'
    )
    return (
        f"<tr><td class='muted'>{_esc(item.get('date'))}</td><td>{desc}</td>"
        f"<td class='{cls}'>{money(item.get('amount_minor'))}</td>"
        f"<td>{accept}{why}{_match_form(tenant, item)}</td>"
        f"<td>{exclude}</td></tr>"
    )


def render_inbox(
    tenant: str,
    view: Mapping[str, object],
    accounts: Mapping[str, object],
    *,
    can_post: bool,
    message: str = "",
    error: str = "",
) -> str:
    """The review queue: everything the bank sent that isn't in the books yet."""
    items = [i for i in _seq(view.get("items")) if isinstance(i, Mapping)]
    rows = "".join(_row(tenant, i, accounts, can_post) for i in items)
    empty = (
        '<tr><td colspan="5" class="muted" style="text-align:center;padding:24px">'
        "Nothing waiting — every bank line has been dealt with.</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Date</th><th>Description</th>'
        '<th class="num">Amount</th><th>What was it?</th><th></th>'
        "</tr></thead><tbody>" + (rows or empty) + "</tbody></table></div>"
    )

    pending = _minor(view.get("pending")) or 0
    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)
    head += _banner(
        "warn" if pending else "good",
        f"{pending} to review · {view.get('posted') or 0} posted · "
        f"{view.get('matched') or 0} matched to invoices and bills · "
        f"{view.get('excluded') or 0} excluded"
        if pending
        else f"All caught up — {view.get('posted') or 0} posted, "
             f"{view.get('matched') or 0} matched, {view.get('excluded') or 0} excluded.",
    )
    summary = (
        f'<p class="sub">Waiting: <strong>{money(view.get("pending_in_minor"))}</strong> in · '
        f'<strong>{money(view.get("pending_out_minor"))}</strong> out · '
        f'<a class="btn-link" href="/t/{_esc(tenant)}/inbox/rules">Rules</a> · '
        f'<a class="btn-link" href="/t/{_esc(tenant)}/inbox?status=POSTED">Already posted</a> · '
        f'<a class="btn-link" href="/t/{_esc(tenant)}/inbox?status=EXCLUDED">Excluded</a></p>'
    )

    out = head + summary + _card("For review", table)
    if can_post and pending:
        out += _bulk_form(tenant)
    return out


def _bulk_form(tenant: str) -> str:
    return _card(
        "Accept everything I'm sure about",
        f'<form method="post" action="/t/{_esc(tenant)}/inbox/bulk-accept">'
        '<div class="grid2">'
        "<div><label>Only accept suggestions at least this confident</label>"
        '<select name="min_confidence">'
        '<option value="1">Certain — only my own rules</option>'
        '<option value="0.9">Very confident (90%+)</option>'
        '<option value="0.75">Fairly confident (75%+)</option>'
        "</select></div></div>"
        '<p class="note">Lines below the bar, and lines with no suggestion at all, '
        "stay in the queue. Anything accepted here can still be undone — it posts a "
        "reversing entry rather than deleting anything.</p>"
        '<button class="btn" type="submit">Accept the confident ones</button></form>',
    )


def render_actioned(tenant: str, status: str, view: Mapping[str, object],
                    *, can_post: bool) -> str:
    """Lines already dealt with — what they became, and a way to undo."""
    items = [i for i in _seq(view.get("items")) if isinstance(i, Mapping)]
    rows = []
    for i in items:
        amount = _minor(i.get("amount_minor")) or 0
        cls = "num neg" if amount < 0 else "num"
        if i.get("doc_id"):
            became = f'Settled {_esc(i.get("doc_kind"))} {_esc(i.get("doc_id"))}'
        elif i.get("category_code"):
            became = f'Posted to {_esc(i.get("category_code"))}'
        else:
            became = f'<span class="muted">{_esc(i.get("note")) or "Excluded"}</span>'
        undo = ""
        if can_post:
            undo = (
                f'<form method="post" action="/t/{_esc(tenant)}/inbox/{_esc(i.get("id"))}/undo" '
                'style="margin:0"><button class="btn ghost" style="padding:4px 10px;margin:0" '
                'type="submit">Undo</button></form>'
            )
        rows.append(
            f"<tr><td class='muted'>{_esc(i.get('date'))}</td>"
            f"<td>{_esc(i.get('description'))}</td>"
            f"<td class='{cls}'>{money(i.get('amount_minor'))}</td>"
            f"<td>{became}</td>"
            f"<td class='muted' style='font-size:12px'>{_esc(i.get('entry_id'))}</td>"
            f"<td>{undo}</td></tr>"
        )
    empty = (
        '<tr><td colspan="6" class="muted" style="text-align:center;padding:24px">'
        "Nothing here yet.</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Date</th><th>Description</th>'
        '<th class="num">Amount</th><th>Became</th><th>Journal entry</th><th></th>'
        "</tr></thead><tbody>" + ("".join(rows) or empty) + "</tbody></table></div>"
    )
    title = {"POSTED": "Posted to the books", "MATCHED": "Matched to invoices and bills",
             "EXCLUDED": "Excluded"}.get(status, status)
    back = f'<a class="btn-link" href="/t/{_esc(tenant)}/inbox">Back to review</a>'
    note = (
        '<p class="note">Undo posts a reversing entry — the original stays in the '
        "journal, because the books are append-only. Then the line comes back to "
        "the review queue.</p>"
    )
    return _card(title, table + (note if can_post else ""), back)


# --- rules -------------------------------------------------------------------

def render_rules(
    tenant: str,
    rules: Mapping[str, object],
    accounts: Mapping[str, object],
    *,
    can_post: bool,
    message: str = "",
    error: str = "",
) -> str:
    """The rules that pre-categorize the feed, and the form to add one."""
    rows = []
    for r in _seq(rules.get("rules")):
        if not isinstance(r, Mapping):
            continue
        conditions: list[str] = []
        if r.get("description_contains"):
            conditions.append(f'description contains “{_esc(r.get("description_contains"))}”')
        if r.get("counterparty_equals"):
            conditions.append(f'payee is {_esc(r.get("counterparty_equals"))}')
        if r.get("sign") == "in":
            conditions.append("money in")
        elif r.get("sign") == "out":
            conditions.append("money out")
        auto = (
            '<span style="color:var(--rg-watch,#b8860b);font-weight:700;font-size:12px">'
            "posts automatically</span>"
            if r.get("auto_post")
            else '<span class="muted" style="font-size:12px">suggests only</span>'
        )
        remove = ""
        if can_post:
            remove = (
                f'<form method="post" action="/t/{_esc(tenant)}/inbox/rules/{_esc(r.get("id"))}/delete" '
                'style="margin:0"><button class="btn ghost" style="padding:4px 10px;margin:0" '
                'type="submit">Remove</button></form>'
            )
        rows.append(
            f"<tr><td><strong>{_esc(r.get('id'))}</strong></td>"
            f"<td>{' and '.join(conditions)}</td>"
            f"<td>{_esc(r.get('account_code'))}</td><td>{auto}</td><td>{remove}</td></tr>"
        )
    empty = (
        '<tr><td colspan="5" class="muted" style="text-align:center;padding:24px">'
        "No rules yet. Add one below and the feed starts pre-filling itself.</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Name</th><th>When</th>'
        "<th>Categorize to</th><th>Then</th><th></th></tr></thead><tbody>"
        + ("".join(rows) or empty) + "</tbody></table></div>"
    )

    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    back = f'<a class="btn-link" href="/t/{_esc(tenant)}/inbox">Back to review</a>'
    out = head + _card("Categorization rules", table, back)
    if can_post:
        out += _card(
            "Add a rule",
            f'<form method="post" action="/t/{_esc(tenant)}/inbox/rules">'
            '<div class="grid3">'
            '<div><label>Name</label><input name="id" placeholder="rent"></div>'
            '<div><label>Description contains</label>'
            '<input name="description_contains" placeholder="RIVERSIDE"></div>'
            '<div><label>Or payee is exactly</label>'
            '<input name="counterparty_equals" placeholder="Riverside Properties"></div>'
            "</div>"
            '<div class="grid3">'
            "<div><label>Direction</label><select name=\"sign\">"
            '<option value="">Either</option><option value="out">Money out</option>'
            '<option value="in">Money in</option></select></div>'
            "<div><label>Categorize to</label>"
            f'<select name="account_code">{_account_options(accounts, "")}</select></div>'
            '<div><label>Post without review?</label><select name="auto_post">'
            '<option value="">No — just suggest it</option>'
            '<option value="1">Yes — post it straight through</option></select></div>'
            "</div>"
            '<p class="note">A rule needs at least one condition. "Post without review" '
            "means matching lines skip the queue entirely — use it only for the ones "
            "you would always categorize the same way, like rent or a bank fee.</p>"
            '<button class="btn" type="submit">Add rule</button></form>',
        )
    return out


def render_feed_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card(
        "Bank feed unavailable",
        _banner("warn", "The ledger service isn't reachable right now.")
        + "<p class=\"muted\">Nothing has been lost — this screen just can't read the "
        "queue at the moment.</p>" + extra,
    )


__all__ = [
    "render_inbox", "render_actioned", "render_rules", "render_feed_unavailable",
]
