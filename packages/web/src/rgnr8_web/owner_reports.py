"""Owner-facing renderers for the new ledger reports.

The TypeScript accounting core computes these and serializes them to small JSON
contracts (`aging/1`, `budget-vs-actual/1`, `retained-earnings/1` — emitted by
`@rgnr8/subledger` and `@rgnr8/financial-statements`). The web app holds the
contract per tenant and renders it here as an owner-facing page body, wrapped in
the app shell by the caller. Money is carried as integer minor-unit strings, so
formatting is exact — never a float.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping


def _esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _minor_int(minor: object) -> int | None:
    if isinstance(minor, bool) or not isinstance(minor, (int, str)):
        return None
    try:
        return int(minor)
    except ValueError:
        return None


def _money(minor: object, currency: str = "USD") -> str:
    """Format an integer minor-unit string/number as "$1,234.56" (2dp assumed)."""
    n = _minor_int(minor)
    if n is None:
        return _esc(minor)
    neg = n < 0
    whole, frac = divmod(abs(n), 100)
    sym = "$" if currency == "USD" else f"{_esc(currency)} "
    return f"{'-' if neg else ''}{sym}{whole:,}.{frac:02d}"


def _num_cell(minor: object, currency: str = "USD") -> str:
    n = _minor_int(minor) or 0
    cls = "num neg" if n < 0 else "num"
    return f'<td class="{cls}">{_money(minor, currency)}</td>'


def _seq(v: object) -> list[object]:
    """Coerce a contract field into a list (empty when absent/not a list)."""
    return list(v) if isinstance(v, (list, tuple)) else []


def _card(title: str, body: str) -> str:
    return (f'<section class="card"><h2 style="margin:0 0 12px;font:600 15px/1.2 var(--rg-sans)">'
            f'{_esc(title)}</h2>{body}</section>')


# --- AR/AP aging (aging/1) ---------------------------------------------------

def render_aging(data: Mapping[str, object]) -> str:
    kind = str(data.get("kind", "AR"))
    who = "Customer" if kind == "AR" else "Vendor"
    title = f"{'Receivables' if kind == 'AR' else 'Payables'} Aging"
    as_of = _esc(data.get("as_of", ""))
    labels = _seq(data.get("bucket_labels"))
    rows = _seq(data.get("rows"))
    header = "".join(f'<th class="num">{_esc(lbl)}</th>' for lbl in labels)
    body_rows = []
    for r in rows:
        if not isinstance(r, Mapping):
            continue
        buckets = _seq(r.get("buckets_minor"))
        cells = "".join(_num_cell(b) for b in buckets)
        body_rows.append(
            f'<tr><td>{_esc(r.get("party_id"))}</td>{cells}{_num_cell(r.get("total_minor"))}</tr>'
        )
    totals = _seq(data.get("column_totals_minor"))
    total_cells = "".join(_num_cell(t) for t in totals)
    empty = f'<tr><td colspan="{len(labels) + 2}" class="muted" style="text-align:center;padding:22px">Nothing outstanding.</td></tr>'
    table = (
        '<div class="table-scroll"><table><thead><tr>'
        f'<th>{who}</th>{header}<th class="num">Total</th></tr></thead><tbody>'
        + ("".join(body_rows) or empty)
        + f'<tr style="font-weight:700"><td>Total</td>{total_cells}{_num_cell(data.get("grand_total_minor"))}</tr>'
        + '</tbody></table></div>'
    )
    return _card(f"{title} — as of {as_of}", table)


# --- budget vs actual (budget-vs-actual/1) -----------------------------------

def render_budget(data: Mapping[str, object]) -> str:
    ccy = str(data.get("currency", "USD"))
    period = _esc(data.get("period", ""))
    rows = _seq(data.get("lines"))
    body_rows = []
    for l in rows:
        if not isinstance(l, Mapping):
            continue
        fav = bool(l.get("favorable"))
        pct = l.get("pct_of_budget")
        pct_cell = f'<td class="num">{pct:.0f}%</td>' if isinstance(pct, (int, float)) else '<td class="num muted">—</td>'
        flag = '<span style="color:var(--rg-sage)">▲ favorable</span>' if fav else '<span style="color:var(--rg-risk,#b4462f)">▼ over</span>'
        body_rows.append(
            f'<tr><td>{_esc(l.get("code"))}</td><td>{_esc(l.get("name"))}</td>'
            f'{_num_cell(l.get("budget_minor"), ccy)}{_num_cell(l.get("actual_minor"), ccy)}'
            f'{_num_cell(l.get("variance_minor"), ccy)}{pct_cell}<td>{flag}</td></tr>'
        )
    empty = '<tr><td colspan="7" class="muted" style="text-align:center;padding:22px">No budget set for this period.</td></tr>'
    table = (
        '<div class="table-scroll"><table><thead><tr><th>Code</th><th>Account</th>'
        '<th class="num">Budget</th><th class="num">Actual</th><th class="num">Variance</th>'
        '<th class="num">% of budget</th><th>Status</th></tr></thead><tbody>'
        + ("".join(body_rows) or empty)
        + f'<tr style="font-weight:700"><td colspan="2">Total</td>'
        + _num_cell(data.get("total_budget_minor"), ccy) + _num_cell(data.get("total_actual_minor"), ccy)
        + _num_cell(data.get("total_variance_minor"), ccy) + '<td></td><td></td></tr>'
        + '</tbody></table></div>'
    )
    return _card(f"Budget vs Actual — {period}", table)


# --- retained earnings (retained-earnings/1) ---------------------------------

def render_retained_earnings(data: Mapping[str, object]) -> str:
    ccy = str(data.get("currency", "USD"))
    rows = [
        ("Beginning retained earnings", data.get("beginning_minor")),
        ("Net income", data.get("net_income_minor")),
        ("Distributions", _neg(data.get("distributions_minor"))),
    ]
    body = "".join(
        f'<tr><td>{_esc(label)}</td>{_num_cell(val, ccy)}</tr>' for label, val in rows
    )
    body += f'<tr style="font-weight:700"><td>Ending retained earnings</td>{_num_cell(data.get("ending_minor"), ccy)}</tr>'
    return _card("Statement of Retained Earnings",
                 f'<div class="table-scroll"><table><tbody>{body}</tbody></table></div>')


def _neg(minor: object) -> str:
    """Present distributions as a negative (they reduce equity)."""
    if isinstance(minor, bool) or not isinstance(minor, (int, str)):
        return "0"
    try:
        return str(-int(minor))
    except ValueError:
        return "0"


# Contract → renderer registry (kind used by the web store + routes).
RENDERERS: dict[str, Callable[[Mapping[str, object]], str]] = {
    "aging_ar": render_aging,
    "aging_ap": render_aging,
    "budget": render_budget,
    "retained_earnings": render_retained_earnings,
}


def render_owner_report(kind: str, data: Mapping[str, object]) -> str | None:
    fn = RENDERERS.get(kind)
    return fn(data) if fn is not None else None
