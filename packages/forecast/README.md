# rgnr8-forecast

The RGNR8 **direct-method 13-week cash forecast engine**. Given a business's verified cash position and its receivables, payables, payroll, debt, recurring items, and plans, it projects the daily liquidity path, finds the cash trough, and flags when the minimum-cash floor is breached — with every projected number traceable to its source.

Pure Python, **no runtime dependencies**, `mypy --strict` clean, 52 tests, ~97% coverage.

## Why it's built this way

This is the first customer-facing value in the product, and in this category trust is everything. So the engine follows the same discipline as the accounting kernel:

- **Direct method.** It begins with verified available cash and walks *dated* inflows and outflows. It does **not** derive cash from a forecasted income statement.
- **Exact money.** Integer minor units throughout — never floating point.
- **Deterministic.** No wall clock and no randomness anywhere. Every date is an input, so the same inputs always produce the same forecast (and the same version fingerprint).
- **Explainable.** The atomic unit is a `CashFlow` carrying a plain-language `basis` and provenance back to the invoice/bill/plan that produced it. Any figure in any week can be drilled to the flows that compose it — the product's progressive-disclosure and answer-validator requirements, met at the engine level.
- **Rules-first, prediction where it earns its place.** Recorded commitments are deterministic; statistical prediction is applied narrowly to customer payment timing, and always with an explanation.

## The forecast hierarchy (confidence tiers)

| Tier | Meaning | Examples |
|---|---|---|
| `RECORDED` | Known commitment, dated and firm | payroll, debt service, rent, bills |
| `PREDICTED` | Behavioral estimate | customer pays N days after due |
| `PLANNED` | Explicit owner plan | a hire, a purchase, a distribution |
| `SCENARIO` | Exists only in a stress/upside case | pipeline, bad-debt/refund reserves |

Weekly and overall confidence are amount-weighted rollups of these tiers.

## Treatment policies implemented

Every policy the blueprint calls for is handled explicitly in `resolve.py`:

- **Transfers / internal movements excluded** (no double counting).
- **Pending vs posted** — opening cash is posted; pending items arrive as one-time flows.
- **Partial invoices** — only the open amount is forecast.
- **Payroll gross/net/liability timing** — net pay on payday, payroll taxes remitted after a configurable lag.
- **Debt principal/interest** — one cash outflow per payment; the split is retained as metadata, never double-counted.
- **Tax reserves, annual & irregular expenses** — recurring or one-time items.
- **Refunds/chargebacks** — a scenario reserve tied to projected receipts.
- **Bad debt / disputed** — disputed AR excluded; aged AR written down under stress.
- **Customer-specific overrides** — a per-customer payment-timing override wins over learned behavior.
- **Minimum cash & restricted cash** — restricted cash raises the effective floor used for breach detection.

## Payment-timing prediction

`predict.py` learns a customer's typical days-late from their `(due, paid)` history using the **median** (robust to the occasional very-late invoice). Confidence falls out of sample size and dispersion. With too little history it falls back to a configurable default; an explicit override always wins. Fully deterministic and explained in the flow's `basis`.

## Scenarios

Base / downside / upside run through the **same engine** and differ only by an explicit `ScenarioAssumptions` object (AR stress delay, bad-debt haircut on aged AR, refund reserve, on-time collection, pipeline inclusion). `run_all_scenarios` returns the envelope and a comparison.

## Reproducibility & publication

Each forecast carries a `ForecastVersion`: engine / assumption / mapping / prediction-model versions, timezone, rounding policy, and a **SHA-256 fingerprint of the exact inputs + config + scenario**. Same inputs → same `version_id`. Results move through `PRELIMINARY → VERIFIED → PUBLISHED` immutably (status changes produce a new version object; the forecast numbers and fingerprint never silently change).

## Use it

```bash
python -m pip install -e ".[dev]"     # or just add src/ to PYTHONPATH
python -m pytest                       # 52 tests
python examples/demo.py                # realistic 13-week run, printed
```

```python
from datetime import date
from rgnr8_forecast import (
    CashPosition, Invoice, CustomerHistory, ForecastInputs, ForecastConfig,
    Money, run_forecast, run_all_scenarios,
)

inputs = ForecastInputs(
    opening=CashPosition(as_of=date(2026, 8, 3), available=Money.from_decimal("68000.00")),
    invoices=(Invoice("INV-1", "acme", date(2026, 8, 1), date(2026, 8, 20), Money.from_decimal("15000.00")),),
    customer_histories=(CustomerHistory("acme", override_days_late=6),),
)
result = run_forecast(inputs, ForecastConfig(minimum_cash=Money.from_decimal("15000.00")))
print(result.headline)                 # owner-facing one-liner
print(result.recommended_action)       # the single action, if at risk
for w in result.projection.weeks:      # 13 weekly buckets
    print(w.index, w.closing.to_decimal_string(), w.confidence)
```

## Layout

```
src/rgnr8_forecast/
  money.py            integer-minor-unit Money
  dates.py            week bucketing + recurrence (no month-end drift)
  enums.py            Direction, Confidence, Category, Scenario, status
  models.py           inputs, config, provenance, scenario assumptions
  predict.py          deterministic payment-timing prediction
  resolve.py          treatment policies -> list[CashFlow]
  projection.py       direct-method daily/weekly path, trough, breach
  scenarios.py        base/downside/upside + comparison
  reproducibility.py  fingerprint + ForecastVersion
  result.py           ForecastResult, headline, action, run_forecast()
tests/                52 tests across every module
examples/demo.py      runnable end-to-end example
```

## What's next

- Wire the forecast to the ledger: feed `RECORDED` commitments and opening cash from `@rgnr8/ledger-postgres` reconciled data (Phase 2), replacing hand-built inputs.
- Backtesting harness: compare published forecasts to actuals for the forecast-accuracy case study.
- Expose the result via an API for the weekly briefing (the next customer-facing surface).
