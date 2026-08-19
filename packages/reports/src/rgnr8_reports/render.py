"""Renderers — turn a rendered :class:`Report` into HTML, CSV, or JSON.

* :func:`render_html` — a self-contained, branded HTML document. It inlines the
  shared brand tokens + component CSS (:data:`RG_TOKENS_CSS` / :data:`RG_BASE_CSS`)
  and the RGNR8 brand bar, so a report looks exactly like the rest of the app and
  makes **zero external calls** (no ``http://`` — owner pages are audited for
  that). KPI rows render as tiles, tables as branded tables, narratives as prose,
  charts as inline SVG; any negative figure is colored with the risk token.
* :func:`render_csv` — flattens every :class:`Table` block to CSV; multiple tables
  are separated by a blank line and a section header, so a multi-section report
  round-trips into a spreadsheet.
* :func:`to_dict` / :func:`to_json` — a stable, JSON-safe view of the report.
"""

from __future__ import annotations

import csv
import io
import json
from html import escape

from rgnr8_forecast.brand import RG_BASE_CSS, RG_TOKENS_CSS, brand_bar

from .model import Block, KpiRow, Narrative, Report, Table


# --- HTML --------------------------------------------------------------------
def _is_negish(value: str) -> bool:
    """Whether a formatted cell reads as a negative amount (for risk coloring)."""
    return value.strip().startswith("-")


def _kpi_html(block: KpiRow) -> str:
    tiles: list[str] = []
    for label, value, is_negative in block.items:
        cls = "v neg" if is_negative else "v"
        tiles.append(
            f'<div class="tile"><div class="k">{escape(label)}</div>'
            f'<div class="{cls}">{escape(value)}</div></div>'
        )
    return f'<div class="tiles">{"".join(tiles)}</div>'


def _table_html(block: Table) -> str:
    head = "".join(f"<th>{escape(c)}</th>" for c in block.columns)
    body_rows: list[str] = []
    for row in block.rows:
        cells: list[str] = []
        for i, cell in enumerate(row):
            classes: list[str] = []
            if i > 0:
                classes.append("num")
            if _is_negish(cell):
                classes.append("neg")
            attr = f' class="{" ".join(classes)}"' if classes else ""
            cells.append(f"<td{attr}>{escape(cell)}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    return (
        '<div class="table-scroll"><table>'
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div>"
    )


def _block_html(block: Block) -> str:
    if isinstance(block, KpiRow):
        return _kpi_html(block)
    if isinstance(block, Table):
        return _table_html(block)
    if isinstance(block, Narrative):
        return f"<p>{escape(block.text)}</p>"
    # Chart — the SVG is generated internally (inline, no external refs).
    return f'<div class="chart">{block.svg}</div>'


def render_html(report: Report) -> str:
    """Render ``report`` as a self-contained, branded HTML document."""
    sections_html: list[str] = []
    for section in report.sections:
        blocks = "".join(_block_html(b) for b in section.blocks)
        sections_html.append(
            f'<section class="card"><h2>{escape(section.title)}</h2>{blocks}</section>'
        )
    meta = report.generated_at.isoformat()
    period = f' · {escape(report.period)}' if report.period else ""
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(report.title)}</title>"
        f"<style>{RG_TOKENS_CSS}\n{RG_BASE_CSS}\n"
        "h2{font-family:var(--rg-serif);font-size:19px;margin:0 0 14px}"
        ".chart{margin-top:8px}</style></head><body>"
        f"{brand_bar('Reports')}"
        '<main class="rg-shell">'
        f'<div class="rg-eyebrow">Report</div><h1>{escape(report.title)}</h1>'
        f'<p class="rg-foot">Generated {escape(meta)}{period}</p>'
        f"{''.join(sections_html)}"
        "</main></body></html>"
    )


# --- CSV ---------------------------------------------------------------------
def render_csv(report: Report) -> str:
    """Flatten every :class:`Table` in the report to CSV.

    Each table is prefixed with its section title; tables are separated by a blank
    line, so a multi-table report reads cleanly in a spreadsheet.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    first = True
    for section in report.sections:
        for block in section.blocks:
            if not isinstance(block, Table):
                continue
            if not first:
                writer.writerow([])
            first = False
            writer.writerow([section.title])
            writer.writerow(list(block.columns))
            for row in block.rows:
                writer.writerow(list(row))
    return buf.getvalue()


# --- dict / JSON -------------------------------------------------------------
def _block_dict(block: Block) -> dict[str, object]:
    if isinstance(block, KpiRow):
        return {
            "kind": "kpi_row",
            "items": [[label, value, is_negative] for (label, value, is_negative) in block.items],
        }
    if isinstance(block, Table):
        return {
            "kind": "table",
            "columns": list(block.columns),
            "rows": [list(r) for r in block.rows],
        }
    if isinstance(block, Narrative):
        return {"kind": "narrative", "text": block.text}
    return {"kind": "chart", "svg": block.svg}


def to_dict(report: Report) -> dict[str, object]:
    """A stable, JSON-safe dict view of a rendered report."""
    return {
        "title": report.title,
        "period": report.period,
        "generated_at": report.generated_at.isoformat(),
        "sections": [
            {
                "title": s.title,
                "blocks": [_block_dict(b) for b in s.blocks],
            }
            for s in report.sections
        ],
    }


def to_json(report: Report, *, indent: int | None = None) -> str:
    """Serialize a rendered report to JSON."""
    return json.dumps(to_dict(report), indent=indent, sort_keys=True)
