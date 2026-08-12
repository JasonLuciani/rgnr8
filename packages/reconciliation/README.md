# @rgnr8/reconciliation

Ties a bank/card **statement** to the **book activity** (ingested primary-source transactions) for the same account, classifies every difference, and gates owner-facing numbers on a clean reconciliation. This is the hard gate the blueprint requires: no number is shown to an owner for an account until that account reconciles.

TypeScript, depends on `@rgnr8/ledger-kernel` and `@rgnr8/ingestion`. 9 tests, `tsc --strict` clean.

## How it works

`reconcileStatement(statement, bookItems)` matches statement lines to book items — first by provider external id, then greedily by equal amount within a date window — and then reasons about what's left:

- **Matched** items cancel.
- **Unmatched book** items are **TIMING** (in transit): booked but not yet on the statement — an outstanding payment or a deposit in transit. Benign.
- **Unmatched statement** lines are **MISSING_SOURCE**: real bank activity not in the books (e.g., a fee the feed missed). These must be resolved.

It computes the book-vs-statement `difference` and the `unreconciledResidual` (the difference after removing benign timing items). The account is **BALANCED** only when the statement is internally consistent, nothing is missing from the books, and the residual is zero. Difference categories also include `MAPPING`, `POSTING_ERROR`, `LEGACY_ERROR`, and `RGNR8_DEFECT` for richer triage as the classifier grows.

## The gate and sign-off

```ts
const recon = reconcileStatement(statement, bookItemsFromCanonical(pipeline.active(), "chk"));
if (!isCleanForPublish(recon)) {
  // block owner-facing output for this account; surface the differences to resolve
}
const signed = signReconciliation(recon, { owner, reviewer, completedAt }); // throws unless BALANCED
```

`isCleanForPublish` is the boolean the rest of the system checks before showing balances or a forecast built on this account. `signReconciliation` records the owner, reviewer, and completion timestamp — and refuses to sign an out-of-balance account.

## Bridges from ingestion

`bookItemsFromCanonical(pipeline.active(), accountId)` turns ingestion's canonical transactions into reconciliation book items (same "+ = into account" convention; superseded pending rows already excluded upstream). The integration test proves the path: raw feed → ingested → reconciled against a statement → BALANCED, and a bank line the feed missed correctly blocks the gate.

## Run it

```bash
npm test
node --import tsx examples/demo.ts
```

The demo shows an in-transit payment reconciling clean (and getting signed off) and a missing bank fee blocking publish.

## Layout

```
src/
  types.ts       Statement, StatementLine, BookItem, MatchPair, ClassifiedItem,
                 DifferenceCategory, Reconciliation, SignOff
  match.ts       external-id then amount+date matching
  reconcile.ts   reconcileStatement, isCleanForPublish, signReconciliation
  bridge.ts      bookItemsFromCanonical (ingestion → book items)
test/            unit (balanced, timing, missing-source, matching, gate, sign-off)
                 + integration (ingest → reconcile)
```

## What's next

- Feed the reconciliation gate into the forecast/briefing: only accounts with a clean, signed reconciliation contribute owner-facing numbers.
- AR/AP subledger-to-control reconciliations (tie customer/vendor subledgers to GL control accounts).
- Richer auto-classification (mapping vs posting-error vs legacy) and suggested adjusting entries via the kernel.
- Statement import adapters (OFX/CSV/provider statement endpoints).
