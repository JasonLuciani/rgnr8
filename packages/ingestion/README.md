# @rgnr8/ingestion

Turns raw bank / card / payroll payloads into **canonical transactions** — normalized, deduplicated, classified, provenance-tagged — and maps them to balanced ledger posting commands. This is the layer that feeds both the accounting kernel and the forecast from primary sources.

TypeScript, depends only on `@rgnr8/ledger-kernel`. 10 tests, `tsc --strict` clean.

## What it does

1. **Archives raw payloads** append-only (`RawArchive`) — the source of truth for replay and audit; raw records are never mutated.
2. **Normalizes** via pluggable `ProviderAdapter`s. Included: a **Plaid-like bank adapter** (which flips the provider's sign convention — positive = money *out* — to RGNR8's positive = money *in*) and a **Gusto-like payroll adapter** (which splits a run into net pay on payday and taxes on the settlement date).
3. **Deduplicates** on a stable key (provider external id, or a content hash when none) — re-ingesting the same feed adds nothing (idempotent).
4. **Resolves pending → posted**: a posted record supersedes the pending one it replaces (provider-driven via `pending_transaction_id`), so amounts aren't double-counted.
5. **Detects internal transfers**: an equal-and-opposite pair across two own-accounts within a window is flagged on both sides and excluded from net cash (no double counting).
6. **Classifies** each transaction (`DEPOSIT`, `PURCHASE`, `FEE`, `INTEREST`, `REFUND`, `PAYROLL_NET`, `PAYROLL_TAX`, `TRANSFER`, `OTHER`).
7. **Maps to the ledger**: `toPostingCommands` produces balanced two-line journals (cash + a counter account chosen by kind/direction) as kernel `PostCommand`s, with the idempotency key derived from the transaction id — so posting is safe to repeat.

## End-to-end guarantee (tested)

The integration test ingests the fixtures, maps the active transactions to journals, posts them through the kernel's `PostingEngine`, and asserts the **trial balance ties** and the cash account nets correctly (−15,445.49 for the sample). Re-posting is idempotent. See `examples/demo.ts` for the printed run.

## Use it

```ts
const pipeline = new IngestionPipeline()
  .register(new BankPlaidLikeAdapter(tenantId))
  .register(new PayrollGustoLikeAdapter(tenantId));

pipeline.ingest(rawRecords);                 // idempotent; returns a report
const txns = pipeline.active();              // canonical, transfers flagged, pending resolved

const { commands } = toPostingCommands(txns, defaultAccountMap());
for (const cmd of commands) await engine.post(cmd, { postedAt });  // -> balanced ledger
```

```bash
npm test
node --import tsx examples/demo.ts
```

## Layout

```
src/
  types.ts        RawRecord, NormalizedInput, CanonicalTransaction, enums
  archive.ts      append-only raw-payload archive
  dedupe.ts       stable dedupe key + direction
  transfers.ts    internal-transfer detection
  adapters/       bankPlaidLike, payrollGustoLike
  pipeline.ts     IngestionPipeline (archive, dedupe, supersede, transfers, classify)
  mapping.ts      canonical -> balanced kernel PostCommands
fixtures/         plaid_transactions.json, gusto_payroll.json
test/             pipeline + mapping-integration (raw -> ledger, trial balance ties)
```

## What's next

- More adapters (cards, additional banks/payroll providers) and a connector-sync/token-health layer.
- Emit **forecast inputs** directly: derive opening cash from balances, `CustomerHistory` from AR payment observations, and recurring detection — so the forecast is fed from reconciled primary-source data (Phase 2).
- Balance reconciliation: tie ingested activity to statement balances before anything is shown to an owner.
