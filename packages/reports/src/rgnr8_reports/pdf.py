"""PDF renderer — turn a rendered :class:`Report` into a polished, branded PDF.

Board packs and investor updates get sent as PDFs, so a report needs a
print-quality export that looks like the rest of RGNR8: the forest brand bar on
every page, an editorial serif title, KPI tiles, and branded tables where every
negative figure is colored with the risk token.

The PDF is built with :mod:`reportlab` directly from the structured
:class:`Report` model (not from HTML), so it stays a pure in-process render with
**no external calls** and no headless browser. Output is **byte-deterministic**:
:data:`reportlab.rl_config.invariant` is enabled, which fixes reportlab's internal
timestamp and document id, so the same report renders to identical bytes every
time — the same determinism guarantee the HTML/CSV/JSON renderers give.

:class:`~rgnr8_reports.model.Chart` blocks are inline SVG meant for the web
surface; they carry no figures a table doesn't, so they are omitted from the PDF
(the tables and KPI rows carry every number). Everything else round-trips.
"""

from __future__ import annotations

import io

from reportlab import rl_config

# Deterministic output: pin reportlab's internal creation timestamp + document id
# so a report renders to identical bytes every run (matches the other renderers).
rl_config.invariant = 1

from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_RIGHT  # noqa: E402
from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import inch  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    BaseDocTemplate,
    Flowable,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table as RLTable,
    TableStyle,
)

from rgnr8_forecast.brand import (
    FOREST,
    IVORY,
    LINE,
    MUTED,
    RISK,
    SAGE,
    INK,
)

from .model import Block, Chart, KpiRow, Narrative, Report, Table

# Brand colors as reportlab colors (single source: rgnr8_forecast.brand).
_FOREST = colors.HexColor(FOREST)
_IVORY = colors.HexColor(IVORY)
_SAGE = colors.HexColor(SAGE)
_LINE = colors.HexColor(LINE)
_MUTED = colors.HexColor(MUTED)
_RISK = colors.HexColor(RISK)
_INK = colors.HexColor(INK)

_MARGIN = 0.75 * inch
_BAR_H = 0.5 * inch  # forest brand bar height at the top of every page


def _is_negish(value: str) -> bool:
    """Whether a formatted cell reads as a negative amount (for risk coloring)."""
    return value.strip().startswith("-")


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "eyebrow": ParagraphStyle(
            "rg-eyebrow", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=8, textColor=_MUTED, spaceAfter=2, leading=10,
        ),
        "title": ParagraphStyle(
            "rg-title", parent=base["Title"], fontName="Times-Bold",
            fontSize=24, textColor=_INK, spaceAfter=4, leading=28, alignment=0,
        ),
        "foot": ParagraphStyle(
            "rg-foot", parent=base["Normal"], fontName="Helvetica",
            fontSize=9, textColor=_MUTED, spaceAfter=14, leading=12,
        ),
        "h2": ParagraphStyle(
            "rg-h2", parent=base["Heading2"], fontName="Times-Bold",
            fontSize=15, textColor=_INK, spaceBefore=6, spaceAfter=8, leading=18,
        ),
        "body": ParagraphStyle(
            "rg-body", parent=base["Normal"], fontName="Helvetica",
            fontSize=10.5, textColor=_INK, spaceAfter=6, leading=15,
        ),
        "cell": ParagraphStyle(
            "rg-cell", parent=base["Normal"], fontName="Helvetica",
            fontSize=9, textColor=_INK, leading=12,
        ),
        "cell_num": ParagraphStyle(
            "rg-cell-num", parent=base["Normal"], fontName="Helvetica",
            fontSize=9, textColor=_INK, leading=12, alignment=TA_RIGHT,
        ),
        "cell_head": ParagraphStyle(
            "rg-cell-head", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=7.5, textColor=_MUTED, leading=10,
        ),
    }


def _kpi_flowable(block: KpiRow, styles: dict[str, ParagraphStyle], width: float) -> Flowable:
    """A KPI row as an evenly-divided, borderless tile strip."""
    cells: list[Paragraph] = []
    for label, value, is_negative in block.items:
        color = RISK if is_negative else FOREST
        cells.append(
            Paragraph(
                f'<font size="7.5" color="{MUTED}">{_esc(label.upper())}</font><br/>'
                f'<font size="15" color="{color}"><b>{_esc(value)}</b></font>',
                styles["cell"],
            )
        )
    n = max(1, len(cells))
    col_w = width / n
    table = RLTable([cells], colWidths=[col_w] * n)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("BOX", (0, 0), (-1, -1), 0.75, _LINE),
                ("INNERGRID", (0, 0), (-1, -1), 0.75, _LINE),
            ]
        )
    )
    return table


def _table_flowable(block: Table, styles: dict[str, ParagraphStyle], width: float) -> Flowable:
    """A data table with an ivory header row and risk-colored negatives."""
    header = [Paragraph(_esc(c.upper()), styles["cell_head"]) for c in block.columns]
    data: list[list[Paragraph]] = [header]
    neg_coords: list[tuple[int, int]] = []
    for r, row in enumerate(block.rows, start=1):
        cells: list[Paragraph] = []
        for c, cell in enumerate(row):
            style = styles["cell_num"] if c > 0 else styles["cell"]
            if _is_negish(cell):
                neg_coords.append((c, r))
                cells.append(Paragraph(f'<font color="{RISK}">{_esc(cell)}</font>', style))
            else:
                cells.append(Paragraph(_esc(cell), style))
        data.append(cells)

    ncols = max(1, len(block.columns))
    # First column takes more room; remaining share the rest.
    if ncols == 1:
        col_widths = [width]
    else:
        first = width * 0.40
        rest = (width - first) / (ncols - 1)
        col_widths = [first] + [rest] * (ncols - 1)

    table = RLTable(data, colWidths=col_widths, repeatRows=1)
    style_cmds: list[tuple[object, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), _IVORY),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, _LINE),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, _LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("BOX", (0, 0), (-1, -1), 0.75, _LINE),
    ]
    table.setStyle(TableStyle(style_cmds))
    return table


