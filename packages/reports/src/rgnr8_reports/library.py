"""The baseline report library — ten ready-to-run :class:`ReportSpec` definitions.

Each baseline is a plain spec composed from the section kinds in
:mod:`rgnr8_reports.sections`, so it renders against any
:class:`~rgnr8_reports.context.DataContext` and degrades gracefully where a data
source is absent. These are what a tenant sees out of the box; the custom builder
produces the same shape for bespoke reports.
"""

from __future__ import annotations

from .model import ReportSpec, SectionSpec

BASELINE_REPORTS: dict[str, ReportSpec] = {
    "cash_flow_outlook": ReportSpec(
        id="cash_flow_outlook",
        title="Cash Flow Outlook",
        description="13-week direct cash projection, the drivers behind it, and status.",
        sections=(
            SectionSpec("cash_outlook", "13-Week Cash Outlook"),
            SectionSpec("cash_drivers", "What's Draining Cash"),
        ),
    ),
    "weekly_briefing": ReportSpec(
        id="weekly_briefing",
        title="Weekly Owner Briefing",
        description="Status, recommended action, key facts, and the week-by-week glance.",
        sections=(
            SectionSpec("cash_outlook", "This Week"),
            SectionSpec("cash_drivers", "Cash Drivers"),
            SectionSpec("runway", "Runway"),
        ),
    ),
    "financial_statements": ReportSpec(
        id="financial_statements",
        title="Financial Statements Pack",
        description="P&L, balance sheet, and cash flow from the financial-statements/1 contract.",
        sections=(
            SectionSpec("income_statement", "Income Statement"),
            SectionSpec("balance_sheet", "Balance Sheet"),
            SectionSpec("cash_flow", "Cash Flow Statement"),
        ),
    ),
    "ar_aging": ReportSpec(
        id="ar_aging",
        title="AR Aging & Collections",
        description="Aging buckets, DSO, and a prioritized collections chase list.",
        sections=(
            SectionSpec("ar_aging", "Aging Summary"),
            SectionSpec("ar_chase", "Collections Chase List"),
        ),
    ),
    "budget_vs_actual": ReportSpec(
        id="budget_vs_actual",
        title="Budget vs Actual",
        description="Per-category variance of actuals against the provided budget.",
        sections=(SectionSpec("budget_vs_actual", "Budget vs Actual"),),
    ),
    "runway": ReportSpec(
        id="runway",
        title="Runway",
        description="Months of runway at the current net burn, plus the projected trough.",
        sections=(SectionSpec("runway", "Runway"),),
    ),
    "transaction_summary": ReportSpec(
        id="transaction_summary",
        title="Transaction Summary",
        description="Categorized money in and money out for the period, from the register.",
        sections=(SectionSpec("transaction_summary", "Transaction Summary"),),
    ),
    "usage_billing": ReportSpec(
        id="usage_billing",
        title="Usage & Billing",
        description="Analyst minutes and metered usage with an invoice preview.",
        sections=(SectionSpec("usage_summary", "Usage & Billing"),),
    ),
    "exec_board_pack": ReportSpec(
        id="exec_board_pack",
        title="Executive Board Pack",
        description="A combined pack: cash outlook, income statement, AR, and runway.",
        sections=(
            SectionSpec("cash_outlook", "Cash Outlook"),
            SectionSpec("income_statement", "Income Statement"),
            SectionSpec("ar_aging", "Accounts Receivable"),
            SectionSpec("runway", "Runway"),
        ),
    ),
    "reconciliation": ReportSpec(
        id="reconciliation",
        title="Reconciliation / Trust",
        description="Ledger vs bank vs forecast opening — the silent-divergence check.",
        sections=(SectionSpec("trust_check", "Trust Check"),),
    ),
}


def baseline(report_id: str) -> ReportSpec:
    """Look up a baseline :class:`ReportSpec` by id; raises ``KeyError`` if unknown."""
    return BASELINE_REPORTS[report_id]
