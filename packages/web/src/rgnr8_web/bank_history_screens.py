"""Bank history: every bank and card transaction QuickBooks holds, as RGNR8 keeps it.

A copy for reading and for the cash outlook, not a register: nothing here posts.
The page says what span it covers, whether each account ties to QuickBooks' own
running balance and to the books, money in and out by month, and the lines.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _banner, _card, _esc, _seq, money
from .components import status_chip
from .provenance_labels import Provenance

# The newest lines shown; the month totals always cover everything.
LINE_LIMIT = 200


def _minor(v: object) -> int:
    try:
        return int(str(v or 0))
    except ValueError:
        return 0


def _tie_out(rows: list[object]) -> str:
    if not rows:
        return ""
    body = []
    for r in rows:
        if not isinstance(r, Mapping):
            continue
        diff = _minor(r.get("difference_minor"))
        status = (status_chip("GOOD", label="Ties to QuickBooks") if diff == 0
                  else status_chip("RISK", label=f"Off by {money(abs(diff))}"))
        body.append(
            f"<tr><td>{_esc(r.get('account_name') or r.get('account_code'))}</td>"
            f"<td class=\"num\">{money(r.get('opening_minor'))}</td>"
            f"<td class=\"num\">{money(r.get('movement_minor'))}</td>"
            f"<td class=\"num\">{money(r.get('closing_minor'))}</td><td>{status}</td></tr>")
    table = ("<table><thead><tr><th>Account</th><th class=\"num\">Start</th>"
             "<th class=\"num\">Movement</th><th class=\"num\">End</th><th></th></tr></thead>"
             f"<tbody>{''.join(body)}</tbody></table>")
    return _card("Check against QuickBooks (latest import)", table,
                 provenance=Provenance.IMPORTED)


def _months(periods: list[object]) -> str:
    """Money in and out of the bank by month, and card spending beside it.

    A card charge is spending but not yet cash leaving: that happens when the
    card is paid, which shows up in the bank's money out. So card spending is
    its own column, never added to the bank's."""
    by: dict[str, dict[str, int]] = {}
    for p in periods:
        if not isinstance(p, Mapping):
            continue
        m = by.setdefault(str(p.get("period")), {"in": 0, "out": 0, "card": 0})
        if p.get("account_kind") == "card":
            m["card"] += _minor(p.get("out_minor"))
        else:
            m["in"] += _minor(p.get("in_minor"))
            m["out"] += _minor(p.get("out_minor"))
    if not by:
        return ""
    rows = "".join(
        f"<tr><td>{_esc(month)}</td><td class=\"num\">{money(v['in'])}</td>"
        f"<td class=\"num\">{money(v['out'])}</td><td class=\"num\">{money(v['in'] - v['out'])}</td>"
        f"<td class=\"num\">{money(v['card'])}</td></tr>"
        for month, v in sorted(by.items(), reverse=True))
    table = ("<table><thead><tr><th>Month</th><th class=\"num\">Money in</th>"
             "<th class=\"num\">Money out</th><th class=\"num\">Net</th>"
             "<th class=\"num\">Card spending</th></tr></thead>"
             f"<tbody>{rows}</tbody></table>"
             '<p class="muted">Money in and out are the bank accounts. Card spending is '
             "shown apart: it leaves the bank when the card is paid, as part of money out.</p>")
    return _card("By month", table, provenance=Provenance.IMPORTED)


def _filters(tenant: str, accounts: list[tuple[str, str]], account: str, frm: str, to: str) -> str:
    opts = '<option value="">All accounts</option>' + "".join(
        f'<option value="{_esc(code)}"{" selected" if code == account else ""}>{_esc(name)}</option>'
        for code, name in accounts)
    return (
        f'<form method="get" action="/t/{_esc(tenant)}/inbox/history" class="grid">'
        f"<label>Account<select name=\"account\">{opts}</select></label>"
        f'<label>From<input type="date" name="from" value="{_esc(frm)}"></label>'
        f'<label>To<input type="date" name="to" value="{_esc(to)}"></label>'
        '<div><button type="submit">Show</button></div></form>')


def _lines(lines: list[object], total: int) -> str:
    shown = [x for x in lines if isinstance(x, Mapping)][-LINE_LIMIT:][::-1]
    if not shown:
        return '<p class="muted">No transactions in this range.</p>'
    rows = []
    for x in shown:
        amt = _minor(x.get("amount_minor"))
        rows.append(
            f"<tr><td>{_esc(x.get('date'))}</td><td>{_esc(x.get('account_name'))}</td>"
            f"<td>{_esc(x.get('txn_type'))}</td><td>{_esc(x.get('name'))}</td>"
            f"<td>{_esc(x.get('memo') or x.get('split'))}</td>"
            f"<td class=\"num\">{money(amt) if amt > 0 else ''}</td>"
            f"<td class=\"num\">{money(-amt) if amt < 0 else ''}</td></tr>")
    more = (f'<p class="muted">The newest {len(shown):,} of {total:,}. '
            "Narrow the dates or pick an account to see the rest.</p>" if total > len(shown) else "")
    return ("<table><thead><tr><th>Date</th><th>Account</th><th>Type</th><th>Name</th>"
            "<th>Memo</th><th class=\"num\">In</th><th class=\"num\">Out</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>{more}")


def render_bank_history(
    tenant: str, body: Mapping[str, object], listed: "Mapping[str, object] | None" = None, *,
    account: str = "", frm: str = "", to: str = "",
    can_import: bool = False, message: str = "", error: str = "",
) -> str:
    """`body` is the whole history's summary (coverage, months, tie-out); `listed`
    the lines for the chosen account and dates (defaults to `body`)."""
    listed = body if listed is None else listed
    covered_from = str(body.get("covered_from") or "")
    covered_to = str(body.get("covered_to") or "")
    imported_at = str(body.get("imported_at") or "")[:16].replace("T", " ")
    head = ("<h1>Bank history</h1><p class=\"sub\">Every bank and card transaction QuickBooks "
            "has, copied for the cash outlook. Nothing here posts to the books.</p>")
    banners = (_banner("good", message) if message else "") + (_banner("warn", error) if error else "")
    action = (f'<form method="post" action="/t/{_esc(tenant)}/inbox/history/import">'
              '<button type="submit">Bring over history</button></form>' if can_import else "")
    if not covered_from:
        intro = ("<p>No history yet. Bring over history copies up to two years of transactions "
                 "on your bank and card accounts from QuickBooks. It can take a minute or two; "
                 "if there's a lot, press it again to carry on.</p>"
                 if can_import else "<p>No history yet. An owner can bring it over from QuickBooks.</p>")
        return head + banners + _card("From QuickBooks", intro + action)
    span = (f"<p>Covers {_esc(covered_from)} to {_esc(covered_to)}. Last brought over "
            f"{_esc(imported_at)} UTC. Refresh from QuickBooks keeps the recent weeks current.</p>")
    periods = _seq(body.get("periods"))
    accounts = sorted({(str(p.get("account_code")), str(p.get("account_name")))
                       for p in periods if isinstance(p, Mapping)}, key=lambda a: a[1])
    lines = _seq(listed.get("lines"))
    total = int(str(listed.get("count") or len(lines)))
    return (head + banners + _card("From QuickBooks", span + action, provenance=Provenance.IMPORTED)
            + _tie_out(_seq(body.get("tie_out"))) + _months(periods)
            + _card("Transactions", _filters(tenant, accounts, account, frm, to) + _lines(lines, total),
                    provenance=Provenance.IMPORTED))
