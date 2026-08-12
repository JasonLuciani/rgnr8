"""Consume side of the cross-language loop.

    python examples/consume.py <in.json>

Loads the ForecastInputs DTO the TypeScript side emitted, runs the forecast, and
builds + validates the owner briefing — real reconciled data all the way through.
"""

from __future__ import annotations

import sys

from rgnr8_forecast import ForecastConfig, Money, loads, run_forecast
from rgnr8_briefing import build_briefing, render_text, validate_briefing


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/forecast_inputs.json"
    with open(path, encoding="utf-8") as fh:
        inputs = loads(fh.read())

    print(f"Loaded ForecastInputs: opening {inputs.opening.available}, "
          f"{len(inputs.recurring)} recurring flow(s), verified={inputs.opening.verified}")

    forecast = run_forecast(inputs, ForecastConfig(minimum_cash=Money.from_decimal("5000.00")))
    briefing = build_briefing(forecast)
    violations = validate_briefing(briefing, forecast)
    print(f"Unsupported-number validator: {len(violations)} violation(s)\n")
    assert not violations
    print(render_text(briefing))


if __name__ == "__main__":
    main()
