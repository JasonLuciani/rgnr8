"""Run base / downside / upside through the same engine and compare them."""

from __future__ import annotations

from dataclasses import dataclass

from .enums import Scenario
from .models import ForecastConfig, ForecastInputs
from .money import Money
from .result import ForecastResult, run_forecast


@dataclass(frozen=True, slots=True)
class ScenarioComparison:
    base: ForecastResult
    downside: ForecastResult
    upside: ForecastResult

    def as_dict(self) -> dict[Scenario, ForecastResult]:
        return {
            Scenario.BASE: self.base,
            Scenario.DOWNSIDE: self.downside,
            Scenario.UPSIDE: self.upside,
        }

    def trough_spread(self) -> tuple[Money, Money]:
        """(downside trough, upside trough) — the liquidity envelope."""
        return self.downside.projection.trough.balance, self.upside.projection.trough.balance

    def any_breach(self) -> bool:
        return any(r.projection.breach.breached for r in self.as_dict().values())

    def summary_lines(self) -> list[str]:
        out: list[str] = []
        for scenario, r in self.as_dict().items():
            p = r.projection
            b = p.breach
            status = (
                f"breaches week {b.weeks_until} (short {p.currency} {b.worst_shortfall.to_decimal_string()})"
                if b.breached
                else "stays above floor"
            )
            out.append(
                f"{scenario.value:<8} trough {p.currency} {p.trough.balance.to_decimal_string()} "
                f"on {p.trough.on_date.isoformat()} — {status}"
            )
        return out


def run_all_scenarios(
    inputs: ForecastInputs, config: ForecastConfig | None = None
) -> ScenarioComparison:
    return ScenarioComparison(
        base=run_forecast(inputs, config, Scenario.BASE),
        downside=run_forecast(inputs, config, Scenario.DOWNSIDE),
        upside=run_forecast(inputs, config, Scenario.UPSIDE),
    )
