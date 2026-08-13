"""Acceptance tests for the reports engine, baseline library, builder, and
renderers."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from html import escape
from typing import Any, Callable, cast

import pytest

from rgnr8_forecast import Money

from rgnr8_reports import (
    BASELINE_REPORTS,
    DataContext,
    InMemorySavedReportStore,
    Narrative,
    Report,
    ReportSpec,
    ReportSpecError,
    SqlSavedReportStore,
    Table,
    baseline,
    build_report,
    known_kinds,
    render,
    render_csv,
    render_html,
    spec_to_dict,
    to_dict,
    to_json,
)


# --- baseline coverage -------------------------------------------------------
def test_ten_baselines_present() -> None:
    assert len(BASELINE_REPORTS) == 10
    assert set(BASELINE_REPORTS) == {
        "cash_flow_outlook", "weekly_briefing", "financial_statements", "ar_aging",
        "budget_vs_actual", "runway", "transaction_summary", "usage_billing",
        "exec_board_pack", "reconciliation",
    }
    # every section kind used by a baseline is a registered kind
    for spec in BASELINE_REPORTS.values():
        for section in spec.sections:
            assert section.kind in known_kinds()


@pytest.mark.parametrize("report_id", sorted(BASELINE_REPORTS))
def test_baseline_renders_to_html_and_dict(
    report_id: str,
    full_context: DataContext,
    clock: Callable[[], datetime],
) -> None:
    spec = baseline(report_id)
    report = render(spec, full_context, clock=clock)
    assert isinstance(report, Report)
    assert report.generated_at == clock()
    assert report.sections  # non-empty

    html = render_html(report)
    assert html.startswith("<!doctype html>")
    assert escape(report.title) in html

    d = to_dict(report)
    assert d["title"] == report.title
    # to_json is valid JSON
    json_text = to_json(report)
    assert report.title in json_text


# --- spot-checked numbers ----------------------------------------------------
def test_ar_aging_spot_check(full_context: DataContext, clock: Callable[[], datetime]) -> None:
    report = render(baseline("ar_aging"), full_context, clock=clock)
    html = render_html(report)
    # Total AR is 10,000 + 5,000 + 8,000 = 23,000
    assert "$23,000.00" in html
    # There is an overdue chase list (INV-1 and INV-3 are past due)
    assert "INV-1" in html and "INV-3" in html


def test_financial_statements_spot_check(
    full_context: DataContext, clock: Callable[[], datetime]
) -> None:
    report = render(baseline("financial_statements"), full_context, clock=clock)
    titles = [s.title for s in report.sections]
    assert titles == ["Income Statement", "Balance Sheet", "Cash Flow Statement"]
    html = render_html(report)
    assert "$3,500.00" in html    # net income
    assert "$73,000.00" in html   # total assets
    assert "$50,000.00" in html   # ending cash


def test_transaction_summary_spot_check(
    full_context: DataContext, clock: Callable[[], datetime]
) -> None:
    report = render(baseline("transaction_summary"), full_context, clock=clock)
    html = render_html(report)
    assert "$8,000.00" in html   # money in: 5,000 + 3,000
    assert "$16,000.00" in html  # money out: 4,000 + 12,000


def test_runway_is_finite(full_context: DataContext, clock: Callable[[], datetime]) -> None:
    report = render(baseline("runway"), full_context, clock=clock)
    html = render_html(report)
    assert "months" in html.lower()


def test_reconciliation_spot_check(
    full_context: DataContext, clock: Callable[[], datetime]
) -> None:
    report = render(baseline("reconciliation"), full_context, clock=clock)
    html = render_html(report)
    # ledger 50,000 vs bank 49,500 → 500 gap → MINOR
    assert "MINOR" in html
    assert "$500.00" in html


def test_usage_billing_invoice_preview(
    full_context: DataContext, clock: Callable[[], datetime]
) -> None:
    report = render(baseline("usage_billing"), full_context, clock=clock)
    html = render_html(report)
    # Assisted plan: base 499 + 80 overage analyst minutes @ 2.50 = 200 → 699 total
    assert "$699.00" in html
    assert "200" in html  # analyst minutes


# --- CSV ---------------------------------------------------------------------
def test_csv_flattens_tables(full_context: DataContext, clock: Callable[[], datetime]) -> None:
    report = render(baseline("ar_aging"), full_context, clock=clock)
    csv_text = render_csv(report)
    lines = csv_text.splitlines()
    # section header + column header appear; a bucket row is present
    assert "Aging Summary" in csv_text
    assert "Bucket,Open amount" in csv_text
    assert any(line.startswith("Current,") for line in lines)


def test_csv_multiple_tables_separated(
    full_context: DataContext, clock: Callable[[], datetime]
) -> None:
    report = render(baseline("financial_statements"), full_context, clock=clock)
    csv_text = render_csv(report)
    # three tables, each prefixed by its section header, blank-line separated
    assert "Income Statement" in csv_text
    assert "Balance Sheet" in csv_text
    assert "Cash Flow Statement" in csv_text
    assert "\r\n\r\n" in csv_text  # blank-line separator between tables


# --- self-contained HTML -----------------------------------------------------
def test_html_is_self_contained(
    full_context: DataContext, clock: Callable[[], datetime]
) -> None:
    for report_id in BASELINE_REPORTS:
        report = render(baseline(report_id), full_context, clock=clock)
        html = render_html(report)
        assert "http://" not in html
        assert "https://" not in html
        assert "<style>" in html  # CSS inlined


# --- graceful degradation ----------------------------------------------------
def test_missing_data_degrades_to_not_available(clock: Callable[[], datetime]) -> None:
    empty = DataContext(period="Empty", as_of=date(2026, 8, 1))
    report = render(baseline("financial_statements"), empty, clock=clock)
    # every section falls back to a single "Not available" narrative
    for section in report.sections:
        assert len(section.blocks) == 1
        block = section.blocks[0]
        assert isinstance(block, Narrative)
        assert "Not available" in block.text
    # and it still renders to HTML without error
    assert "Not available" in render_html(report)


# --- custom builder ----------------------------------------------------------
def test_build_report_from_section_list(
    full_context: DataContext, clock: Callable[[], datetime]
) -> None:
    spec_json = [
        {"kind": "narrative", "title": "Intro", "params": {"text": "Hello owner."}},
        {"kind": "ar_aging", "title": "Receivables"},
        {"kind": "kpi_row", "title": "Highlights",
         "params": {"items": [["Cash", "$50,000.00", False], ["Risk", "-$500.00", True]]}},
    ]
    spec = build_report(spec_json)
    assert isinstance(spec, ReportSpec)
    assert [s.kind for s in spec.sections] == ["narrative", "ar_aging", "kpi_row"]

    report = render(spec, full_context, clock=clock)
    html = render_html(report)
    assert "Hello owner." in html
    assert "$23,000.00" in html  # ar_aging rendered against the context


def test_build_report_rejects_unknown_kind() -> None:
    with pytest.raises(ReportSpecError):
        build_report([{"kind": "does_not_exist", "title": "x"}])


def test_build_report_accepts_full_object() -> None:
    spec = build_report({
        "id": "my_report",
        "title": "My Report",
        "description": "custom",
        "sections": [{"kind": "runway"}],
    })
    assert spec.id == "my_report"
    assert spec.title == "My Report"


# --- saved report stores -----------------------------------------------------
def _sample_custom_spec() -> ReportSpec:
    return build_report({
        "id": "custom_pack",
        "title": "Custom Pack",
        "sections": [
            {"kind": "cash_outlook", "title": "Cash"},
            {"kind": "narrative", "params": {"text": "note"}},
        ],
    })


def test_inmemory_store_roundtrip() -> None:
    store = InMemorySavedReportStore()
    spec = _sample_custom_spec()
    store.save("tenant-a", spec)
    loaded = store.get("tenant-a", "custom_pack")
    assert loaded is not None
    assert spec_to_dict(loaded) == spec_to_dict(spec)
    assert store.get("tenant-b", "custom_pack") is None
    assert [s.id for s in store.list_for_tenant("tenant-a")] == ["custom_pack"]


def test_sql_store_roundtrip_sqlite(
    full_context: DataContext, clock: Callable[[], datetime]
) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        store = SqlSavedReportStore(cast(Any, conn), placeholder="?")
        store.create_schema()
        spec = _sample_custom_spec()
        store.save("tenant-a", spec)

        loaded = store.get("tenant-a", "custom_pack")
        assert loaded is not None
        assert spec_to_dict(loaded) == spec_to_dict(spec)

        # tenant isolation
        assert store.get("tenant-b", "custom_pack") is None
        assert [s.id for s in store.list_for_tenant("tenant-a")] == ["custom_pack"]

        # a reloaded spec still renders
        report = render(loaded, full_context, clock=clock)
        assert render_html(report).startswith("<!doctype html>")
    finally:
        conn.close()


# --- block model -------------------------------------------------------------
def test_to_dict_block_kinds(full_context: DataContext, clock: Callable[[], datetime]) -> None:
    report = render(baseline("cash_flow_outlook"), full_context, clock=clock)
    d = to_dict(report)
    sections = cast("list[dict[str, Any]]", d["sections"])
    kinds: set[str] = set()
    for section in sections:
        for block in cast("list[dict[str, Any]]", section["blocks"]):
            kinds.add(cast(str, block["kind"]))
    # cash_outlook produces a narrative, a kpi row, a table, and a chart
    assert {"narrative", "kpi_row", "table", "chart"} <= kinds
