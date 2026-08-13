"""The `Alert` value object — one emitted, deliverable alert.

An `Alert` is a frozen, deterministic record produced by an `AlertRule` and
delivered by the dispatcher. Its `key` is the stable dedupe identity: two
evaluations that describe *the same condition* produce the same `key`, so the
dispatcher can fire it once and stay quiet while it persists. `at` is stamped
from the engine's injected clock (epoch seconds) — never the wall clock — so the
same inputs yield the same alert.

`Severity` lives in ``rules`` (it is part of the rule vocabulary); it is imported
here only for type-checking so this module has no runtime dependency on
``rules`` and the two can reference each other without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .rules import Severity


@dataclass(frozen=True, slots=True)
class Alert:
    """A single proactive alert.

    ``key`` is the stable dedupe identity for the *condition* (not the tick), so
    an unchanged condition re-evaluates to an equal key and does not re-fire.
    ``evidence`` carries the machine-readable facts behind the message (amounts
    in minor units, dates as ISO strings) for downstream rendering/telemetry.
    """

    key: str
    severity: "Severity"
    title: str
    message: str
    evidence: dict[str, object] = field(default_factory=dict)
    at: int = 0
