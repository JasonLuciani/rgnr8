"""Classes and locations — the question a chart of accounts can't answer.

A chart of accounts tells you what you spent on wages. It cannot tell you what
*the catering side* spent on wages. Businesses that need that answer and don't
have classes end up doing one of two things: living with a P&L that averages
their lines of business into mush, or exploding the chart into "Wages —
Catering", "Wages — Dine-in", "Wages — Events" and losing the ability to ask
what wages cost altogether.

The design decision that makes it work is that values are **chosen from a list,
not typed**. A free-text box gives you "Denver", "denver", "Denver " and
"Dnever" — four lines of business where there is one, and a report that is
quietly wrong rather than obviously broken.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _num, _seq

_ANY_VALUE = '<span class="muted">any value</span>'


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


def dimension_selects(dimensions: Mapping[str, object], suffix: str = "") -> str:
    """Dropdowns for each defined dimension, for an entry or inbox form.

    Returns "" when nothing is defined, so the forms stay uncluttered for the
    businesses that don't need this.
    """
    blocks = []
    for d in _seq(dimensions.get("dimensions")):
        if not isinstance(d, Mapping):
            continue
        key = str(d.get("key"))
        required = bool(d.get("required"))
        options = ['<option value="">—</option>'] if not required else [
            '<option value="">— choose —</option>'
        ]
        for v in _seq(d.get("values")):
            options.append(f'<option value="{_esc(v)}">{_esc(v)}</option>')
        label = _esc(d.get("label")) + (" (required)" if required else "")
        blocks.append(
            f"<div><label>{label}</label>"
            f'<select name="dim_{_esc(key)}{_esc(suffix)}">{"".join(options)}</select></div>'
        )
    return "".join(blocks)


def render_dimensions(
    tenant: str,
    data: Mapping[str, object],
    *,
    can_post: bool,
    message: str = "",
    error: str = "",
) -> str:
    """The list of defined dimensions and the form to add one."""
    rows = []
    for d in _seq(data.get("dimensions")):
        if not isinstance(d, Mapping):
            continue
        key = _esc(d.get("key"))
        values = [str(v) for v in _seq(d.get("values"))]
        required = (
            '<span style="color:var(--rg-watch,#b8860b);font-weight:700;font-size:12px">'
            "required on income and costs</span>"
            if d.get("required")
            else '<span class="muted" style="font-size:12px">optional</span>'
        )
        remove = ""
        if can_post:
            remove = (
                f'<form method="post" action="/t/{_esc(tenant)}/books/dimensions/{key}/delete" '
                'style="margin:0"><button class="btn ghost" style="padding:4px 10px;margin:0" '
                'type="submit">Remove</button></form>'
            )
        rows.append(
            f"<tr><td><strong>{_esc(d.get('label'))}</strong><br>"
            f'<span class="muted" style="font-size:12px">{key}</span></td>'
            f"<td>{_esc(', '.join(values)) or _ANY_VALUE}</td>"
            f"<td>{required}</td>"
            f'<td><a class="btn-link" href="/t/{_esc(tenant)}/books/dimensions/{key}">'
            "Report</a></td>"
            f"<td>{remove}</td></tr>"
        )
    empty = (
        '<tr><td colspan="5" class="muted" style="text-align:center;padding:24px">'
        "No classes or locations yet. Most single-line businesses never need one."
        "</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Dimension</th><th>Values</th>'
        "<th>Rule</th><th></th><th></th></tr></thead><tbody>"
        + ("".join(rows) or empty) + "</tbody></table></div>"
    )

    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    back = f'<a class="btn-link" href="/t/{_esc(tenant)}/books">Back to books</a>'
    out = head + _card("Classes and locations", table, back)
    if can_post:
        out += _card(
            "Add one",
            f'<form method="post" action="/t/{_esc(tenant)}/books/dimensions">'
            '<div class="grid3">'
            '<div><label>Short key</label><input name="key" placeholder="class"></div>'
            '<div><label>What to call it</label>'
            '<input name="label" placeholder="Line of business"></div>'
            '<div><label>Require it?</label><select name="required">'
            '<option value="">No — optional</option>'
            '<option value="1">Yes — on every income and cost line</option>'
            "</select></div></div>"
            '<div><label>Allowed values, one per line</label>'
            '<textarea name="values" rows="4" placeholder="Dine-in&#10;Catering&#10;Events" '
            'style="width:100%"></textarea></div>'
            '<p class="note">Values are chosen from this list, never typed free-hand — '
            'otherwise "Denver" and "denver" become two lines of business that don\'t '
            "exist. Requiring one means an income or cost line can't be posted without "
            "it; the cash side never needs one.</p>"
            '<button class="btn" type="submit">Add</button></form>',
        )
    return out


def render_dimension_report(
    tenant: str, data: Mapping[str, object], *, frm: str = "", to: str = ""
) -> str:
    """A trial balance per value — the basis of P&L by class."""
    blocks = []
    for b in _seq(data.get("buckets")):
        if not isinstance(b, Mapping):
            continue
        rows = []
        for r in _seq(b.get("rows")):
            if not isinstance(r, Mapping):
                continue
            rows.append(
                f"<tr><td>{_esc(r.get('code'))}</td><td>{_esc(r.get('name'))}</td>"
                f"{_num(r.get('debit_minor'))}{_num(r.get('credit_minor'))}</tr>"
            )
        table = (
            '<div class="table-scroll"><table><thead><tr><th>Code</th><th>Account</th>'
            '<th class="num">Debit</th><th class="num">Credit</th></tr></thead><tbody>'
            + "".join(rows)
            + '<tr style="font-weight:700"><td colspan="2">Totals</td>'
            + _num(b.get("total_debit_minor")) + _num(b.get("total_credit_minor"))
            + "</tr></tbody></table></div>"
        )
        note = ""
        if b.get("unassigned"):
            note = (
                '<p class="note">Activity nobody has attributed yet. This is usually '
                "the most useful line on the report — it is exactly the work that "
                "hasn't been classified.</p>"
            )
        blocks.append(_card(str(b.get("value")), table + note))

    if not blocks:
        blocks.append(_card(
            "Nothing to report",
            '<p class="muted">Nothing has been posted against this dimension yet.</p>',
        ))

    key = _esc(data.get("dimension"))
    filter_form = _card(
        f"{_esc(data.get('label'))} — by value",
        f'<form method="get" action="/t/{_esc(tenant)}/books/dimensions/{key}">'
        '<div class="grid2">'
        f'<div><label>From</label><input name="from" value="{_esc(frm)}" '
        'placeholder="YYYY-MM-DD"></div>'
        f'<div><label>To</label><input name="to" value="{_esc(to)}" '
        'placeholder="YYYY-MM-DD"></div></div>'
        '<button class="btn" type="submit">Show</button></form>',
        f'<a class="btn-link" href="/t/{_esc(tenant)}/books/dimensions">All dimensions</a>',
    )
    return filter_form + "".join(blocks)


def render_dimensions_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card(
        "Unavailable",
        _banner("warn", "The ledger service isn't reachable right now.") + extra,
    )


__all__ = [
    "dimension_selects",
    "render_dimensions",
    "render_dimension_report",
    "render_dimensions_unavailable",
]
