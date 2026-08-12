"""Forecast version identity and reproducibility fingerprinting.

A forecast is only trustworthy if it can be reproduced and, once published,
never silently changes. We capture the versioned inputs the blueprint calls for
(engine, assumption, mapping, prediction-model versions, calendar/rounding) plus
a content fingerprint of the exact inputs + config + scenario assumptions. Same
inputs -> same fingerprint -> same forecast.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from enum import Enum
from typing import Any

from .enums import PublicationStatus, Scenario
from .money import Money


def _canonical(value: Any) -> Any:
    """Convert a value into a JSON-serializable, order-stable canonical form."""
    if isinstance(value, Money):
        return {"__money__": [value.minor_units, value.currency]}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _canonical(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    return value


def fingerprint(*parts: Any) -> str:
    """Stable SHA-256 hex digest over the canonical form of the given parts."""
    canonical = _canonical(list(parts))
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ForecastVersion:
    engine_version: str
    assumption_version: str
    mapping_version: str
    model_version: str
    timezone: str
    rounding_policy: str
    scenario: Scenario
    input_fingerprint: str
    status: PublicationStatus = PublicationStatus.PRELIMINARY
    published_at: str | None = None
    reviewer: str | None = None

    @property
    def version_id(self) -> str:
        """Short, human-referenceable id combining engine version and fingerprint."""
        return f"{self.engine_version}+{self.scenario.value.lower()}.{self.input_fingerprint[:12]}"
