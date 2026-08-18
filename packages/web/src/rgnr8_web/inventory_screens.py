"""Inventory — what is on the shelf, what it cost, and whether the two agree.

The report ends with the number most stock systems never show: the difference
between what the item records say inventory is worth and what the inventory
account on the balance sheet says. Those two can disagree, so eventually they
will, and the only useful thing to do about it is put the gap on the screen.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _minor, _num, _seq, money
from .job_screens import _milli


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


def _options(values: list[tuple[str, str]], selected: str = "") -> str:
    return "".join(
        f'<option value="{_esc(v)}"{" selected" if v == selected else ""}>{_esc(label)}</option>'
        for v, label in values
    )


def render_inventory_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card("Inventory unavailable", '<div class="banner warn">The ledger service '
                 f"isn't reachable right now.</div>{extra}")


def render_inventory(
    tenant: str,
    valuation: Mapping[str, object],
    jobs: Mapping[str, object],
    cost_codes: Mapping[str, object],
    accounts: Mapping[str, object],
    *,
    can_edit: bool,
    message: str = "",
    error: str = "",
) -> str:
    items = [i for i in _seq(valuation.get("items")) if isinstance(i, Mapping)]
    totals = valuation.get("totals") if isinstance(valuation.get("totals"), Mapping) else {}
    assert isinstance(totals, Mapping)

    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    ties = str(totals.get("ties_out")) == "True"
    difference = _minor(totals.get("difference_minor")) or 0
    tie_out = (
        f'<p class="muted">{money(totals.get("items_value_minor"))} on the shelf, '
        f'{money(totals.get("ledger_balance_minor"))} on the balance sheet. '
        + ("They agree." if ties else "")
        + "</p>"
    )
    if not ties:
        tie_out += _banner(
            "warn",
            f"They differ by {money(abs(difference))}. Usually something was coded straight "
            "to the inventory account without going through an item — worth finding before "
            "it becomes a habit.",
        )
    out = head + _card("What stock is worth", tie_out)

    if items:
        cells = ""
        for i in items:
            low = ' style="color:var(--rg-warn)"' if str(i.get("below_reorder_point")) == "True" else ""
            cells += (
                f"<tr{low}><td>{_esc(i.get('sku'))}"
                f'<br><span class="muted" style="font-size:12px">{_esc(i.get("name"))}</span></td>'
                f'<td class="num">{_milli(i.get("quantity_milli"))}</td>'
                f'<td class="muted">{_esc(i.get("unit"))}</td>'
                f"{_num(i.get('unit_cost_minor'))}"
                f"{_num(i.get('value_minor'))}"
                f'<td class="num">{_milli(i.get("reorder_point_milli"))}</td></tr>'
            )
        out += _card("Items", (
            "<table><thead><tr><th>SKU</th><th class='num'>On hand</th><th>Unit</th>"
            "<th class='num'>Average cost</th><th class='num'>Value</th>"
            f"<th class='num'>Reorder at</th></tr></thead><tbody>{cells}</tbody></table>"
            '<p class="muted">Cost is a moving weighted average, worked out from the totals '
            "rather than accumulated, so a year of deliveries cannot compound a rounding "
            "error.</p>"
        ))
    else:
        out += _card("Items", (
            '<p class="muted">No stock items. Track something here when you buy it before '
            "you need it — the point is that material bought in bulk in March lands on the "
            "job that used it in June.</p>"
        ))

    reorder = [r for r in _seq(valuation.get("reorder")) if isinstance(r, Mapping)]
    if reorder:
        items_list = "".join(
            f"<li>{_esc(r.get('sku'))} — {_milli(r.get('quantity_milli'))} left, "
            f"reorder at {_milli(r.get('reorder_point_milli'))}</li>"
            for r in reorder
        )
        out += _card("Running low", f"<ul>{items_list}</ul>")

    if can_edit:
        out += _forms(tenant, items, jobs, cost_codes, accounts)
    return out


def _forms(
    tenant: str,
    items: list[Mapping[str, object]],
    jobs: Mapping[str, object],
    cost_codes: Mapping[str, object],
    accounts: Mapping[str, object],
) -> str:
    sku_options = _options(
        [(str(i.get("sku")), f"{i.get('sku')} — {i.get('name')}") for i in items]
    )
    job_rows = [j for j in _seq(jobs.get("jobs")) if isinstance(j, Mapping)]
    job_options = _options(
        [("", "—")] + [(str(j.get("id")), str(j.get("name"))) for j in job_rows]
    )
    codes = [c for c in _seq(cost_codes.get("cost_codes")) if isinstance(c, Mapping)]
    code_options = _options(
        [("", "—")] + [(str(c.get("code")), f"{c.get('code')} — {c.get('name')}") for c in codes]
    )
    asset = [
        a for a in _seq(accounts.get("accounts"))
        if isinstance(a, Mapping) and str(a.get("type")) == "ASSET"
    ]
    asset_options = _options(
        [(str(a.get("code")), f"{a.get('code')} — {a.get('name')}") for a in asset], "1300",
    )

    body = (
        f'<form method="post" action="/t/{_esc(tenant)}/inventory/items" class="grid">'
        '<label>SKU<input name="sku" required placeholder="PLY-34"></label>'
        '<label>Name<input name="name" required placeholder="3/4in plywood"></label>'
        '<label>Unit<input name="unit" placeholder="sheet"></label>'
        '<label>Reorder at<input name="reorder_point" placeholder="20"></label>'
        f'<label>Stock account<select name="inventory_account_code">{asset_options}</select></label>'
        '<label>Cost account<input name="cost_account_code" placeholder="5100"></label>'
        '<div style="grid-column:1/-1"><button type="submit">Save the item</button></div>'
        "</form>"
    )
    out = _card("Add an item", body)

    if items:
        out += _card("Bought stock", (
            f'<form method="post" action="/t/{_esc(tenant)}/inventory/receipts" class="grid">'
            f'<label>Item<select name="sku" required>{sku_options}</select></label>'
            '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
            '<label>How many<input name="quantity" required placeholder="100"></label>'
            '<label>Cost each<input name="unit_cost" required placeholder="48.00"></label>'
            '<label>Paid from<input name="paid_from_code" placeholder="1000"></label>'
            '<div style="grid-column:1/-1"><button type="submit">Record it</button>'
            '<span class="muted" style="margin-left:8px">For stock on a purchase order, '
            "receive it there instead — this is the trip to the supply house.</span>"
            "</div></form>"
        ))
        out += _card("Used on a job", (
            f'<form method="post" action="/t/{_esc(tenant)}/inventory/issues" class="grid">'
            f'<label>Item<select name="sku" required>{sku_options}</select></label>'
            '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
            '<label>How many<input name="quantity" required></label>'
            f'<label>Job<select name="job_id">{job_options}</select></label>'
            f'<label>Cost code<select name="cost_code">{code_options}</select></label>'
            '<div style="grid-column:1/-1"><button type="submit">Issue it</button>'
            '<span class="muted" style="margin-left:8px">Costed at the moving average, so '
            "what the job carries is what the stock actually cost.</span></div></form>"
        ))
        out += _card("Counted it", (
            f'<form method="post" action="/t/{_esc(tenant)}/inventory/counts" class="grid">'
            f'<label>Item<select name="sku" required>{sku_options}</select></label>'
            '<label>Date<input name="date" placeholder="YYYY-MM-DD" required></label>'
            '<label>Actually there<input name="counted" required></label>'
            '<div style="grid-column:1/-1"><button type="submit">Correct the record</button>'
            '<span class="muted" style="margin-left:8px">The difference posts to shrinkage. '
            "The books follow the shelf, not the other way round.</span></div></form>"
        ))
    return out


__all__ = ["render_inventory", "render_inventory_unavailable"]
