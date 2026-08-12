"""Customer payment-timing prediction.

Deterministic and explainable: given a customer's history of (due, paid) pairs,
predict how many days after the due date they typically pay. The estimate is the
median days-late (robust to the occasional very-late invoice); confidence falls
out of the sample size and dispersion. No randomness, no hidden state.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .models import CustomerHistory, ForecastConfig


@dataclass(frozen=True, slots=True)
class TimingPrediction:
    days_late: int
    sample_size: int
    dispersion_days: int  # rounded standard deviation, for stress/confidence
    basis: str
    numeric_confidence: int  # 0..100


def predict_days_late(
    history: CustomerHistory | None,
    config: ForecastConfig,
) -> TimingPrediction:
    """Predict a customer's typical days-late (may be negative if they pay early)."""
    # 1) Explicit override wins (customer-specific policy).
    if history is not None and history.override_days_late is not None:
        return TimingPrediction(
            days_late=history.override_days_late,
            sample_size=0,
            dispersion_days=0,
            basis=f"override: {history.override_days_late:+d} days vs due",
            numeric_confidence=90,
        )

    samples = [o.days_late for o in history.observations] if history else []

    # 2) Enough history → learn from it.
    if len(samples) >= config.min_history_observations:
        med = int(round(statistics.median(samples)))
        disp = int(round(statistics.pstdev(samples))) if len(samples) > 1 else 0
        # More samples and tighter dispersion => higher confidence.
        conf = min(95, 45 + 5 * len(samples) - 2 * disp)
        conf = max(30, conf)
        return TimingPrediction(
            days_late=med,
            sample_size=len(samples),
            dispersion_days=disp,
            basis=(
                f"median of {len(samples)} paid invoices: {med:+d} days vs due "
                f"(±{disp}d)"
            ),
            numeric_confidence=conf,
        )

    # 3) Not enough history → configured default.
    return TimingPrediction(
        days_late=config.default_payment_delay_days,
        sample_size=len(samples),
        dispersion_days=0,
        basis=(
            f"default {config.default_payment_delay_days:+d} days "
            f"({len(samples)} prior payments, below {config.min_history_observations} needed)"
        ),
        numeric_confidence=40,
    )
