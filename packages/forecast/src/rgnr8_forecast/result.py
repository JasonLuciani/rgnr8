"""Top-level forecast result, the owner-facing headline, and the run entrypoint."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date

from .enums import Category, Confidence, Direction, PublicationStatus, Scenario
from .flow import CashFlow
from .models import (
    ForecastConfig,
    ForecastInputs,
    ScenarioAssumptions,
    base_assumptions,
    downside_assumptions,
    upside_assumptions,
)
from .money import Money, money_sum
from .projection import Projection, compute_projection
from .reproducibility import ForecastVersion, fingerprint
from .resolve import resolve


@dataclass(frozen=True, slots=True)
class ForecastResult:
    scenario: Scenario
    version: ForecastVersion
    projection: Projection
    flows: tuple[CashFlow, ...]
    overall_confidence: int
    data_quality: tuple[str, ...]
    headline: str
    recommended_action: str | None

    @property
    def status(self) -> PublicationStatus:
        return self.version.status

    def flow(self, seq: int) -> CashFlow | None:
        for f in self.flows:
            if f.seq == seq:
                return f
        return None

    # --- publication lifecycle (immutability by versioning) ------------------
    def verified(self, reviewer: str) -> "ForecastResult":
        return self._with_status(PublicationStatus.VERIFIED, reviewer=reviewer)

    def published(self, reviewer: str, published_at: str) -> "ForecastResult":
        return self._with_status(
            PublicationStatus.PUBLISHED, reviewer=reviewer, published_at=published_at
        )

    def _with_status(
        self,
        status: PublicationStatus,
        *,
        reviewer: str | None = None,
        published_at: str | None = None,
    ) -> "ForecastResult":
        new_version = dataclasses.replace(
            self.version,
            status=status,
            reviewer=reviewer if reviewer is not None else self.version.reviewer,
            published_at=published_at if published_at is not None else self.version.published_at,
        )
        return dataclasses.replace(self, version=new_version)


def run_forecast(
    inputs: ForecastInputs,
    config: ForecastConfig | None = None,
    scenario: Scenario = Scenario.BASE,
    assumptions: ScenarioAssumptions | None = None,
) -> ForecastResult:
    """Run a single-scenario direct cash forecast."""
    config = config or ForecastConfig(currency=inputs.currency)
    if assumptions is None:
        assumptions = {
            Scenario.BASE: base_assumptions,
            Scenario.DOWNSIDE: downside_assumptions,
            Scenario.UPSIDE: upside_assumptions,
        }[scenario]()

    resolved = resolve(inputs, config, scenario, assumptions)
    projection = compute_projection(
        inputs.opening.available,
        inputs.opening.restricted,
        resolved.flows,
        config,
        inputs.opening.as_of,
    )

    data_quality = list(resolved.notes)
    if not inputs.opening.verified:
        data_quality.insert(0, "opening cash is not yet verified/reconciled")
    if not inputs.invoices:
        data_quality.append("no receivables provided — inflows may be understated")
    missing_hist = _customers_without_history(inputs)
    if missing_hist:
        data_quality.append(
            f"{missing_hist} customer(s) have no payment history; default timing used"
        )

    overall_conf = _overall_confidence(resolved.flows, inputs)
    headline = _headline(projection, scenario)
    action = _recommended_action(projection, resolved.flows, inputs)

    version = ForecastVersion(
        engine_version=config.engine_version,
        assumption_version=config.assumption_version,
        mapping_version=config.mapping_version,
        model_version=config.model_version,
        timezone=config.timezone,
        rounding_policy=config.rounding_policy,
        scenario=scenario,
        input_fingerprint=fingerprint(inputs, config, assumptions, scenario.value),
        status=PublicationStatus.PRELIMINARY,
    )

    return ForecastResult(
        scenario=scenario,
        version=version,
        projection=projection,
        flows=tuple(resolved.flows),
        overall_confidence=overall_conf,
        data_quality=tuple(data_quality),
        headline=headline,
        recommended_action=action,
    )


# --- helpers -----------------------------------------------------------------
def _customers_without_history(inputs: ForecastInputs) -> int:
    known = {h.customer_id for h in inputs.customer_histories if h.observations or h.override_days_late is not None}
    customers = {inv.customer_id for inv in inputs.invoices}
    return len(customers - known)


def _overall_confidence(flows: list[CashFlow], inputs: ForecastInputs) -> int:
    from .enums import CONFIDENCE_WEIGHT

    total = sum(f.amount.minor_units for f in flows)
    if total == 0:
        base = 100
    else:
        base = round(sum(f.amount.minor_units * CONFIDENCE_WEIGHT[f.confidence] for f in flows) / total)
    if not inputs.opening.verified:
        base = min(base, 60)
    return base


def _headline(projection: Projection, scenario: Scenario) -> str:
    ccy = projection.currency
    trough = projection.trough
    label = {Scenario.BASE: "", Scenario.DOWNSIDE: " (downside)", Scenario.UPSIDE: " (upside)"}[scenario]
    if projection.breach.breached and projection.breach.first_date is not None:
        wk = projection.breach.weeks_until
        short = projection.breach.worst_shortfall
        return (
            f"Cash is projected to fall below your {ccy} "
            f"{projection.effective_floor.to_decimal_string()} floor in week {wk} "
            f"(around {projection.breach.first_date.isoformat()}), short by up to "
            f"{ccy} {short.to_decimal_string()}{label}."
        )
    return (
        f"Cash stays above your {ccy} {projection.effective_floor.to_decimal_string()} "
        f"floor for all {len(projection.weeks)} weeks. Low point is {ccy} "
        f"{trough.balance.to_decimal_string()} around {trough.on_date.isoformat()}{label}."
    )


def _recommended_action(
    projection: Projection, flows: list[CashFlow], inputs: ForecastInputs
) -> str | None:
    """The single most useful action, tied to what's driving the risk."""
    if not projection.breach.breached or projection.breach.first_date is None:
        return None
    breach_date = projection.breach.first_date
    # Overdue receipts that land after the breach could be pulled forward.
    pullable = [
        f
        for f in flows
        if f.category is Category.CUSTOMER_RECEIPT
        and f.direction is Direction.INFLOW
        and f.on_date >= breach_date
    ]
    if pullable:
        total = money_sum((f.amount for f in pullable), projection.currency)
        largest = max(pullable, key=lambda f: (f.amount.minor_units, f.on_date))
        return (
            f"Accelerate collection of {len(pullable)} receipt(s) totaling "
            f"{projection.currency} {total.to_decimal_string()} that currently land on or after "
            f"{breach_date.isoformat()} — pulling in the largest (invoice "
            f"{largest.origin_id}, {projection.currency} {largest.amount.to_decimal_string()}) "
            f"would most directly protect the floor."
        )
    return (
        f"Cover the projected shortfall of up to {projection.currency} "
        f"{projection.breach.worst_shortfall.to_decimal_string()} before {breach_date.isoformat()} "
        f"— delay non-critical outflows or arrange a short-term draw."
    )
