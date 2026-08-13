"""The render engine: turn a :class:`ReportSpec` + :class:`DataContext` into a
:class:`Report`.

The engine is a thin, deterministic orchestrator. For each :class:`SectionSpec`
in the spec it looks the section's ``kind`` up in the registry and calls the
builder against the shared context; a spec-level ``title`` overrides the builder's
default. ``generated_at`` is stamped from an **injected clock** (never the wall
clock), so a report renders byte-identically in tests and in production given the
same inputs.

An unknown ``kind`` in a spec renders as a "not available" section rather than
raising — specs come from the builder, which validates kinds up front, so this is
only a defensive backstop.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Callable

from .context import DataContext
from .model import Narrative, Report, ReportSection, ReportSpec
from .sections import REGISTRY

Clock = Callable[[], datetime]


def render(spec: ReportSpec, context: DataContext, *, clock: Clock) -> Report:
    """Render ``spec`` against ``context``; stamp ``generated_at`` from ``clock``."""
    sections: list[ReportSection] = []
    for spec_section in spec.sections:
        builder = REGISTRY.get(spec_section.kind)
        if builder is None:
            sections.append(
                ReportSection(
                    title=spec_section.title or spec_section.kind,
                    blocks=(Narrative(f"Not available — unknown section kind {spec_section.kind!r}."),),
                )
            )
            continue
        section = builder(context, spec_section.params)
        if spec_section.title:
            section = dataclasses.replace(section, title=spec_section.title)
        sections.append(section)

    period = context.period or (spec.params.get("period") if isinstance(spec.params.get("period"), str) else "")
    return Report(
        title=spec.title,
        period=period if isinstance(period, str) else "",
        generated_at=clock(),
        sections=tuple(sections),
    )
