# @rgnr8/qbo-migrate

QuickBooks migration + **parallel-close compare**. Import a QuickBooks Online export into the RGNR8 ledger, then diff RGNR8's ledger-derived trial balance against QuickBooks' *reported* trial balance — account by account, to the penny. This is the wedge from "cash-intelligence tool" to **system of record**: prove the migration preserved the books, then run the same diff every period as a gate.

TypeScript, depends on `@rgnr8/ledger-kernel` + `@rgnr8/statements`. 37 tests, `tsc --strict` clean.

## The export shape

Real QBO exports arrive as CSV/IIF/QBXML or via the API in several report shapes; adapters map those into one normalized `QboExport` DTO (`accounts`, general-journal `entries`, and an optional `reportedTrialBalance`). Amounts are decimal strings — never floats — parsed into integer-minor-unit `Money` at the boundary.

## CSV adapters (real QBO exports)

`parseQboCsvExport({ accountsCsv, journalCsv, trialBalanceCsv })` builds a `QboExport` directly from the three CSV reports QuickBooks Online exports:

- **Chart of Accounts** (`parseAccountsCsv`) — name, account number, and QBO's verbose type strings normalized (`normalizeQboAccountType`: "Accounts receivable (A/R)" → `Accounts Receivable`, "Expenses" → `Expense`, …).
- **Journal report** (`parseJournalCsv`) — the debit/credit-by-line report, grouped into transactions: a non-empty Date starts a new entry, blank-Date lines continue it; TOTAL rows are dropped.
- **Trial Balance** (`parseTrialBalanceCsv`) — account + debit/credit, TOTAL row skipped.

A dependency-free RFC-4180 `parseCsv` (quoted commas, escaped quotes, embedded newlines) and `parseAmount` ("$1,234.56", "(50.00)" → "-50.00") handle the messy reality of exported cells. A test parses a full three-file export, imports it, and confirms the ledger ties to QBO's reported trial balance.

## Import

- `buildChartFromQbo(accounts, currency)` → a kernel `ChartOfAccounts`. QBO account types collapse onto the kernel's five (`mapQboType`: Bank/AR/Fixed Asset → ASSET, AP/Credit Card/LT Liability → LIABILITY, Income → REVENUE, COGS/Expense → EXPENSE, Equity → EQUITY). The ledger `code` is the QBO account number (or a stable slug); the id is `qbo:<code>` — deterministic, same export in → same ids out.
- `toPostCommands(export, chart, opts)` → **balanced** kernel `PostCommand`s. Pure and total: an entry that references an unknown account, doesn't balance, or carries a malformed amount is **dropped to an `issue`** rather than throwing, so one bad row can't lose the whole migration. Provenance stamps `sourceSystem: "quickbooks"` + a mapping version; the idempotency key is `qbo:<txnId>`.
- `importQbo(engine, export, chart, opts, postedAt)` posts them via the kernel `PostingEngine` and returns an `ImportReport` (`posted`, `skipped`, `issues`, `periods`). **Idempotent**: re-importing the same export replays the same keys and posts nothing new (proven in-test).

## Parallel-close compare

`compareTrialBalances(rgnr8TrialBalance, qboReportedRows, { currency, toleranceMinor? })` matches accounts by name and compares each as a signed net-debit position in **integer minor units** (no float drift). Each row is `match` / `mismatch` / `only_rgnr8` / `only_qbo`; the report carries `inAgreement`, `totalAbsDelta`, and the buckets. Tolerance is in minor units and defaults to `0` (exact). `renderDiffHtml(report)` produces a self-contained side-by-side page (validated palette) — green when the ledgers tie out, otherwise the mismatches and one-sided accounts.

### Statement-level compare

Beyond the account-by-account trial balance, `compareStatements(rgnr8Income, rgnr8Balance, qboReportedTotals, opts)` compares the **statements an owner actually reads** — `compareIncomeStatement` (revenue, expenses, net income) and `compareBalanceSheet` (total assets, total liabilities + equity, net income) — against QBO's reported totals, again in minor units with a tolerance. `renderStatementsDiffHtml` renders both. This is the controller-facing tie-out: not just "every account matches" but "the P&L and balance sheet match."

```ts
const chart = buildChartFromQbo(exp.accounts, USD);
const engine = new PostingEngine(chart.coa, store, new PeriodRegistry());
const imp = await importQbo(engine, exp, chart, { tenantId, currency: USD, ingestedAt }, postedAt);
const tb = await computeTrialBalance(store, tenantId, chart.coa, USD);
const diff = compareTrialBalances(tb, exp.reportedTrialBalance!, { currency: USD });
// diff.inAgreement === true  → RGNR8 ties to QuickBooks
```

## QuickBooks Online API adapter