def _block_flowables(
    block: Block, styles: dict[str, ParagraphStyle], width: float
) -> list[Flowable]:
    if isinstance(block, KpiRow):
        return [_kpi_flowable(block, styles, width), Spacer(1, 8)]
    if isinstance(block, Table):
        return [_table_flowable(block, styles, width), Spacer(1, 8)]
    if isinstance(block, Narrative):
        return [Paragraph(_esc(block.text), styles["body"])]
    # Chart: inline SVG for the web surface; omitted from the PDF (tables carry
    # every figure). Returning nothing keeps the export clean and deterministic.
    assert isinstance(block, Chart)
    return []


def _esc(text: str) -> str:
    """Escape for reportlab's mini-markup (which parses a subset of XML)."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class _BrandedDoc(BaseDocTemplate):  # type: ignore[misc]  # reportlab is untyped
    """A doc template that paints the forest brand bar + footer on every page."""

    def __init__(self, buf: io.BytesIO, report: Report) -> None:
        super().__init__(
            buf,
            pagesize=letter,
            leftMargin=_MARGIN,
            rightMargin=_MARGIN,
            topMargin=_MARGIN + _BAR_H,
            bottomMargin=_MARGIN,
            title=report.title,
            author="RGNR8",
            creator="RGNR8 Financial OS",
        )
        self._report = report
        frame = Frame(
            self.leftMargin,
            self.bottomMargin,
            self.width,
            self.height,
            id="body",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        self.addPageTemplates([PageTemplate(id="branded", frames=[frame], onPage=self._decorate)])

    def _decorate(self, canvas: object, doc: object) -> None:
        c = canvas  # reportlab canvas
        w, h = letter
        # Forest brand bar across the top.
        c.saveState()  # type: ignore[attr-defined]
        c.setFillColor(_FOREST)  # type: ignore[attr-defined]
        c.rect(0, h - _BAR_H, w, _BAR_H, stroke=0, fill=1)  # type: ignore[attr-defined]
        # Wordmark: "RGNR" ivory + "8" sage.
        c.setFont("Helvetica-Bold", 12)  # type: ignore[attr-defined]
        baseline_y = h - _BAR_H + (_BAR_H - 12) / 2 + 1
        c.setFillColor(_IVORY)  # type: ignore[attr-defined]
        c.drawString(_MARGIN, baseline_y, "RGNR")  # type: ignore[attr-defined]
        rgnr_w = c.stringWidth("RGNR", "Helvetica-Bold", 12)  # type: ignore[attr-defined]
        c.setFillColor(_SAGE)  # type: ignore[attr-defined]
        c.drawString(_MARGIN + rgnr_w, baseline_y, "8")  # type: ignore[attr-defined]
        # Context label, right-aligned.
        c.setFont("Helvetica-Bold", 8)  # type: ignore[attr-defined]
        c.setFillColor(colors.HexColor("#A9B7AC"))  # type: ignore[attr-defined]
        c.drawRightString(w - _MARGIN, baseline_y + 1, "REPORTS")  # type: ignore[attr-defined]
        # Footer: title + page number.
        c.setFont("Helvetica", 8)  # type: ignore[attr-defined]
        c.setFillColor(_MUTED)  # type: ignore[attr-defined]
        c.drawString(_MARGIN, _MARGIN - 14, self._report.title)  # type: ignore[attr-defined]
        c.drawRightString(  # type: ignore[attr-defined]
            w - _MARGIN, _MARGIN - 14, f"Page {doc.page}"  # type: ignore[attr-defined]
        )
        c.restoreState()  # type: ignore[attr-defined]


def render_pdf(report: Report) -> bytes:
    """Render ``report`` as a polished, branded, byte-deterministic PDF."""
    buf = io.BytesIO()
    styles = _styles()
    doc = _BrandedDoc(buf, report)
    width = doc.width

    story: list[Flowable] = [
        Paragraph("REPORT", styles["eyebrow"]),
        Paragraph(_esc(report.title), styles["title"]),
    ]
    meta = f"Generated {report.generated_at.isoformat()}"
    if report.period:
        meta += f" &middot; {_esc(report.period)}"
    story.append(Paragraph(meta, styles["foot"]))

    for section in report.sections:
        block_flowables: list[Flowable] = []
        for block in section.blocks:
            block_flowables.extend(_block_flowables(block, styles, width))
        section_flowables: list[Flowable] = [
            Paragraph(_esc(section.title), styles["h2"]),
            *block_flowables,
            Spacer(1, 10),
        ]
        # Keep a section's heading with its first block where it fits on a page.
        story.append(KeepTogether(section_flowables[:2]))
        story.extend(section_flowables[2:])

    doc.build(story)
    return buf.getvalue()
