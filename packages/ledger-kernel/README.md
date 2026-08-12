# @rgnr8/ledger-kernel

The RGNR8 accounting kernel: exact money primitives and a **balanced-by-construction, append-only double-entry posting engine**. This is Release 0 of the build plan and the foundation the rest of the platform is built on — the piece the execution plan flags as the one whose invariants must exist from commit one so that everything posted later is ledger-compatible.

It has **zero runtime dependencies** and no I/O. Persistence is an interface (`LedgerStore`); a PostgreSQL adapter slots in behind the same contract later without touching the engine.

## Why this exists first

In an owner-facing financial product, being wrong once destroys trust. So the money math and the ledger are deterministic services with hard invariants — never AI, never floating point, never a silent edit. AI (later) may *propose* a treatment; only this engine can validate balance, currency, period, and authorization and actually post it.

## Invariants enforced

- **Integer money.** Values are a `bigint` of minor units (cents). No binary floating point, ever. `0.10 + 0.20 === 0.30` exactly.
- **Balanced by construction.** The only path to a posted entry validates that debits equal credits, every account exists, currencies match, and amounts are positive (direction is carried by the line's side, not a signed amount). There is no way to post an unbalanced entry.
- **Append-only / immutable.** Posted entries are deeply frozen and never updated or deleted. Corrections happen through a linked **reversal** (sides swapped, `reversalOf` set) — the original is never mutated.
- **Idempotent.** Every command carries an idempotency key. Replaying a key returns the original entry and appends nothing; reusing a key with a *different* payload is rejected.
- **Period locks in the engine.** Posting into a closed period throws — enforced in the posting service, not just the UI.
- **Provenance on every fact.** Source system, object, version, and effective/posted/ingested timestamps travel with each entry.
- **Deterministic.** No wall clock or randomness in-core; timestamps, ids, and keys are supplied by the caller, so the same command stream always reproduces the same ids, sequences, and balances.

## Layout

```
src/
  money.ts          Money value object (integer minor units) + Currency
  types.ts          Accounts, provenance, commands, posted-entry shapes (branded ids)
  chartOfAccounts.ts Immutable account registry
  periods.ts        Period open/closed registry (lock enforcement)
  journal.ts        validateAndBuildLines — the balanced-by-construction gate
  ledgerStore.ts    LedgerStore interface + InMemoryLedgerStore (append-only)
  postingEngine.ts  PostingEngine — the single typed command API (post, reverse)
  trialBalance.ts   Trial balance + account balances, derived from ledger facts
  errors.ts         Typed domain errors
test/               31 tests — invariants, posting, reversal, idempotency, periods, reproducibility
examples/demo.ts    Runnable end-to-end example
```

## Use it

```bash
npm install
npm run typecheck     # tsc strict, exactOptionalPropertyTypes, noUncheckedIndexedAccess
npm test              # 31 tests, node:test
node --import tsx examples/demo.ts
```

## Example

```ts
import {
  AccountType, ChartOfAccounts, InMemoryLedgerStore, Money, PeriodRegistry,
  PostingEngine, USD, asAccountId, asIdempotencyKey, asPeriodKey, asTenantId,
  computeTrialBalance,
} from "@rgnr8/ledger-kernel";

const coa = new ChartOfAccounts([
  { id: asAccountId("cash"), code: "1000", name: "Cash", type: AccountType.ASSET, currency: USD },
  { id: asAccountId("rev"),  code: "4000", name: "Revenue", type: AccountType.REVENUE, currency: USD },
]);
const engine = new PostingEngine(coa, new InMemoryLedgerStore(), new PeriodRegistry());

engine.post({
  tenantId: asTenantId("acme"),
  idempotencyKey: asIdempotencyKey("sale-1"),
  periodKey: asPeriodKey("2026-08"),
  currency: USD,
  entryDate: "2026-08-15",
  provenance: { /* source system, versions, dates … */ } as any,
  lines: [
    { accountId: asAccountId("cash"), side: "DEBIT",  amount: Money.fromDecimal("100.00", USD) },
    { accountId: asAccountId("rev"),  side: "CREDIT", amount: Money.fromDecimal("100.00", USD) },
  ],
}, { postedAt: "2026-08-15T12:00:00Z" });
```

## Test coverage

31 tests passing; ~98% line coverage on `src`. Covered behaviors include: exact money arithmetic and parsing, unbalanced/empty/unknown-account/currency-mismatch rejection, idempotent replay and divergent-key rejection, period-lock enforcement, deep immutability, reversal (non-destructive, nets to zero), trial-balance tie-out, and full reproducibility.

## What's next (per the build plan)

- **PostgreSQL `LedgerStore` adapter** — same interface, real persistence with tenant_id + row-level security.
- **Chart-of-accounts templates** for the first subvertical, with journal templates for common events (sale, collection, bill, payment, payroll, transfer, loan).
- **Reconciliation module** (Phase 1) — bank/card matching against ledger activity.
- **Ingestion + normalization** (Phase 0/1) — primary-source feeds producing proposed entries this engine validates and posts.
- **Statements** — P&L and balance sheet grouped from `accountBalances`, plus cash/accrual basis handling (Phase 2).
