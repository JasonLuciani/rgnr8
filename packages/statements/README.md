# @rgnr8/statements

Generates the **income statement** and **balance sheet** directly from the ledger's posted entries — the accountant-facing view. Because every journal in the kernel is balanced, the balance sheet **balances by construction** (Assets = Liabilities + Equity + current earnings); the tests assert it.

TypeScript, depends only on `@rgnr8/ledger-kernel`. 4 tests, `tsc --strict` clean.

## What it does

- `computeIncomeStatement(store, tenant, coa, periodStart, asOf, currency)` — sums revenue and expense accounts for entries dated within the period; nets to income.
- `computeBalanceSheet(store, tenant, coa, asOf, currency)` — assets, liabilities, equity as of a date, with current earnings (revenue − expenses to date) folded onto the equity side. Reports `balances: true` when assets equal liabilities + equity + earnings.
- `renderIncomeStatement` / `renderBalanceSheet` — plain-text renderers.

Statements are derived from the same ledger facts as the trial balance (no separate running totals), so everything ties. Accounts are grouped by their `AccountType`; a chart-of-accounts with a grouping/subtotal structure is the natural next extension.

## Use it

```ts
const is = await computeIncomeStatement(store, tenant, coa, "2026-08-01", "2026-08-31", USD);
const bs = await computeBalanceSheet(store, tenant, coa, "2026-08-31", USD);
console.log(bs.balances); // true — balanced by construction
```

```bash
npm test
```

## What's next

- Chart-of-accounts grouping/subtotals (current vs. non-current, COGS vs. opex) and comparative periods.
- Cash-flow statement (indirect method) and cash vs. accrual basis presentation.
- Tie the statements into close-management (lock the period, publish the financial package).
