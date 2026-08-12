# @rgnr8/forecast-inputs

Emits the Python forecast engine's `ForecastInputs` from **reconciled** ingestion data — closing the loop from primary-source bank/card activity to the owner-facing cash forecast. This is the boundary between the TypeScript app services (ingestion, reconciliation) and the Python forecast/briefing.

Depends on `@rgnr8/ledger-kernel`, `@rgnr8/ingestion`, `@rgnr8/reconciliation`. 9 tests, `tsc --strict` clean.

## The contract

A small JSON contract (`forecast-inputs/1`) mirrors the Python `rgnr8_forecast.io` (de)serializer exactly. Money is integer minor units (`{"minor": N, "currency": "USD"}`), dates are ISO. The Python side loads it with `rgnr8_forecast.loads(text)`; the round-trip preserves the forecast's version fingerprint.

## What it does

`buildForecastInputs({ reconciliations, transactions })`:

- **Gates opening cash on reconciliation.** Only accounts whose reconciliation is BALANCED (`isCleanForPublish`) contribute their reconciled closing balance to opening cash. Any out-of-balance account is **excluded and reported**, and the opening position is marked `verified: false`. No unreconciled number reaches the forecast.
- **Detects recurring flows** from the reconciled transactions (`detectRecurring`): groups by counterparty/sign, and emits a recurring item only when the occurrences are frequent enough, the gaps are consistent and match a known cadence (weekly / biweekly / monthly / quarterly / annual), and the amounts are stable. Anchored at the most recent occurrence so the forecast projects it forward. Internal transfers are ignored.

The result is the `ForecastInputsDTO` plus the list of excluded accounts and notes.

## The loop, end to end

`examples/emit.ts` ingests a bank feed, reconciles the current month, and writes the DTO as JSON. `examples/consume.py` loads it, runs the forecast, and builds + validates the owner briefing:

```bash
node --import tsx examples/emit.ts /tmp/fi.json
PYTHONPATH="../forecast/src:../briefing/src" python examples/consume.py /tmp/fi.json
```

Real reconciled activity → opening cash + detected recurring flows → 13-week forecast → validated weekly briefing, with the reconciliation gate enforced at the boundary.

## Layout

```
src/
  dto.ts        the forecast-inputs/1 contract (mirrors Python rgnr8_forecast.io)
  recurring.ts  detectRecurring — cadence + amount-stability inference
  build.ts      buildForecastInputs (reconciliation-gated) + toJson
test/           recurrence detection + gated build
examples/       emit.ts (TS) + consume.py (Python) — the cross-language loop
```

## What's next / limits

- v1 emits opening cash + recurring flows. **AR invoices, customer payment histories, and open bills** need the AR/AP subledgers (not yet built) — until then the forecast's inflows come from detected recurring receipts, not invoice-level timing. This is the honest Phase 0 shape (on top of existing books).
- Provenance is not carried in the contract (it stays with the ledger); loaded records have no provenance.
