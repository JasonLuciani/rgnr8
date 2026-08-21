"""Split a commingled account into two sets of books — the guided screen (A-2).

Business and personal spending (or several clients) running through one account is
the mess bookkeepers untangle by hand. This screen reads the account's *imported*
statement straight from the bank feed and lets the owner author a few plain-English
routing rules — no JSON, no re-typing transactions — then produces one *balanced*
set of books per destination, with a line-by-line audit of where every
transaction landed and why. Nothing is posted; it's a proposal the owner reviews.

Rendering only: the app loads the statement and computes the split (shared with
the JSON/MCP path) and hands the result here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .books_screens import _banner, _card, _esc

# How many rule rows the builder shows. Six covers a household/business split or a
# handful of clients; blank rows are ignored.
RULE_ROWS = 6

# What a rule can match on — kept to the two fields an owner actually reasons about.
_MATCH_FIELDS = (("counterparty", "Counterparty is"), ("description", "Description contains"))
_DIRECTIONS = (("", "Any amount"), ("in", "Money in"), ("out", "Money out"))


def _opts(choices: Sequence[tuple[str, str]], selected: str) -> str:
    return "".join(
        f'<option value="{_esc(v)}"{" selected" if v == selected else ""}>{_esc(lbl)}</option>'
        for v, lbl in choices
    )


def _money(minor: object) -> str:
    try:
        m = int(str(minor))
    except (TypeError, ValueError):
        return str(minor)
    neg = m < 0
    whole, frac = divmod(abs(m), 100)
    return f"{'-' if neg else ''}${whole:,}.{frac:02d}"


def _rule_row(i: int, rule: Mapping[str, str] | None) -> str:
    r = rule or {}
    book = _esc(r.get("book", ""))
    field = str(r.get("field", "counterparty"))
    value = _esc(r.get("value", ""))
    direction = str(r.get("dir", ""))
    category = _esc(r.get("cat", ""))
    return (
        '<tr>'
        f'<td><input name="rule_book_{i}" value="{book}" placeholder="Business" '
        'style="min-width:110px"></td>'
        f'<td><select name="rule_field_{i}">{_opts(_MATCH_FIELDS, field)}</select></td>'
        f'<td><input name="rule_value_{i}" value="{value}" placeholder="GUSTO" '
        'style="min-width:120px"></td>'
        f'<td><select name="rule_dir_{i}">{_opts(_DIRECTIONS, direction)}</select></td>'
        f'<td><input name="rule_cat_{i}" value="{category}" placeholder="Wages" '
        'style="min-width:110px"></td>'
        '</tr>'
    )


def render_split(
    tenant: str,
    *,
    source_count: int = 0,
    source_preview: Sequence[Mapping[str, object]] = (),
    default_book: str = "Personal",
    rules: Sequence[Mapping[str, str]] | None = None,
    result: Mapping[str, object] | None = None,
    error: str = "",
) -> str:
    base = f"/t/{_esc(tenant)}/books/split"
    sections: list[str] = []

    # --- source: the imported statement, read from the bank feed ---------------
    if source_count == 0:
        sections.append(_card(
            "Split a commingled account",
            _banner("warn", "No imported transactions yet.")
            + '<p class="muted">This flow splits the transactions already imported into your '
            "bank feed. Import a statement (Transactions → import, or a connected feed) and "
            "come back — every line will be here to route.</p>",
        ))
        return "".join(sections)

    preview_rows = "".join(
        f'<tr><td>{_esc(t.get("date"))}</td>'
        f'<td>{_esc(t.get("description"))}</td>'
        f'<td class="muted">{_esc(t.get("counterparty"))}</td>'
        f'<td class="num">{_money(t.get("amount_minor"))}</td></tr>'
        for t in source_preview
    )
    more = source_count - len(source_preview)
    preview = (
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<thead><tr class="muted" style="text-align:left"><th>Date</th><th>Description</th>'
        '<th>Counterparty</th><th class="num">Amount</th></tr></thead>'
        f'<tbody>{preview_rows}</tbody></table>'
        + (f'<p class="muted" style="font-size:12px;margin-top:6px">…and {more} more.</p>'
           if more > 0 else "")
    )
    intro = (
        f'<p class="muted">Reading <strong>{source_count}</strong> imported '
        f'transaction{"s" if source_count != 1 else ""} from your bank feed. Route them into '
        "sets of books with the rules below — first matching rule wins; anything unmatched "
        "goes to the default book. Nothing is posted.</p>"
    )
    sections.append(_card("Statement to split", intro + preview))

    # --- the rule builder ------------------------------------------------------
    row_html = "".join(_rule_row(i, (rules[i] if rules and i < len(rules) else None))
                        for i in range(RULE_ROWS))
    builder = (
        f'<form method="post" action="{base}" class="grid">'
        '<label style="grid-column:1/-1">Default book (for anything no rule matches)'
        f'<input name="default_book" value="{_esc(default_book)}" placeholder="Personal" '
        'style="max-width:240px"></label>'
        '<div style="grid-column:1/-1;overflow-x:auto">'
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<thead><tr class="muted" style="text-align:left">'
        '<th>Book</th><th>Match</th><th>Value</th><th>Direction</th><th>Category</th>'
        '</tr></thead><tbody>' + row_html + '</tbody></table></div>'
        '<div style="grid-column:1/-1"><button type="submit">Preview split</button>'
        '<span class="muted" style="font-size:12px;margin-left:10px">'
        'Leave a row blank to ignore it.</span></div>'
        "</form>"
    )
    sections.append(_card("Routing rules", builder))

    if error:
        sections.append(_banner("warn", error))
    if result is not None:
        sections.append(_render_result(tenant, result))
    return "".join(sections)


def _render_result(tenant: str, result: Mapping[str, object]) -> str:
    books = result.get("books")
    books = books if isinstance(books, list) else []
    all_balanced = bool(result.get("all_balanced"))
    banner = _banner(
        "good" if all_balanced else "warn",
        "Every set of books balances." if all_balanced
        else "One or more books do not balance — check the rules.",
    )

    cards = []
    for b in books:
        if not isinstance(b, Mapping):
            continue
        bal = bool(b.get("balanced"))
        badge = (
            f'<span style="font-size:12px;padding:2px 8px;border-radius:10px;'
            f'background:{"var(--rg-ok-bg,#e6f7ec)" if bal else "var(--rg-warn-bg,#fdecec)"}">'
            f'{"balanced" if bal else "out of balance"}</span>'
        )
        cards.append(
            '<div style="border:1px solid var(--rg-line);border-radius:8px;padding:12px;min-width:200px">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;gap:8px">'
            f'<strong>{_esc(b.get("book"))}</strong>{badge}</div>'
            f'<div class="muted" style="font-size:13px;margin-top:6px">'
            f'{_esc(b.get("entry_count"))} entries · net cash {_money(b.get("net_cash_minor"))}<br>'
            f'debits {_money(b.get("debits_minor"))} = credits {_money(b.get("credits_minor"))}</div>'
            "</div>"
        )
    summary = f'<div style="display:flex;gap:12px;flex-wrap:wrap;margin:10px 0">{"".join(cards)}</div>'

    audit = result.get("audit")
    audit = audit if isinstance(audit, list) else []
    rows = "".join(_audit_row(r) for r in audit if isinstance(r, Mapping))
    table = (
        '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;'
        'font-size:13px;margin-top:8px">'
        '<thead><tr class="muted" style="text-align:left">'
        "<th>Transaction</th><th>Amount</th><th>Book</th><th>Routed by</th><th>Category</th>"
        "</tr></thead><tbody>" + rows + "</tbody></table></div>"
    )
    return _card("Proposed split", banner + summary + table)


def _audit_row(r: Mapping[str, object]) -> str:
    return (
        "<tr>"
        f'<td style="padding:4px 8px 4px 0">{_esc(r.get("description")) or _esc(r.get("txn_id"))}</td>'
        f'<td style="padding:4px 8px 4px 0;font-variant-numeric:tabular-nums">{_money(r.get("amount_minor"))}</td>'
        f'<td style="padding:4px 8px 4px 0"><strong>{_esc(r.get("book"))}</strong></td>'
        f'<td style="padding:4px 8px 4px 0" class="muted">{_esc(r.get("routed_by"))}</td>'
        f'<td style="padding:4px 0">{_esc(r.get("category"))}</td>'
        "</tr>"
    )


__all__ = ["render_split", "RULE_ROWS"]
