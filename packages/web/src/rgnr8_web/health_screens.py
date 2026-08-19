"""Financial health — the "am I healthy?" dashboard in plain language.

Renders the ratio set (liquidity, leverage, profitability) with each group's
plain-language read and any covenant breaches. Read-only; the numbers come from
the same balances the statements use, so this can't disagree with the books.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _banner, _card, _esc, _seq, money


def render_health_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card("Health unavailable", _banner("warn", "The ledger service isn't reachable right now.") + extra)


def _metric(label: str, value: object, suffix: str = "") -> str:
    shown = "—" if value in (None, "") else f"{_esc(value)}{suffix}"
    return (
        '<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--rg-line)">'
        f'<span class="muted">{_esc(label)}</span><span class="num">{shown}</span></div>'
    )


def _group(title: str, health: object, rows: str) -> str:
    read = f'<p style="margin:0 0 10px;font:italic 14px/1.4 var(--rg-serif)">{_esc(health)}</p>' if health else ""
    return _card(title, read + rows)


def render_health(tenant: str, ratios: Mapping[str, object]) -> str:
    liq = _as_map(ratios.get("liquidity"))
    lev = _as_map(ratios.get("leverage"))
    prof = _as_map(ratios.get("profitability"))
    inputs = _as_map(ratios.get("inputs"))
    covenants = _seq(ratios.get("covenants"))

    as_of = _esc(ratios.get("as_of"))
    header = f'<p class="muted">As of {as_of}. Figures come straight from your books.</p>'

    liquidity = _group(
        "Liquidity — can you cover the short term?", liq.get("health"),
        _metric("Current ratio", liq.get("current_ratio"), "×")
        + _metric("Quick ratio", liq.get("quick_ratio"), "×")
        + _metric("Working capital", money(liq.get("working_capital_minor"))),
    )
    leverage = _group(
        "Leverage — how much do you owe, and can you service it?", lev.get("health"),
        _metric("Debt to equity", lev.get("debt_to_equity"), "×")
        + _metric("Debt to assets", lev.get("debt_to_assets"), "×")
        + _metric("Interest coverage", lev.get("interest_coverage"), "×")
        + _metric("Debt-service coverage (DSCR)", lev.get("dscr"), "×"),
    )
    profitability = _group(
        "Profitability — is the work paying off?", prof.get("health"),
        _metric("Gross margin", prof.get("gross_margin_pct"), "%")
        + _metric("Net margin", prof.get("net_margin_pct"), "%")
        + _metric("Return on assets", prof.get("return_on_assets_pct"), "%")
        + _metric("Return on equity", prof.get("return_on_equity_pct"), "%"),
    )

    covenant_html = ""
    breaches = [c for c in covenants if isinstance(c, Mapping) and c.get("breached")]
    if breaches:
        items = "".join(
            f'<li>Loan <strong>{_esc(c.get("loan_id"))}</strong>: DSCR below its '
            f'{int(str(c.get("min_dscr_micro")))/1_000_000:.2f}× covenant</li>'
            for c in breaches
        )
        covenant_html = _card("Covenant alerts", _banner("warn", "One or more loan covenants are at risk:") + f"<ul>{items}</ul>")

    basis = _card(
        "The numbers behind it",
        _metric("Revenue", money(inputs.get("revenue_minor")))
        + _metric("Net income", money(inputs.get("net_income_minor")))
        + _metric("Total assets", money(inputs.get("total_assets_minor")))
        + _metric("Total liabilities", money(inputs.get("total_liabilities_minor")))
        + _metric("Equity", money(inputs.get("equity_minor")))
        + _metric("EBITDA", money(inputs.get("ebitda_minor")))
        + _metric("Annual debt service", money(inputs.get("annual_debt_service_minor"))),
    )

    return header + liquidity + leverage + profitability + covenant_html + basis


def _as_map(v: object) -> Mapping[str, object]:
    return v if isinstance(v, Mapping) else {}
