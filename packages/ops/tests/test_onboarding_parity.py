"""Cross-language contract: the Python COA category slugs must match the TS
`ledger-kernel` BusinessCategory enum values exactly (same one-to-one mapping the
`OnboardingRegistry` promises). If someone adds a template on one side, this
fails until the other side catches up."""

from __future__ import annotations

import re
from pathlib import Path

from rgnr8_ops import BUSINESS_CATEGORIES, SOURCE_SYSTEMS

_TS = Path(__file__).resolve().parents[2] / "ledger-kernel" / "src" / "coaTemplates.ts"


def _ts_enum_values() -> set[str]:
    text = _TS.read_text()
    body = text.split("export enum BusinessCategory", 1)[1].split("}", 1)[0]
    # lines like:  SERVICE_GENERAL = "SERVICE_GENERAL",
    return set(re.findall(r'=\s*"([A-Z_]+)"', body))


def test_python_slugs_match_ts_business_category_enum() -> None:
    py = {slug for slug, _ in BUSINESS_CATEGORIES}
    ts = _ts_enum_values()
    assert py == ts, f"drift: py-only={py - ts}, ts-only={ts - py}"


def test_source_systems_match_ts_cutover_type() -> None:
    text = (_TS.parent / "cutover.ts").read_text()
    body = text.split("export type SourceSystem", 1)[1].split(";", 1)[0]
    ts = set(re.findall(r'"([a-z]+)"', body))
    assert SOURCE_SYSTEMS == ts, f"drift: py={SOURCE_SYSTEMS}, ts={ts}"
