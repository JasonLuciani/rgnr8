"""Split a commingled account into two sets of books — the guided screen (A-2).

Business and personal spending (or several clients) running through one account is
the mess bookkeepers untangle by hand. This screen takes one statement plus a few
routing rules and produces one *balanced* set of books per destination, with a
line-by-line audit of where every transaction landed and why. Nothing is posted —
it's a read-only proposal the owner reviews before committing.

Rendering only: the app computes the split (shared with the JSON/MCP path) and
hands the result here. Provisional surface — badged accordingly by scope.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _banner, _card, _esc

# A worked example prefilled into the form so the flow is self-explanatory: two
# business rules (payroll out, card deposits in), everything else personal.
_EXAMPLE = """{
  "default_book": "personal",
  "rules": [
    {"book": "business", "name": "payroll", "category": "Wages", "counterparty": "GUSTO"},
    {"book": "business", "name": "card sales", "category": "Sales", "description_regex": "STRIPE"}
  ],
  "transactions": [
    {"id": "t1", "description": "STRIPE PAYOUT", "counterparty": "STRIPE", "amount_minor": "250000"},
    {"id": "t2", "description": "GUSTO PAYROLL", "counterparty": "GUSTO", "amount_minor": "-120000"},
    {"id": "t3", "description": "WHOLE FOODS", "counterparty": "WHOLEFOODS", "amount_minor": "-8000"},
    {"id": "t4", "description": "MORTGAGE", "counterparty": "BIGBANK", "amount_minor": "-300000"}
  ]
}"""


def _money(minor: object) -> str:
    try:
        m = int(str(minor))
    except (TypeError, ValueError):
        return str(minor)
    neg = m < 0
    whole, frac = divmod(abs(m), 100)
    return f"{'-' if neg else ''}${whole:,}.{frac:02d}"


def render_split(
    tenant: str,
    *,
    spec: str = "",
    result: Mapping[str, object] | None = None,
    error: str = "",
) -> str:
    base = f"/t/{_esc(tenant)}/books/split"
    intro = (
        '<p class="muted">Paste one statement and a few routing rules; RGNR8 sorts every '
        "transaction into a set of books and proves each set balances. First matching rule "
        "wins. Nothing is posted — this is a proposal you review first.</p>"
    )
    form = (
        f'<form method="post" action="{base}" class="grid">'
        '<label style="grid-column:1/-1">Statement + rules (JSON)'
        f'<textarea name="spec" rows="16" style="font-family:var(--rg-mono,monospace);font-size:13px">'
        f'{_esc(spec or _EXAMPLE)}</textarea></label>'
        '<div style="grid-column:1/-1"><button type="submit">Split into books</button></div>'
        "</form>"
    )
    sections = [_card("Split a commingled account", intro + form)]
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
        "ok" if all_balanced else "warn",
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
    rows = "".join(_audit_row(tenant, r) for r in audit if isinstance(r, Mapping))
    table = (
        '<table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:8px">'
        '<thead><tr class="muted" style="text-align:left">'
        "<th>Transaction</th><th>Amount</th><th>Book</th><th>Routed by</th><th>Category</th>"
        "</tr></thead><tbody>" + rows + "</tbody></table>"
    )
    return _card("Proposed split", banner + summary + table)


def _audit_row(tenant: str, r: Mapping[str, object]) -> str:
    return (
        "<tr>"
        f'<td style="padding:4px 8px 4px 0">{_esc(r.get("description")) or _esc(r.get("txn_id"))}</td>'
        f'<td style="padding:4px 8px 4px 0;font-variant-numeric:tabular-nums">{_money(r.get("amount_minor"))}</td>'
        f'<td style="padding:4px 8px 4px 0"><strong>{_esc(r.get("book"))}</strong></td>'
        f'<td style="padding:4px 8px 4px 0" class="muted">{_esc(r.get("routed_by"))}</td>'
        f'<td style="padding:4px 0">{_esc(r.get("category"))}</td>'
        "</tr>"
    )


__all__ = ["render_split"]