`QboApiClient` pulls the same `QboExport` straight from Intuit's REST API instead of a file, over an injected `QboHttpClient` (`get(url, headers)` — wrap `fetch`), so request-building and report-parsing are unit-tested against fixture JSON with no network. `fetchAccounts()` (chart, via the `Account` query) and `fetchTrialBalance()` (QBO's reported TB — the authoritative side of the parallel-close compare, parsed out of the report's nested `Rows`/`ColData`, dropping the TOTAL summary) are complete; `fetchJournalEntries()` maps manual `JournalEntry` objects (debit/credit posting lines). `fetchExport()` assembles all three. A non-2xx response raises `QboApiError`. **Going live against a real company is just a `fetch`-backed client + an OAuth access token + the realm id** — the same seam pattern as the bank connectors.

```ts
const client = new QboApiClient(new FetchQbo(), { realmId, accessToken });
const exp = await client.fetchExport();            // { accounts, entries, reportedTrialBalance }
// → buildChartFromQbo / importQbo / compareTrialBalances, exactly like the CSV path
```

### Full-history import (GeneralLedger report)

`fetchGeneralLedger(start, end)` / `fetchExportViaGeneralLedger(start, end)` do a **faithful full-company import across all transaction types** — invoices, bills, payments, deposits, not just manual journals. QBO's GeneralLedger report is organized *by account*: each transaction appears once under every account it touches, with that account's signed amount (debit +, credit −). `parseGeneralLedger` tags each row with its account section and **groups rows across sections by transaction id**, reconstructing one balanced journal entry per transaction (lines sum to zero). Anything that doesn't balance is caught by `toPostCommands` (dropped to an issue), never silently posted. A test reconstructs three transactions across four accounts, imports them, and confirms the ledger ties to QBO's reported trial balance to the penny.

## Onboarding: two stages, one contract

Onboarding a business onto RGNR8 happens in two stages, and **both emit the same `forecast-inputs/1` DTO** — so the operator layer (`rgnr8-ops` `Fleet.onboard_from_dto`) provisions a client identically no matter which path it arrived by.

### Stage 1 — overlay (RGNR8 *on top of* QBO)

RGNR8 sits over QuickBooks; QBO stays the system of record. `QboApiClient.fetchOverlaySnapshot(asOf)` takes one live read of the three things a cash forecast needs — `fetchBankBalances()` (opening cash from Bank-type `CurrentBalance`), `fetchOpenInvoices()` (open AR, `Balance > 0`), `fetchOpenBills()` (open AP) — and `buildOverlayForecastInputs(snapshot, opts)` maps them to a `forecast-inputs/1` DTO: opening cash = sum of bank balances, open invoices → forecast inflows, open bills → forecast outflows. No import, no re-posting. The owner keeps running QBO and gets a verified 13-week outlook layered on day one. Zero/unparseable rows are dropped and noted; the opening is `verified: false` unless the caller asserts it was reconciled to the bank (`openingVerified: true`), since these are QBO's *reported* balances.

```ts
const snap = await client.fetchOverlaySnapshot("2026-08-31");
const { dto } = buildOverlayForecastInputs(snap, { currency: "USD" });
// → forecast-inputs/1, hand straight to Fleet.onboard_from_dto
```

### Stage 2 — migration (RGNR8 *becomes* the system of record)

`runMigrationOnboarding(export, opts)` runs the full pipeline end to end: build the chart, import the GeneralLedger into the RGNR8 ledger, compute the RGNR8 trial balance, **verify it against QBO's reported TB** (the parallel close), and emit the `forecast-inputs/1` DTO — with opening cash derived from **RGNR8's own posted ledger** (net of every Bank-type account) and marked `verified: true` *iff* the parallel close agrees. Open AR/AP timing still comes from the transaction-level QBO snapshot (a trial balance alone carries no per-document dates). `buildMigrationForecastInputs(export, trialBalance, opts)` is the pure DTO builder underneath.

```ts
const res = await runMigrationOnboarding(exp, { tenantId, currency: USD, asOf, ingestedAt, postedAt, openInvoices, openBills });
// res.verified === true  → the RGNR8 ledger ties to QuickBooks; res.dto is the verified opening
```

The distinction is the point: Stage 1's opening is QBO's reported number; Stage 2's opening is backed by immutable, balanced-by-construction journal entries RGNR8 posted itself, with the parallel-close diff as evidence. See `packages/prototype/onboard.ts` + `onboard.py` for both stages run cross-language into one operator dashboard.

## What's next

- IIF/QBXML file adapters (for QuickBooks Desktop), all emitting the same `QboExport`.
- OAuth token-refresh hook for the API client (access-token supplied today).
- Retain the parallel-close diff (trial-balance + statements) as part of the period's immutable financial package (`@rgnr8/close`).
