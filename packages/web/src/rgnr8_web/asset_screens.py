"""Fixed assets — the register, its depreciation, and disposal.

Shows what the business owns, what it cost, how it's being written down, and its
net book value; lets a bookkeeper (POST_JOURNAL) add an asset, run depreciation
to a date, record production usage, or dispose of it.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _banner, _card, _esc, _seq, money

_METHODS = [
    ("STRAIGHT_LINE", "Straight-line"),
    ("DECLINING_BALANCE", "Declining balance"),
    ("UNITS_OF_PRODUCTION", "Units of production"),
]


def render_assets_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card("Assets unavailable", _banner("warn", "The ledger service isn't reachable right now.") + extra)


def _options(values: list[tuple[str, str]], selected: str = "") -> str:
    return "".join(
        f'<option value="{_esc(v)}"{" selected" if v == selected else ""}>{_esc(label)}</option>'
        for v, label in values
    )


def _banners(done: str, error: str) -> str:
    out = ""
    if done:
        out += _banner("good", done)
    if error:
        out += _banner("warn", error)
    return out


def _tile(label: str, value: str) -> str:
    return (
        '<div class="card" style="margin:0;padding:14px">'
        f'<div class="muted" style="font-size:12px;text-transform:uppercase;letter-spacing:.04em">{_esc(label)}</div>'
        f'<div style="font:600 22px/1.2 var(--rg-serif);margin-top:4px">{_esc(value)}</div></div>'
    )


def render_assets(
    tenant: str, register: Mapping[str, object], *, can_write: bool, done: str = "", error: str = "",
) -> str:
    totals = register.get("totals", {})
    totals = totals if isinstance(totals, Mapping) else {}
    assets = _seq(register.get("assets"))

    tiles = (
        '<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px">'
        + _tile("Cost", money(totals.get("cost_minor")))
        + _tile("Accumulated depr.", money(totals.get("accumulated_minor")))
        + _tile("Net book value", money(totals.get("book_value_minor")))
        + _tile("Active assets", str(totals.get("active_count", 0)))
        + "</div>"
    )

    rows = []
    for a in assets:
        if not isinstance(a, Mapping):
            continue
        aid = _esc(a.get("id"))
        disposed = a.get("status") == "DISPOSED"
        badge = ' <span class="pill">disposed</span>' if disposed else ""
        rows.append(
            f'<tr><td><a href="/t/{_esc(tenant)}/assets/{aid}">{_esc(a.get("name"))}</a>{badge}'
            f'<div class="muted" style="font-size:12px">{_esc(a.get("category"))}</div></td>'
            f'<td>{_esc(a.get("method"))}</td>'
            f'<td class="num">{money(a.get("cost_minor"))}</td>'
            f'<td class="num">{money(a.get("accumulated_minor"))}</td>'
            f'<td class="num">{money(a.get("book_value_minor"))}</td></tr>'
        )
    table = (
        '<table class="tbl"><thead><tr><th>Asset</th><th>Method</th><th class="num">Cost</th>'
        '<th class="num">Accum.</th><th class="num">Book value</th></tr></thead><tbody>'
        + ("".join(rows) or '<tr><td colspan="5" class="muted">No assets yet.</td></tr>')
        + "</tbody></table>"
    )

    sections = [_banners(done, error), _card("Fixed asset register", tiles + table)]
    if can_write:
        sections.append(_card("Add an asset", _add_asset_form(tenant)))
    else:
        sections.append('<p class="muted">Adding assets needs the Post journal permission.</p>')
    return "".join(sections)


def _add_asset_form(tenant: str) -> str:
    return (
        f'<form method="post" action="/t/{_esc(tenant)}/assets" class="grid">'
        '<label>Asset id (short handle)<input name="id" required></label>'
        '<label>Name<input name="name" required></label>'
        '<label>Category<input name="category" placeholder="Vehicles"></label>'
        '<label>In-service date<input name="in_service_date" type="date" required></label>'
        '<label>Cost ($)<input name="cost" type="text" placeholder="12000.00" required></label>'
        '<label>Salvage value ($)<input name="salvage" type="text" placeholder="0.00"></label>'
        f'<label>Method<select name="method">{_options(_METHODS)}</select></label>'
        '<label>Useful life (months)<input name="useful_life_months" type="number" min="0" placeholder="48"></label>'
        '<label>Declining factor (e.g. 2 for double)<input name="declining_factor" placeholder="2"></label>'
        '<label>Total units (units-of-production)<input name="total_units" placeholder=""></label>'
        '<label>Pay from (account code)<input name="paid_from_code" placeholder="1000"></label>'
        '<div style="grid-column:1/-1"><button type="submit">Add asset</button></div>'
        "</form>"
    )


def render_asset_detail(
    tenant: str, data: Mapping[str, object], *, can_write: bool, done: str = "", error: str = "",
) -> str:
    asset = data.get("asset", {})
    asset = asset if isinstance(asset, Mapping) else {}
    schedule = _seq(data.get("schedule"))
    aid = _esc(asset.get("id"))
    disposed = asset.get("status") == "DISPOSED"
    units = asset.get("method") == "UNITS_OF_PRODUCTION"

    summary = (
        '<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px">'
        + _tile("Cost", money(asset.get("cost_minor")))
        + _tile("Accumulated", money(asset.get("accumulated_minor")))
        + _tile("Book value", money(asset.get("book_value_minor")))
        + _tile("Method", str(asset.get("method")))
        + "</div>"
    )

    srows = []
    for r in schedule[:600]:
        if not isinstance(r, Mapping):
            continue
        srows.append(
            f'<tr><td>{_esc(r.get("period"))}</td><td>{_esc(r.get("through_date"))}</td>'
            f'<td class="num">{money(r.get("expense_minor"))}</td>'
            f'<td class="num">{money(r.get("accumulated_minor"))}</td>'
            f'<td class="num">{money(r.get("book_value_minor"))}</td></tr>'
        )
    schedule_tbl = (
        '<table class="tbl"><thead><tr><th>#</th><th>Through</th><th class="num">Depreciation</th>'
        '<th class="num">Accumulated</th><th class="num">Book value</th></tr></thead><tbody>'
        + ("".join(srows) or '<tr><td colspan="5" class="muted">Usage-driven — no calendar schedule.</td></tr>')
        + "</tbody></table>"
    )

    actions = ""
    if can_write and not disposed:
        if units:
            actions += (
                f'<form method="post" action="/t/{_esc(tenant)}/assets/{aid}/usage" class="grid">'
                '<label>Date<input name="date" type="date" required></label>'
                '<label>Units used<input name="units" type="text" required></label>'
                '<div style="grid-column:1/-1"><button type="submit">Record usage</button></div></form>'
            )
        else:
            actions += (
                f'<form method="post" action="/t/{_esc(tenant)}/assets/{aid}/depreciate" class="grid">'
                '<label>Depreciate through<input name="through_date" type="date" required></label>'
                '<div style="grid-column:1/-1"><button type="submit">Run depreciation</button></div></form>'
            )
        actions += (
            f'<form method="post" action="/t/{_esc(tenant)}/assets/{aid}/dispose" class="grid" '
            'onsubmit="return confirm(\'Dispose of this asset?\')" style="margin-top:12px">'
            '<label>Disposal date<input name="date" type="date" required></label>'
            '<label>Proceeds ($)<input name="proceeds" type="text" placeholder="0.00"></label>'
            '<label>Deposit to (account code)<input name="proceeds_to_code" placeholder="1000"></label>'
            '<div style="grid-column:1/-1"><button type="submit" class="danger">Dispose</button></div></form>'
        )

    sections = [
        _banners(done, error),
        f'<p><a href="/t/{_esc(tenant)}/assets">← All assets</a></p>',
        _card(_esc(asset.get("name")), summary),
        _card("Depreciation schedule", schedule_tbl),
    ]
    if actions:
        sections.append(_card("Actions", actions))
    return "".join(sections)
