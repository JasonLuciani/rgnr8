"""The report model: specifications (what to render) and results (what rendered).

Two layers, kept deliberately separate:

* **Specification** — :class:`SectionSpec` and :class:`ReportSpec` describe a report
  declaratively (which section kinds, in what order, with what params). A spec is
  data: it is what the baseline library ships, what the custom builder produces,
  and what a :class:`~rgnr8_reports.builder.SavedReportStore` persists.
* **Result** — :class:`Report`, :class:`ReportSection`, and the :data:`Block`
  union (:class:`KpiRow`, :class:`Table`, :class:`Narrative`, :class:`Chart`) are
  the rendered output the engine produces from a spec + a
  :class:`~rgnr8_reports.context.DataContext`. Results are what the renderers turn
  into HTML / CSV / JSON.

Everything here is a frozen dataclass with tuple fields, so a rendered report is
immutable and hashes/serializes deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Union

# --- specification -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SectionSpec:
    """One section of a report: a ``kind`` (bound to a builder in the registry),
    an optional display ``title`` (overrides the builder's default), and ``params``
    that parameterize the section (thresholds, column choices, inline content)."""

    kind: str
    title: str = ""
    params: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReportSpec:
    """A full report definition: an id, human title/description, an ordered tuple
    of sections, and report-level params. This is the unit the library ships and
    the store persists."""

    id: str
    title: str
    description: str = ""
    sections: tuple[SectionSpec, ...] = ()
    params: Mapping[str, object] = field(default_factory=dict)


# --- rendered blocks ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KpiRow:
    """A row of headline figures. Each item is ``(label, value, is_negative)`` —
    ``is_negative`` drives risk coloring in the renderers."""

    items: tuple[tuple[str, str, bool], ...]


@dataclass(frozen=True, slots=True)
class Table:
    """A tabular block: column headers plus already-formatted string rows."""

    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class Narrative:
    """A prose block — status text, an action, or a "not available" notice."""

    text: str


@dataclass(frozen=True, slots=True)
class Chart:
    """A chart rendered as a self-contained inline-SVG string (no external URLs)."""

    svg: str


Block = Union[KpiRow, Table, Narrative, Chart]


@dataclass(frozen=True, slots=True)
class ReportSection:
    """A titled group of rendered blocks."""

    title: str
    blocks: tuple[Block, ...]


@dataclass(frozen=True, slots=True)
class Report:
    """A fully rendered report: title, the period it covers, when it was generated
    (stamped by the engine's injected clock), and its ordered sections."""

    title: str
    period: str
    generated_at: datetime
    sections: tuple[ReportSection, ...]
