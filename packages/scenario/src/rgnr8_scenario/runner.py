"""Run a scenario end to end: baseline, adjusted, and the diff between them.

The forecast engine is injected as a ``ForecastRunner`` (default
``rgnr8_forecast.run_forecast``) so tests can substitute a recording or stubbed
engine — the same Protocol-seam pattern used across RGNR8. Nothing here reads a
clock or randomizes: given the same inputs, config, and scenario, the result is
identical every call.
"""

from __future__ import annotations

from typing import Protocol

from rgnr8_forecast import (
    ForecastConfig,
    ForecastInputs,
    ForecastResult,
    run_forecast,
)

from .adjustments import Scenario, apply, config_override
from .compare import ScenarioDiff, compare


class ForecastRunner(Protocol):
    """The seam onto the forecast engine: inputs + config -> result."""

    def __call__(
        self, inputs: ForecastInputs, config: ForecastConfig
    ) -> ForecastResult: ...


def run_scenario(
    inputs: ForecastInputs,
    config: ForecastConfig,
    scenario: Scenario,
    *,
    run: ForecastRunner = run_forecast,
) -> tuple[ForecastResult, ScenarioDiff]:
    """Apply ``scenario``, re-run baseline and adjusted forecasts, and diff.

    Returns ``(scenario_result, diff)`` where ``diff`` is the signed change of the
    scenario relative to the untouched baseline.
    """
    base_result = run(inputs, config)
    scenario_inputs = apply(inputs, scenario)
    scenario_config = config_override(scenario, config)
    scenario_result = run(scenario_inputs, scenario_config)
    diff = compare(base_result, scenario_result)
    return scenario_result, diff


__all__ = ["ForecastRunner", "run_scenario"]
