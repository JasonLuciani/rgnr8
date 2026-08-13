"""Turn an out-of-tolerance divergence into an alert payload for the ops layer.

``divergence_alert`` is the seam between the trust monitor and whatever forwards
notifications (email / push / webhook — implemented elsewhere). It returns a plain
JSON-safe payload when a divergence is anything other than ``IN_SYNC``, and
``None`` when the figures agree, so a caller can map the whole fleet through it and
forward exactly the non-empty results. Pure and deterministic — no clock, no
transport, no side effects.
"""

from __future__ import annotations

from .divergence import Divergence, Severity, money_to_dict


def divergence_alert(divergence: Divergence) -> dict[str, object] | None:
    """An alert payload for an out-of-sync divergence, or ``None`` if in sync."""
    if divergence.severity is Severity.IN_SYNC:
        return None

    return {
        "severity": divergence.severity.value,
        "tenant": divergence.tenant_id,
        "as_of": divergence.as_of.isoformat(),
        "message": divergence.note,
        "amounts": {
            "ledger_cash": money_to_dict(divergence.ledger_cash),
            "bank_cash": money_to_dict(divergence.bank_cash),
            "forecast_opening": money_to_dict(divergence.forecast_opening),
            "ledger_vs_bank": money_to_dict(divergence.ledger_vs_bank),
            "bank_vs_forecast": money_to_dict(divergence.bank_vs_forecast),
            "worst_gap": money_to_dict(divergence.worst_gap),
        },
    }
