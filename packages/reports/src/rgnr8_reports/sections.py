"""The section registry: one builder per section ``kind``.

Each builder is ``(context, params) -> ReportSection`` and is bound to a ``kind``
in :data:`REGISTRY`. The engine looks a section's kind up here and calls its
builder against the shared :class:`~rgnr8_reports.context.DataContext`.

Two families of kinds:

* **Presentation-only** — ``kpi_row``, ``table``, ``narrative``, ``chart`` render
  content carried in their own ``params`` (used by custom reports).
* **Source-bound composites** — ``cash_outlook``, ``cash_drivers``, ``ar_aging``,
  ``ar_chase``, ``income_statement``, ``balance_sheet``, ``cash_flow``,
  ``budget_vs_actual``, ``runway``, ``transaction_summary``, ``usage_summary``,
  ``scenario_compare``, ``trust_check`` — each composes one of the platform's
  engines (forecast, briefing, AR, billing, recon, scenario, financial
  statements). When the data a composite needs is absent from the context, it
  degrades to a single "not available" :class:`Narrative` rather than raising.

Every figure is formatted through the one shared ``format_money`` so a report
reads exactly like the rest of the app.
"""

from __future__ import annotations

from typing import Callable, Mapping, Sequence

from rgnr8_forecast import Money
from rgnr8_forecast.brand import format_money
from rgnr8_briefing import build_briefing
from rgnr8_ar import AgingBucket, ar_report, chase_list
from rgnr8_billing import build_invoice, plan_for
from rgnr8_recon_monitor import check
from rgnr8_scenario import run_scenario

from .context import DataContext
from .model import Block, Chart, KpiRow, Narrative, ReportSection, Table

SectionBuilder = Callable[[DataContext, Mapping[str, object]], ReportSection]


# --- param coercion (typed, mypy --strict friendly) --------------------------
def _p_str(params: Mapping[str, object], key: str, default: str = "") -> str:
    v = params.get(key, default)
    return v if isinstance(v, str) else default


def _p_int(params: Mapping[str, object], key: str, default: int) -> int:
    v = params.get(key, default)
    return v if isinstance(v, int) and not isinstance(v, bool) else default


def _as_seq(o: object) -> Sequence[object]:
    return o if isinstance(o, (list, tuple)) else ()


def _as_mapping(o: object) -> Mapping[str, object]:
    return o if isinstance(o, Mapping) else {}


def _as_int(o: object) -> int:
    return o if isinstance(o, int) and not isinstance(o, bool) else 0


def _as_str(o: object) -> str:
    return o if isinstance(o, str) else ""


def _as_bool(o: object) -> bool:
    return bool(o)


# --- shared helpers ----------------------------------------------------------
def _not_available(title: str, reason: str) -> ReportSection:
    """A section that had no data to render — the graceful-degradation block."""
    return ReportSection(title=title, blocks=(Narrative(f"Not available — {reason}."),))


def _money_kpi(label: str, m: Money) -> tuple[str, str, bool]:
    """A KPI item for a money value, risk-flagged when negative."""
    return (label, format_money(m), m.is_negative)


def _fs_lines(o: object) -> list[tuple[str, int]]:
    """Parse a financial-statements/1 line list ``[{label, amount_minor}]``."""
    out: list[tuple[str, int]] = []
    for item in _as_seq(o):
        m = _as_mapping(item)
        out.append((_as_str(m.get("label")), _as_int(m.get("amount_minor"))))
    return out


