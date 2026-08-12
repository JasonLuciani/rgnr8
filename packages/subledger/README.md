# @rgnr8/subledger

AR and AP subledgers: invoices and bills with open balances, partial-payment application, aging, control-account reconciliation, customer payment histories, and forecast-input emission. This is what upgrades the forecast from *detected recurring flows* to **invoice-level receivable timing** — and unlocks "which customer is creating the risk" answers.

TypeScript, depends only on `@rgnr8/ledger-kernel`. 11 tests, `tsc --strict` clean.

## Accounts receivable (`ARSubledger`)

- `addInvoice`, `applyPayment` (supports partials; overpayment clamps to zero), `writeOff`.
- `openInvoices()`, `controlBalance()` (sum of open receivables — ties to the GL AR control account), `aging(asOf)` (Current / 1-30 / 31-60 / 61-90 / 90+).
- `customerHistories()` — `(due, paid)` observations from **fully-settled** invoices, grouped by customer. This is exactly what the forecast's payment-timing model learns from.

## Accounts payable (`APSubledger`)

Symmetric: `addBill`, `applyPayment`, `openBills()`, `controlBalance()` (ties to GL AP control), `aging(asOf)`.

## Control reconciliation

`reconcileControl(name, subledgerControl, glControl)` ties a subledger's open total to its GL control-account balance. A nonzero difference means the subledger and the ledger have drifted — the same clean/dirty discipline the bank reconciliation uses, applied to receivables and payables.

## Forecast inputs

`subledgerForecastParts(ar, ap)` emits the DTO fragments the forecast consumes, in the `forecast-inputs/1` contract shape:

- **open invoices** (id, customer, issue/due dates, remaining `open_amount`, status) — so the forecast times each receivable by its due date and the customer's predicted lateness;
- **customer payment histories** — so the timing model is learned from real behavior, not a default;
- **open bills** — scheduled cash outflows.

These merge straight into `@rgnr8/forecast-inputs` via `buildForecastInputs({ …, subledger: parts })`. See `packages/forecast-inputs/examples/emit_with_ar.ts` → `consume_rich.py` for the full loop: reconciled bank + AR/AP → forecast → briefing, where "largest upcoming receipt" and the ask-your-CFO "which receivable matters most" are now actual invoices.

## Layout

```
src/
  types.ts          AR/AP inputs + live states, statuses, aging, histories
  ar.ts             ARSubledger (open balances, aging, control, histories)
  ap.ts             APSubledger (open balances, aging, control)
  aging.ts          shared aging buckets by days-past-due
  control.ts        reconcileControl (subledger vs GL control account)
  forecastInputs.ts subledgerForecastParts -> forecast-inputs/1 fragments
test/               AR (payments, aging, histories, write-off), AP + control + parts
```

## Cash auto-application

`applyReceiptsToAR(ar, receipts, aliases)` / `applyPaymentsToAP(ap, payments, aliases)` match ingested bank receipts/payments to open documents and apply them — **deterministically**: first by remittance reference (the invoice/bill id in the description), then by counterparty + an exact single open-amount match. Ambiguous or unmatched items are returned for manual review, never guessed. This keeps the subledger current straight from the reconciled feed, so the whole loop runs without manual payment entry.

## Post to the general ledger

The subledgers emit an event log (`ar.events()` / `ap.events()`). `toARPostingCommands` / `toAPPostingCommands` map those to balanced kernel journals — INVOICE → Dr AR control / Cr Revenue, PAYMENT → Dr Cash / Cr AR control, WRITEOFF → Dr Bad debt / Cr AR control (and the AP mirror). Post them through the kernel and the **GL control accounts tie to the subledger automatically** (integration test asserts GL AR control == `ar.controlBalance()`); posting is idempotent on the event id.

## What's next

- Credit memos, deposits/prepayments, and multi-invoice payment application.
