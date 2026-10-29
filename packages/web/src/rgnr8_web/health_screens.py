"""Financial health — the "am I healthy?" dashboard in plain language.

Renders the ratio set (liquidity, leverage, profitability) with each group's
plain-language read and any covenant breaches. Read-only; the numbers come from
the same balances the statements use, so this can't disagree with the books.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from .books_screens import _banner, _card, _esc, _seq, money

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")


def month_name(month: int) -> str:
    return _MONTHS[month - 1] if 1 <= month <= 12 else _MONTHS[0]


def fiscal_year_start(as_of: date, start_month: int) -> date:
    """The first day of the fiscal year `as_of` falls in, for a fiscal year that
    starts in `start_month` (1 = January, the calendar year)."""
    month = start_month if 1 <= start_month <= 12 else 1
    year = as_of.year if as_of.month >= month else as_of.year - 1
    return date(year, month, 1)


def period_words(start: date, end: date) -> str:
    """"Jul 1 – Oct 29, 2026", or "Jul 1, 2025 – Mar 3, 2026" across a year end."""
    head = f"{_MONTHS[start.month - 1][:3]} {start.day}" + (
        "" if start.year == end.year else f", {start.year}")
    return f"{head} – {_MONTHS[end.month - 1][:3]} {end.day}, {end.year}"


def render_health_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card("Health unavailable", _banner("warn", "The ledger service isn't reachable right now.") + extra)


def _metric(label: str, value: object, suffix: str = "", why: str = "") -> str:
    # A ratio that can't be worked out says so, and why, instead of a bare "—"
    # that reads as a broken page or a zero.
    if value in (None, "", "—"):
        reason = f" · {_esc(why)}" if why else ""
        shown = f'<span class="muted">Not enough data{reason}</span>'
    else:
        shown = f"{_esc(value)}{suffix}"
    return (
        '<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--rg-line)">'
        f'<span class="muted">{_esc(label)}</span><span class="num">{shown}</span></div>'
    )


def _group(title: str, health: object, rows: str) -> str:
    # Was synthetic italic in a serif. The self-hosted Inter ships upright only,
    # so the voice change is carried by colour and size rather than a browser's
    # slanted approximation of a face that doesn't exist.
    # The ledger writes verdicts as clauses ("healthy — comfortably covers…");
    # here each stands as a sentence of its own.
    verdict = str(health or "")
    verdict = verdict[:1].upper() + verdict[1:]
    read = (f'<p style="margin:0 0 10px;font:400 13.5px/1.45 var(--rg-sans);'
            f'color:var(--rg-ink-2)">{_esc(verdict)}</p>') if verdict else ""
    return _card(title, read + rows)


def render_health(tenant: str, ratios: Mapping[str, object], *,
                  fiscal: Mapping[str, object] | None = None) -> str:
    """`fiscal`, when the app could read it, is the fiscal year to date from the
    books' own statements: {"start": date, "end": date, "start_month": int,
    "revenue_minor": int, "net_income_minor": int}."""
    liq = _as_map(ratios.get("liquidity"))
    lev = _as_map(ratios.get("leverage"))
    prof = _as_map(ratios.get("profitability"))
    inputs = _as_map(ratios.get("inputs"))
    covenants = _seq(ratios.get("covenants"))

    as_of = _esc(ratios.get("as_of"))
    period = ""
    if fiscal is not None:
        start, end = fiscal.get("start"), fiscal.get("end")
        if isinstance(start, date) and isinstance(end, date):
            period = period_words(start, end)
    if period and fiscal is not None:
        month = fiscal.get("start_month")
        header = (f'<p class="muted">As of {as_of}. Figures come straight from your books. '
                  "Revenue and net income below are for your fiscal year to date (it starts "
                  f"in {_esc(month_name(month if isinstance(month, int) else 1))}); the ratios "
                  f"use everything the books hold through {as_of}.</p>")
        earnings = (
            _metric(f"Revenue, fiscal year to date ({period})", money(fiscal.get("revenue_minor")))
            + _metric(f"Net income, fiscal year to date ({period})",
                      money(fiscal.get("net_income_minor"))))
    else:
        header = (f'<p class="muted">As of {as_of}. Figures come straight from your books; revenue '
                  f"and net income are everything they hold through {as_of}.</p>")
        earnings = (
            _metric(f"Revenue, through {ratios.get('as_of') or 'today'}",
                    money(inputs.get("revenue_minor")))
            + _metric(f"Net income, through {ratios.get('as_of') or 'today'}",
                      money(inputs.get("net_income_minor"))))

    liquidity = _group(
        "Liquidity — can you cover the short term?", liq.get("health"),
        _metric("Current ratio", liq.get("current_ratio"), "×", "no current liabilities")
        + _metric("Quick ratio", liq.get("quick_ratio"), "×", "no current liabilities")
        + _metric("Working capital", money(liq.get("working_capital_minor"))),
    )
    leverage = _group(
        "Leverage — how much do you owe, and can you service it?", lev.get("health"),
        _metric("Debt to equity", lev.get("debt_to_equity"), "×", "equity is zero or negative")
        + _metric("Debt to assets", lev.get("debt_to_assets"), "×", "no assets recorded")
        + _metric("Interest coverage", lev.get("interest_coverage"), "×", "no interest expense")
        + _metric("Debt-service coverage (DSCR)", lev.get("dscr"), "×", "no loan payments due"),
    )
    profitability = _group(
        "Profitability — is the work paying off?", prof.get("health"),
        _metric("Gross margin", prof.get("gross_margin_pct"), "%", "no revenue yet")
        + _metric("Net margin", prof.get("net_margin_pct"), "%", "no revenue yet")
        + _metric("Return on assets", prof.get("return_on_assets_pct"), "%", "no assets recorded")
        + _metric("Return on equity", prof.get("return_on_equity_pct"), "%",
                  "equity is zero or negative"),
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
        earnings
        + _metric("Total assets", money(inputs.get("total_assets_minor")))
        + _metric("Total liabilities", money(inputs.get("total_liabilities_minor")))
        + _metric("Equity", money(inputs.get("equity_minor")))
        + _metric("EBITDA", money(inputs.get("ebitda_minor")))
        + _metric("Annual debt service", money(inputs.get("annual_debt_service_minor"))),
    )

    return header + liquidity + leverage + profitability + covenant_html + basis


def _as_map(v: object) -> Mapping[str, object]:
    return v if isinstance(v, Mapping) else {}