def _sparkline(values: Sequence[int], *, width: int = 320, height: int = 48) -> str:
    """A self-contained inline-SVG sparkline of ``values`` (no external URLs)."""
    from rgnr8_forecast.brand import SAGE, LINE

    n = len(values)
    if n == 0:
        return (
            f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            'role="img" aria-label="no data"></svg>'
        )
    lo = min(values)
    hi = max(values)
    span = hi - lo or 1
    pad = 4
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad
    pts: list[str] = []
    for i, v in enumerate(values):
        x = pad + (inner_w * i / (n - 1) if n > 1 else 0.0)
        y = pad + inner_h * (1 - (v - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    zero_line = ""
    if lo < 0 < hi:
        zy = pad + inner_h * (1 - (0 - lo) / span)
        zero_line = (
            f'<line x1="{pad}" y1="{zy:.1f}" x2="{width - pad}" y2="{zy:.1f}" '
            f'stroke="{LINE}" stroke-width="1"/>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        'role="img" aria-label="trend">'
        f"{zero_line}"
        f'<polyline fill="none" stroke="{SAGE}" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round" points="{" ".join(pts)}"/>'
        "</svg>"
    )


# --- presentation-only builders ----------------------------------------------
def _b_narrative(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Note")
    return ReportSection(title=title, blocks=(Narrative(_p_str(params, "text")),))


def _b_kpi_row(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Highlights")
    items: list[tuple[str, str, bool]] = []
    for raw in _as_seq(params.get("items")):
        row = _as_seq(raw)
        if len(row) >= 2:
            items.append((_as_str(row[0]), _as_str(row[1]), _as_bool(row[2]) if len(row) > 2 else False))
    return ReportSection(title=title, blocks=(KpiRow(tuple(items)),))


def _b_table(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Table")
    columns = tuple(_as_str(c) for c in _as_seq(params.get("columns")))
    rows = tuple(
        tuple(_as_str(c) for c in _as_seq(r)) for r in _as_seq(params.get("rows"))
    )
    return ReportSection(title=title, blocks=(Table(columns, rows),))


def _b_chart(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Chart")
    raw_svg = params.get("svg")
    if isinstance(raw_svg, str):
        return ReportSection(title=title, blocks=(Chart(raw_svg),))
    values = [_as_int(v) for v in _as_seq(params.get("values"))]
    return ReportSection(title=title, blocks=(Chart(_sparkline(values)),))


# --- forecast / cash ---------------------------------------------------------
def _b_cash_outlook(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Cash Outlook")
    if ctx.forecast is None:
        return _not_available(title, "no forecast available")
    fc = ctx.forecast
    p = fc.projection
    briefing = build_briefing(fc)
    cushion = p.trough.balance - p.effective_floor

    blocks: list[Block] = []
    action = f" Recommended action: {briefing.primary_action}" if briefing.primary_action else ""
    blocks.append(
        Narrative(f"{briefing.status.value} — {briefing.status_reason} {briefing.headline}{action}".strip())
    )
    blocks.append(
        KpiRow(
            (
                _money_kpi("Cash today", p.opening_available),
                _money_kpi("Minimum floor", p.effective_floor),
                _money_kpi("Projected low point", p.trough.balance),
                _money_kpi("Cushion at low point", cushion),
            )
        )
    )
    rows = tuple(
        (
            f"W{w.index}",
            w.start.isoformat(),
            format_money(w.closing),
            g.status.value,
        )
        for w, g in zip(p.weeks, briefing.week_glance)
    )
    blocks.append(Table(("Week", "Starting", "Closing cash", "Status"), rows))
    blocks.append(Chart(_sparkline([w.closing.minor_units for w in p.weeks])))
    return ReportSection(title=title, blocks=tuple(blocks))


def _b_cash_drivers(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "What's Draining Cash")
    if ctx.forecast is None:
        return _not_available(title, "no forecast available")
    briefing = build_briefing(ctx.forecast)
    if not briefing.drivers:
        return _not_available(title, "no outflow drivers")
    rows = tuple(
        (
            d.label,
            format_money(d.amount),
            f"{d.share_bps / 100:.1f}%",
        )
        for d in briefing.drivers
    )
    return ReportSection(
        title=title, blocks=(Table(("Category", "Amount out", "Share of outflows"), rows),)
    )


def _b_runway(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Runway")
    if ctx.forecast is None:
        return _not_available(title, "no forecast available")
    p = ctx.forecast.projection
    weeks = len(p.weeks)
    net_burn = p.total_outflows - p.total_inflows  # positive = net cash burn
    weekly_net = net_burn.scale_by(1, weeks) if weeks else Money.zero(p.currency)
    monthly_burn = weekly_net.scale_by(30, 7)
    opening = p.opening_available

    if monthly_burn.minor_units <= 0:
        runway_text = "Indefinite — cash-flow positive over the horizon"
        months_display = "∞"
    else:
        months = opening.minor_units / monthly_burn.minor_units
        months_display = f"{months:.1f} months"
        runway_text = (
            f"At the current net burn of {format_money(monthly_burn)}/month, "
            f"{format_money(opening)} of cash lasts about {months:.1f} months."
        )

    blocks: list[Block] = [
        KpiRow(
            (
                _money_kpi("Cash today", opening),
                _money_kpi("Net burn / month", monthly_burn),
                ("Runway", months_display, monthly_burn.minor_units > 0 and opening.minor_units < monthly_burn.minor_units * 3),
                _money_kpi("Projected trough", p.trough.balance),
            )
        ),
        Narrative(runway_text),
    ]
    return ReportSection(title=title, blocks=tuple(blocks))


# --- accounts receivable -----------------------------------------------------
def _b_ar_aging(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "AR Aging")
    if not ctx.invoices:
        return _not_available(title, "no open invoices")
    rep = ar_report(
        ctx.invoices, ctx.as_of, avg_daily_sales=ctx.avg_daily_sales, currency=ctx.currency
    )
    labels = {
        AgingBucket.CURRENT: "Current",
        AgingBucket.D1_30: "1–30 days",
        AgingBucket.D31_60: "31–60 days",
        AgingBucket.D60_PLUS: "60+ days",
    }
    rows = tuple(
        (labels[b], format_money(rep.aging.amount(b))) for b in labels
    )
    kpis: list[tuple[str, str, bool]] = [
        _money_kpi("Total AR", rep.total_ar),
        _money_kpi("Overdue", rep.overdue_total),
    ]
    if rep.dso is not None:
        kpis.append(("DSO", f"{rep.dso:.1f} days", False))
    return ReportSection(
        title=title,
        blocks=(KpiRow(tuple(kpis)), Table(("Bucket", "Open amount"), rows)),
    )


def _b_ar_chase(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Collections Chase List")
    if not ctx.invoices:
        return _not_available(title, "no open invoices")
    limit = _p_int(params, "limit", 10)
    items = chase_list(ctx.invoices, ctx.as_of, histories=ctx.histories)
    if not items:
        return _not_available(title, "nothing overdue")
    rows = tuple(
        (
            it.invoice.id,
            it.invoice.customer_id,
            str(it.days_overdue),
            format_money(it.invoice.open_amount),
            str(it.risk_score),
        )
        for it in items[:limit]
    )
    return ReportSection(
        title=title,
        blocks=(
            Table(
                ("Invoice", "Customer", "Days overdue", "Open amount", "Risk weight"),
                rows,
            ),
        ),
    )


# --- financial statements (financial-statements/1 contract) ------------------
def _fs_currency(fs: Mapping[str, object], ctx: DataContext) -> str:
    ccy = fs.get("currency")
    return ccy if isinstance(ccy, str) and ccy else ctx.currency


def _b_income_statement(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Income Statement")
    if ctx.financial_statements is None:
        return _not_available(title, "no financial statements provided")
    fs = ctx.financial_statements
    inc = _as_mapping(fs.get("income_statement"))
    if not inc:
        return _not_available(title, "no income statement in the contract")
    ccy = _fs_currency(fs, ctx)
    revenue = _fs_lines(inc.get("revenue"))
    expenses = _fs_lines(inc.get("expenses"))
    total_rev = Money(_as_int(inc.get("total_revenue")), ccy)
    total_exp = Money(_as_int(inc.get("total_expenses")), ccy)
    net = Money(_as_int(inc.get("net_income")), ccy)

    rows: list[tuple[str, str]] = []
    for label, amt in revenue:
        rows.append((label, format_money(Money(amt, ccy))))
    rows.append(("Total revenue", format_money(total_rev)))
    for label, amt in expenses:
        rows.append((label, format_money(Money(amt, ccy))))
    rows.append(("Total expenses", format_money(total_exp)))
    rows.append(("Net income", format_money(net)))

    return ReportSection(
        title=title,
        blocks=(
            KpiRow(
                (
                    _money_kpi("Total revenue", total_rev),
                    _money_kpi("Total expenses", total_exp),
                    _money_kpi("Net income", net),
                )
            ),
            Table(("Line", "Amount"), tuple(rows)),
        ),
    )


def _b_balance_sheet(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Balance Sheet")
    if ctx.financial_statements is None:
        return _not_available(title, "no financial statements provided")
    fs = ctx.financial_statements
    bs = _as_mapping(fs.get("balance_sheet"))
    if not bs:
        return _not_available(title, "no balance sheet in the contract")
    ccy = _fs_currency(fs, ctx)
    total_assets = Money(_as_int(bs.get("total_assets")), ccy)
    total_liab = Money(_as_int(bs.get("total_liabilities")), ccy)
    total_eq = Money(_as_int(bs.get("total_equity")), ccy)
    balanced = _as_bool(bs.get("balanced"))

    rows: list[tuple[str, str]] = []
    for group, key in (("Assets", "assets"), ("Liabilities", "liabilities"), ("Equity", "equity")):
        for label, amt in _fs_lines(bs.get(key)):
            rows.append((f"{group}: {label}", format_money(Money(amt, ccy))))
    rows.append(("Total assets", format_money(total_assets)))
    rows.append(("Total liabilities", format_money(total_liab)))
    rows.append(("Total equity", format_money(total_eq)))

    check_text = (
        "Assets = Liabilities + Equity (balanced)."
        if balanced
        else "WARNING: assets do not equal liabilities plus equity."
    )
    return ReportSection(
        title=title,
        blocks=(
            KpiRow(
                (
                    _money_kpi("Total assets", total_assets),
                    _money_kpi("Total liabilities", total_liab),
                    _money_kpi("Total equity", total_eq),
                )
            ),
            Table(("Line", "Amount"), tuple(rows)),
            Narrative(check_text),
        ),
    )


def _b_cash_flow(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Cash Flow Statement")
    if ctx.financial_statements is None:
        return _not_available(title, "no financial statements provided")
    fs = ctx.financial_statements
    cf = _as_mapping(fs.get("cash_flow"))
    if not cf:
        return _not_available(title, "no cash flow statement in the contract")
    ccy = _fs_currency(fs, ctx)
    net_change = Money(_as_int(cf.get("net_change")), ccy)
    ending = Money(_as_int(cf.get("ending_cash")), ccy)

    rows: list[tuple[str, str]] = []
    for group, key in (("Operating", "operating"), ("Investing", "investing"), ("Financing", "financing")):
        for label, amt in _fs_lines(cf.get(key)):
            rows.append((f"{group}: {label}", format_money(Money(amt, ccy))))
    rows.append(("Net change in cash", format_money(net_change)))
    rows.append(("Ending cash", format_money(ending)))

    return ReportSection(
        title=title,
        blocks=(
            KpiRow(
                (
                    _money_kpi("Net change in cash", net_change),
                    _money_kpi("Ending cash", ending),
                )
            ),
            Table(("Line", "Amount"), tuple(rows)),
        ),
    )


# --- budget vs actual --------------------------------------------------------
def _actuals_by_category(ctx: DataContext) -> dict[str, Money]:
    """Actual spend per category: from register outflows when present, else from
    the income statement's expense lines."""
    out: dict[str, Money] = {}
    if ctx.transactions:
        for t in ctx.transactions:
            if t.amount_minor < 0:
                mag = Money(-t.amount_minor, t.currency)
                out[t.category] = out.get(t.category, Money.zero(t.currency)) + mag
        return out
    if ctx.financial_statements is not None:
        fs = ctx.financial_statements
        inc = _as_mapping(fs.get("income_statement"))
        ccy = _fs_currency(fs, ctx)
        for label, amt in _fs_lines(inc.get("expenses")):
            out[label] = out.get(label, Money.zero(ccy)) + Money(amt, ccy)
    return out


def _b_budget_vs_actual(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Budget vs Actual")
    if not ctx.budget:
        return _not_available(title, "no budget provided")
    actuals = _actuals_by_category(ctx)
    categories = sorted(set(ctx.budget) | set(actuals))
    rows: list[tuple[str, ...]] = []
    total_budget = Money.zero(ctx.currency)
    total_actual = Money.zero(ctx.currency)
    for cat in categories:
        budget = ctx.budget.get(cat, Money.zero(ctx.currency))
        actual = actuals.get(cat, Money.zero(ctx.currency))
        variance = actual - budget  # >0 = over budget (unfavorable for spend)
        total_budget = total_budget + budget
        total_actual = total_actual + actual
        flag = "OVER" if variance.is_positive else "under" if variance.is_negative else "on"
        rows.append(
            (cat, format_money(budget), format_money(actual), format_money(variance), flag)
        )
    total_var = total_actual - total_budget
    rows.append(
        ("Total", format_money(total_budget), format_money(total_actual), format_money(total_var),
         "OVER" if total_var.is_positive else "under" if total_var.is_negative else "on")
    )
    return ReportSection(
        title=title,
        blocks=(
            KpiRow(
                (
                    _money_kpi("Budgeted", total_budget),
                    _money_kpi("Actual", total_actual),
                    ("Variance", format_money(total_var), total_var.is_positive),
                )
            ),
            Table(("Category", "Budget", "Actual", "Variance", "Status"), tuple(rows)),
        ),
    )


# --- register ----------------------------------------------------------------
def _b_transaction_summary(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Transaction Summary")
    if not ctx.transactions:
        return _not_available(title, "no transactions in the period")
    ins: dict[str, Money] = {}
    outs: dict[str, Money] = {}
    for t in ctx.transactions:
        if t.amount_minor >= 0:
            ins[t.category] = ins.get(t.category, Money.zero(t.currency)) + Money(t.amount_minor, t.currency)
        else:
            outs[t.category] = outs.get(t.category, Money.zero(t.currency)) + Money(-t.amount_minor, t.currency)
    categories = sorted(set(ins) | set(outs))
    total_in = Money.zero(ctx.currency)
    total_out = Money.zero(ctx.currency)
    rows: list[tuple[str, ...]] = []
    for cat in categories:
        money_in = ins.get(cat, Money.zero(ctx.currency))
        money_out = outs.get(cat, Money.zero(ctx.currency))
        net = money_in - money_out
        total_in = total_in + money_in
        total_out = total_out + money_out
        rows.append((cat, format_money(money_in), format_money(money_out), format_money(net)))
    net_total = total_in - total_out
    rows.append(("Total", format_money(total_in), format_money(total_out), format_money(net_total)))
    return ReportSection(
        title=title,
        blocks=(
            KpiRow(
                (
                    _money_kpi("Money in", total_in),
                    _money_kpi("Money out", total_out),
                    _money_kpi("Net", net_total),
                )
            ),
            Table(("Category", "Money in", "Money out", "Net"), tuple(rows)),
        ),
    )


# --- billing / usage ---------------------------------------------------------
def _b_usage_summary(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Usage & Billing")
    if ctx.usage is None:
        return _not_available(title, "no usage summary")
    u = ctx.usage
    blocks: list[Block] = [
        KpiRow(
            (
                ("Analyst minutes", f"{u.analyst_minutes:,}", False),
                ("Briefings sent", f"{u.briefings_sent:,}", False),
                ("API calls", f"{u.api_calls:,}", False),
                ("Active tenants", f"{u.active_tenants:,}", False),
            )
        )
    ]
    if ctx.account is not None:
        plan = plan_for(ctx.account.tier)
        invoice = build_invoice(ctx.account, plan, u)
        rows = tuple(
            (
                ln.description,
                str(ln.quantity),
                format_money(ln.unit_amount),
                format_money(ln.amount),
            )
            for ln in invoice.lines
        )
        blocks.append(Table(("Line", "Qty", "Unit", "Amount"), rows))
        blocks.append(Narrative(f"Invoice preview total: {format_money(invoice.total)}."))
    return ReportSection(title=title, blocks=tuple(blocks))


# --- scenario / what-if ------------------------------------------------------
def _b_scenario_compare(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Scenario vs Baseline")
    if ctx.forecast_inputs is None or ctx.forecast_config is None or ctx.scenario is None:
        return _not_available(title, "no scenario inputs")
    _, diff = run_scenario(ctx.forecast_inputs, ctx.forecast_config, ctx.scenario)
    before = "none" if diff.breach_week_before is None else f"week {diff.breach_week_before}"
    after = "none" if diff.breach_week_after is None else f"week {diff.breach_week_after}"
    rows = tuple(
        (f"W{i + 1}", format_money(d)) for i, d in enumerate(diff.weekly_closing_deltas)
    )
    return ReportSection(
        title=title,
        blocks=(
            KpiRow(
                (
                    ("Trough Δ", format_money(diff.trough_delta), diff.trough_delta.is_negative),
                    ("Cushion Δ", format_money(diff.cushion_delta), diff.cushion_delta.is_negative),
                    ("Breach before", before, diff.breach_week_before is not None),
                    ("Breach after", after, diff.breach_week_after is not None),
                )
            ),
            Table(("Week", "Closing Δ"), rows),
        ),
    )


# --- reconciliation / trust --------------------------------------------------
def _b_trust_check(ctx: DataContext, params: Mapping[str, object]) -> ReportSection:
    title = _p_str(params, "title", "Reconciliation / Trust")
    if ctx.recon is None:
        return _not_available(title, "no reconciliation figures")
    r = ctx.recon
    d = check(
        r.tenant_id,
        ctx.as_of,
        r.ledger_cash,
        r.bank_cash,
        r.forecast_opening,
        minor_tolerance=r.minor_tolerance,
        major_tolerance=r.major_tolerance,
    )
    return ReportSection(
        title=title,
        blocks=(
            KpiRow(
                (
                    _money_kpi("Ledger cash", d.ledger_cash),
                    _money_kpi("Bank cash", d.bank_cash),
                    _money_kpi("Forecast opening", d.forecast_opening),
                    ("Worst gap", format_money(d.worst_gap), d.worst_gap.is_positive),
                )
            ),
            Narrative(f"{d.severity.value.upper()}: {d.note}"),
        ),
    )


# --- the registry ------------------------------------------------------------
REGISTRY: dict[str, SectionBuilder] = {
    # presentation-only
    "narrative": _b_narrative,
    "kpi_row": _b_kpi_row,
    "table": _b_table,
    "chart": _b_chart,
    # source-bound composites
    "cash_outlook": _b_cash_outlook,
    "cash_drivers": _b_cash_drivers,
    "runway": _b_runway,
    "ar_aging": _b_ar_aging,
    "ar_chase": _b_ar_chase,
    "income_statement": _b_income_statement,
    "balance_sheet": _b_balance_sheet,
    "cash_flow": _b_cash_flow,
    "budget_vs_actual": _b_budget_vs_actual,
    "transaction_summary": _b_transaction_summary,
    "usage_summary": _b_usage_summary,
    "scenario_compare": _b_scenario_compare,
    "trust_check": _b_trust_check,
}


def known_kinds() -> frozenset[str]:
    """The set of section kinds the registry can render."""
    return frozenset(REGISTRY)
