# @rgnr8/close

Month-end close management: a close checklist that **gates the ledger period lock** on the period's controls being clean, and locks the period in the kernel when they are — after which the posting engine rejects any further entry into that period.

TypeScript, depends only on `@rgnr8/ledger-kernel`. 29 tests, `tsc --strict` clean.

## The gate

`buildCloseChecklist(inputs)` turns the period's control results into pass/fail tasks:

- **All bank/card accounts reconciled** (every reconciliation is BALANCED),
- **Reconciliations signed off** by a reviewer (when `requireSignOff`),
- **Subledger control accounts tie to the GL** (AR/AP control reconciliations balanced),
- **Trial balance is in balance**.

`closePeriod(periods, periodKey, inputs, { closedBy, at })` runs the checklist. If every task passes, it calls `periods.close(periodKey)` — and because period locks are enforced in the kernel's posting engine, **no further postings can hit a closed period**. If anything fails, the close is blocked, the failing tasks are returned, and nothing is locked. `reopenPeriod` unlocks for a controlled prior-period adjustment.

The inputs are structural (`ReconLike`, `ControlLike`, `trialBalanceBalanced`) so this composes with `@rgnr8/reconciliation`, `@rgnr8/subledger` (control reconciliations), and the kernel's trial balance without a hard dependency on each.

## Use it

```ts
const pkg = closePeriod(periods, asPeriodKey("2026-08"), {
  reconciliations,           // from @rgnr8/reconciliation
  controls,                  // from @rgnr8/subledger reconcileControl
  trialBalanceBalanced: tb.inBalance,
  requireSignOff: true,
}, { closedBy: "controller@rgnr8", at: new Date().toISOString() });

pkg.closed;    // true only if every gate passed
pkg.blockers;  // the failing tasks otherwise
```

```bash
npm test   # includes proof that a closed period rejects further postings
```

## The immutable financial package

When a period closes, `buildFinancialPackage(input, meta)` freezes its numbers — trial balance, income statement, balance sheet, and (when a QuickBooks parallel close was run) the reconciliation to QBO — into a **tamper-evident** artifact. The fingerprint is a SHA-256 over a canonical serialization of the *financial content only* (envelope metadata like `closedBy`/`packagedAt` is excluded), so it is **reproducible from the ledger**: re-deriving the same books yields the same fingerprint, and any later edit to a stored package is caught by `verifyFinancialPackage`. Money is carried as minor-unit integer strings, so the package is exact and language-neutral (this stays a leaf module — no hard dep on the kernel's `Money`).

```ts
const pkg = buildFinancialPackage(
  { periodKey: "2026-08", currency: "USD", trialBalance, incomeStatement, balanceSheet, qboReconciliation },
  { closedBy: "controller", closedAt, packagedAt },
);
pkg.fingerprint;                     // sha256 hex over the financial content
verifyFinancialPackage(pkg).valid;   // false if any stored number was altered
```

The test suite seals a real posted ledger's books (via the kernel + `@rgnr8/statements`), proves the fingerprint is envelope-independent and reproducible, and that tampering with a sealed number is detected.

## Persisting the package (the published record)

A sealed package is a *published record* — it must be stored, retrievable, and never silently changed. `FinancialPackageStore` (`save` / `get` / `list`) has two implementations — `InMemoryFinancialPackageStore` and `SqlFinancialPackageStore` (over the same `SqlExecutor` seam the ledger and migration runner use; its table ships as `FINANCIAL_PACKAGE_MIGRATIONS` for `@rgnr8/migrations`). Both enforce two invariants, covered by a shared contract test against each backend:

- **Integrity** — a package is fingerprint-verified before it's stored and again when read; a row edited directly in the database is caught on `get` (`PackageIntegrityError`).
- **Immutability** — writing a *different* package for a `(tenant, period)` that already has one throws `PackagePublishedError`; re-writing the identical package is an idempotent no-op. A sealed period's record can't be quietly rewritten.

```ts
await runMigrations(pool, FINANCIAL_PACKAGE_MIGRATIONS, { appliedAt });
const store = new SqlFinancialPackageStore(pool);
await store.save(tenantId, buildFinancialPackage(input, meta));
const published = await store.get(tenantId, "2026-08"); // verified on read
```

## The close calendar

The gate answers "can we lock the period?"; the **calendar** sequences the *work* to get there. `buildCloseCalendar(periodEnd, { holidays?, template? })` computes each task's due date as business days after period end (weekends + injected holidays skipped, deterministic — `addBusinessDays`). `DEFAULT_CLOSE_TEMPLATE` is the standard close in order (sync feeds → reconcile bank → tie AR/AP → review adjustments → trial balance → statements → seal package → deliver), each with an owner role and dependencies. `closeCalendarStatus(entries, asOf, completed)` classifies every task — **done / overdue / due-today / blocked / upcoming** (blocked while any dependency is incomplete) — and surfaces `nextUp`, counts, and `complete`. `renderCloseCalendarHtml` draws it. Deterministic throughout (ISO dates, injected holidays, no wall clock).

## What's next

- Persist per-tenant completion state so the calendar tracks a live close, and wire the `seal_package`/`deliver` tasks to the actual `buildFinancialPackage` + delivery runtime.
- Per-role notifications when a task goes overdue.
