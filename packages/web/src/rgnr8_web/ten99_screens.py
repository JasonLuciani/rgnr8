"""1099-NEC — what you paid contractors, and who you can't file for.

The mechanics are simple. The timing is what hurts: the $600 threshold is
crossed in March, the W-9 is never collected, and it surfaces in January when
the contractor has moved on and the business is filing without a TIN.

So this screen is built around the warnings rather than the totals. Who is over
the threshold with no tax id. Who is under it but heading there. And what money
left the bank to a contractor without ever going through a bill — a prompt, not
a number, because guessing produces a figure nobody can defend to the IRS.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _num, _seq, money


def _mask_tin(tax_id: str) -> str:
    """Show only the last 4 of a TIN/EIN on screen (e.g. ``***-**-6789``); the full
    value is never rendered in HTML. The unmasked value stays server-side for the
    actual 1099 filing."""
    digits = "".join(c for c in tax_id if c.isdigit())
    if len(digits) < 4:
        return "***" if tax_id else ""
    return f"***-**-{digits[-4:]}"


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


def render_ten99(
    tenant: str,
    year: str,
    data: Mapping[str, object],
    *,
    can_post: bool,
    message: str = "",
    error: str = "",
) -> str:
    rows = [r for r in _seq(data.get("rows")) if isinstance(r, Mapping)]
    below = [b for b in _seq(data.get("below_threshold")) if isinstance(b, Mapping)]
    missing = [m for m in _seq(data.get("possibly_missing")) if isinstance(m, Mapping)]
    needs_w9 = [r for r in rows if r.get("needs_w9")]

    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    if needs_w9:
        names = ", ".join(str(r.get("vendor_name")) for r in needs_w9)
        head += _banner(
            "warn",
            f"You owe a 1099 to {names} and have no tax id on file. "
            "A W-9 collected now is a phone call; collected in January it is a "
            "search for someone who has moved on.",
        )
    elif rows:
        head += _banner("good", f"{len(rows)} form(s) to file for {year}, all with a tax id.")

    out = head + _card(
        f"1099-NEC — {_esc(year)}",
        _reportable_table(rows, data) + _period_form(tenant, year),
    )
    if below:
        out += _below_card(below)
    if missing:
        out += _missing_card(missing)
    return out


def _reportable_table(
    rows: list[Mapping[str, object]], data: Mapping[str, object]
) -> str:
    body = []
    for r in rows:
        tax = (
            _esc(_mask_tin(str(r.get("tax_id")))) if r.get("tax_id")
            else '<span style="color:var(--rg-risk,#b4462f);font-weight:700;'
                 'font-size:12px">W-9 needed</span>'
        )
        body.append(
            f"<tr><td>{_esc(r.get('vendor_name'))}</td><td>{tax}</td>"
            f"{_num(r.get('amount_minor'))}</tr>"
        )
    empty = (
        '<tr><td colspan="3" class="muted" style="text-align:center;padding:24px">'
        "Nobody was paid enough to need a form this year.</td></tr>"
    )
    threshold = money(data.get("threshold_minor"))
    return (
        '<div class="table-scroll"><table><thead><tr><th>Contractor</th>'
        '<th>Tax ID</th><th class="num">Box 1 — nonemployee compensation</th>'
        "</tr></thead><tbody>" + ("".join(body) or empty)
        + '<tr style="font-weight:700"><td colspan="2">Total</td>'
        + _num(data.get("total_minor")) + "</tr></tbody></table></div>"
        + f'<p class="note">A form is required above {threshold} paid in the '
        "calendar year. Amounts are what was actually paid, not what was billed.</p>"
    )


def _period_form(tenant: str, year: str) -> str:
    return (
        f'<form method="get" action="/t/{_esc(tenant)}/books/1099" '
        'style="display:flex;gap:8px;align-items:end;margin:12px 0 0">'
        f'<div><label>Year</label><input name="year" value="{_esc(year)}" '
        'style="width:110px"></div>'
        '<button class="btn" style="margin:0" type="submit">Show</button>'
        f'<a class="btn-link" href="/t/{_esc(tenant)}/bills" '
        'style="margin-left:auto">Vendors</a></form>'
    )


def _below_card(below: list[Mapping[str, object]]) -> str:
    rows = "".join(
        f"<tr><td>{_esc(b.get('vendor_name'))}</td>"
        f"{_num(b.get('amount_minor'))}{_num(b.get('short_by_minor'))}</tr>"
        for b in below
    )
    return _card(
        "Tracked, but under the threshold",
        '<div class="table-scroll"><table><thead><tr><th>Contractor</th>'
        '<th class="num">Paid this year</th>'
        '<th class="num">Short of needing a form by</th></tr></thead><tbody>'
        + rows + "</tbody></table></div>"
        + '<p class="note">No form is due for these yet. They are here because one '
        "more invoice moves them into the list above — which is the moment to ask "
        "for a W-9, not the following January.</p>",
    )


def _missing_card(missing: list[Mapping[str, object]]) -> str:
    rows = "".join(
        f"<tr><td class='muted'>{_esc(m.get('date'))}</td>"
        f"<td>{_esc(m.get('vendor_name'))}<br>"
        f'<span class="muted" style="font-size:12px">{_esc(m.get("description"))}</span>'
        f"</td>{_num(m.get('amount_minor'))}</tr>"
        for m in missing
    )
    return _card(
        "Paid from the bank, never billed",
        '<div class="table-scroll"><table><thead><tr><th>Date</th>'
        '<th>Looks like</th><th class="num">Amount</th></tr></thead><tbody>'
        + rows + "</tbody></table></div>"
        + '<p class="note">These went out of the bank to a name matching a '
        "contractor, but were never run through a bill — so they are <strong>not "
        "counted above</strong>. They probably should be. Enter them as bills "
        "against that vendor and they will be. This is a prompt rather than a "
        "number because guessing produces a total you cannot defend.</p>",
    )


def render_ten99_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card(
        "1099 report unavailable",
        _banner("warn", "The ledger service isn't reachable right now.") + extra,
    )
