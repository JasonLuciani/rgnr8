"""Consume the AR-enriched DTO and show the invoice-level briefing + Q&A."""

from __future__ import annotations

import sys

from rgnr8_forecast import ForecastConfig, Money, loads, run_forecast
from rgnr8_briefing import Question, answer, build_briefing, render_text, validate_briefing


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/forecast_inputs_ar.json"
    with open(path, encoding="utf-8") as fh:
        inputs = loads(fh.read())

    print(
        f"Loaded: opening {inputs.opening.available}, "
        f"{len(inputs.invoices)} open invoice(s), "
        f"{len(inputs.customer_histories)} customer history(ies), "
        f"{len(inputs.bills)} open bill(s)\n"
    )

    fc = run_forecast(inputs, ForecastConfig(minimum_cash=Money.from_decimal("10000.00")))
    briefing = build_briefing(fc)
    assert not validate_briefing(briefing, fc)
    print(render_text(briefing))

    print("\n--- Ask your CFO ---")
    for q in (Question.WILL_I_BREACH, Question.TOP_RECEIVABLE, Question.BIGGEST_COST):
        print("Q:", q.value)
        print("A:", answer(fc, q).answer_text, "\n")


if __name__ == "__main__":
    main()
