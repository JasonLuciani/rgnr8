# rgnr8-briefing

The RGNR8 **weekly owner briefing** — the customer-facing surface of Phase 0. It turns a `ForecastResult` into a calm, owner-language summary of the next 13 weeks of cash, and it enforces the blueprint's hardest trust rule: **no number ships unless it recomputes from the forecast.**

Pure Python, depends only on `rgnr8-forecast`. `mypy --strict` clean, 48 tests.

## What it produces

- **A status** — `STABLE` / `WATCH` / `AT_RISK` — with a plain-language reason.
- **The headline and the one action** carried up from the forecast.
- **Evidence-backed facts**: cash today, the minimum-cash floor, the projected low point and its date, the cushion, money in vs. out, the largest projected shortfall, and the biggest upcoming receipt/outflow.
- **Drivers** — the categories draining cash, with each total's share of outflows.
- **A 13-week glance** — closing balance and per-week status.
- **Data-quality heads-up** and an overall confidence score.

Render it as owner-ready **plain text** (`render_text`), a **self-contained HTML** card (`render_html`), or the full **owner "Today" dashboard** (`render_today_html`) — a status hero, KPI tiles, an interactive 13-week cash chart (single-hue line with the minimum-cash floor and the trough marked), driver bars, and a clickable ask-your-CFO panel with answers embedded. No external assets; colors follow the validated data-viz palette (status uses the reserved status palette with an icon + label, never color alone). See `examples/today_demo.py`.

## The unsupported-number validator (the trust mechanism)

Every `Fact` and `Driver` carries `Evidence` pointing back into the forecast — either a named projection field (`trough_balance`, `effective_floor`, `cushion_at_trough`, …) or the set of cash-flow sequence numbers whose magnitudes sum to the stated amount. `validate_briefing(briefing, forecast)` recomputes each one; a mismatch is a `Violation`. **A briefing with any violation must not be published.** This is what lets the product make claims to an owner without a human re-checking every figure.

```python
from rgnr8_briefing import build_briefing, validate_briefing, render_text
briefing = build_briefing(forecast)
assert validate_briefing(briefing, forecast) == []   # every number is backed
print(render_text(briefing))
```

Tests prove the validator catches a tampered fact amount, a driver total that doesn't match its flows, and a reference to a non-existent flow.

## Ask-your-CFO (constrained answerer)

A fixed set of high-value cash questions (`Question`), each answered **deterministically from the forecast** with evidence-backed facts that pass the same validator. Free text is routed by keyword to a supported question (a stand-in for upstream model interpretation — "AI interprets, deterministic services calculate"). Anything unsupported is declined with a clear limit and the list of questions that *can* be answered — never an empty chat box.

```python
from rgnr8_briefing import ask, suggested_questions
ask(forecast, "am I going to run short?").answer_text
# "Yes — cash is projected to dip below your USD 23000.00 floor around 2026-09-04 ..."
suggested_questions()  # the 6 supported prompts to show instead of a blank box
```

## Layout

```
src/rgnr8_briefing/
  models.py     StatusLevel, Evidence, Fact, Driver, WeekGlance, WeeklyBriefing, Violation
  build.py      build_briefing(forecast) -> WeeklyBriefing
  validate.py   resolve_field + validate_briefing (the unsupported-number validator)
  answer.py     constrained ask-your-CFO answerer + routing + suggestions
  render.py     render_text / render_html (self-contained)
  variance.py   compute_variance vs. a published forecast + render
  delivery.py   DeliveryEnvelope, channels/priority, deep links, Deliverer seam
  providers.py  real email/push providers over an EmailTransport/PushTransport seam + fakes
  scheduler.py  deterministic weekly cadence: is_due / next_fire / run_due over Subscriptions
tests/          41 tests (build, validate incl. tamper, answer, render, variance, delivery, providers, scheduler)
examples/demo.py  end-to-end: build -> validate -> print -> write HTML -> Q&A
```

## Variance vs. the last published forecast

`compute_variance(published_forecast, actuals)` holds a published forecast's frozen weekly closings against what actually happened — per-week deltas, mean-absolute-percent-error, within-tolerance, and a narrative. The basis for "did the action change the outcome" and the forecast-accuracy case study.

## Delivery

`build_envelope(briefing, recipient, tenant_id, variance=…)` produces a channel-ready `DeliveryEnvelope`: status-aware subject, priority, channels, text + HTML bodies, and deep links to a single resolvable task. Push is reserved for urgent cases — an `AT_RISK` briefing or **material worsening** vs. the last published forecast. A `Deliverer` protocol is the send seam (`RecordingDeliverer` for tests; real email/push providers implement the same `send`); `Schedule` carries the recurring cadence.

`ProviderDeliverer` (in `providers.py`) is the real send: it routes an envelope across its channels via injected `EmailTransport` / `PushTransport` — the wire call sits behind that seam exactly like the connectors' `HttpClient`, so routing, partial-failure, and receipts are fully testable with `FakeEmailTransport`/`FakePushTransport` and going live is just supplying a transport over SendGrid/SES/APNs/FCM.

The production transports ship in `http_providers.py`: **`HttpEmailTransport`** (formats a SendGrid v3 `mail/send` payload — `personalizations`/`from`/`subject`/plain+html `content` — with a bearer key) and **`HttpPushTransport`** (a generic FCM/Expo-style body), both posting over an injected **`HttpClient`** (`post_json`). A non-2xx response raises `TransportError` (so `ProviderDeliverer` reports it as PARTIAL/FAILED); the provider message id comes from the `X-Message-Id` header / response body. `UrllibHttpClient` is the stdlib production client; `FakeHttpClient` scripts status/headers/body (and connection failures) so the entire payload-building + auth + error path is unit-tested with no network — going live is just a real client + credentials. A push with no configured push transport degrades to `PARTIAL` (email still sent) rather than crashing; the receipt's `channels` are the ones that actually succeeded.

`scheduler.py` consumes `Schedule` deterministically — `now` is always injected (never the system clock). `most_recent_fire`/`next_fire` compute the cadence *in the schedule's timezone* (DST-correct via `zoneinfo`); `is_due(schedule, now, last_sent)` uses catch-up semantics so a missed tick still sends once. `run_due(subscriptions, now, envelope_for, deliverer)` sends to every due `Subscription`, advances each fired sub's `last_sent` to the fire time it satisfied (so re-running is idempotent), and skips a tenant whose envelope isn't available (e.g. its numbers didn't reconcile) without advancing — it retries next run.

## Run it

```bash
# from the monorepo (forecast is a sibling package)
cd packages/briefing
PYTHONPATH="src:../forecast/src" python -m pytest
PYTHONPATH="src:../forecast/src" python examples/demo.py   # writes briefing.html
```

## What's next

- Delivery providers ✅ (email/push over a transport seam, with **real HTTP-backed SendGrid/FCM transports**) and a deterministic weekly scheduler ✅ are built; the delivery **runtime** (`rgnr8-runtime`) drives `run_due` on a real clock with a durable subscription store — going live is just real credentials.
- Wire to real data: with ingestion + reconciliation + connectors landed, the forecast inputs come from the ledger and this briefing is the live weekly product.
- Variance section ✅: compares this week's actuals to the last **published** forecast (the forecast engine versions and freezes published runs).
