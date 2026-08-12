/**
 * Onboarding demo — the TypeScript half of the two-stage onboarding proof.
 *
 * Stage 1 (overlay): RGNR8 sits on top of QBO. A live QBO snapshot (bank
 * balances + open AR/AP) is mapped to a `forecast-inputs/1` DTO — QBO stays the
 * system of record.
 *
 * Stage 2 (migration): the full QBO GeneralLedger is imported into the RGNR8
 * ledger (RGNR8 becomes the system of record), verified against QBO's reported
 * trial balance (the parallel close), and the forecast opening is derived from
 * RGNR8's own posted ledger.
 *
 * Both stages emit the *same* `forecast-inputs/1` contract, so the Python
 * onboarding side (`onboard.py`) provisions each tenant identically. Writes:
 *   out/overlay_inputs.json      Stage-1 DTO (from the QBO snapshot)
 *   out/migration_inputs.json    Stage-2 DTO (from the imported RGNR8 ledger)
 *   out/migration_diff.html      parallel-close diff (RGNR8 vs QBO)
 *   out/onboard_summary.json     both stages' headline numbers + verification
 */

import { writeFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { USD, asTenantId } from "@rgnr8/ledger-kernel";
import {
  buildOverlayForecastInputs,
  runMigrationOnboarding,
  renderDiffHtml,
  type QboExport,
  type QboOpenBill,
  type QboOpenInvoice,
  type QboOverlaySnapshot,
} from "@rgnr8/qbo-migrate";

const OUT = join(dirname(fileURLToPath(import.meta.url)), "out");
mkdirSync(OUT, { recursive: true });
const write = (name: string, body: string): void => {
  writeFileSync(join(OUT, name), body);
  // eslint-disable-next-line no-console
  console.log(`  wrote out/${name}`);
};

// Shared open AR/AP (the transaction-level timing both stages want).
const OPEN_INVOICES: QboOpenInvoice[] = [
  { id: "INV-101", customerId: "C-7", customerName: "Northwind LLC", issueDate: "2026-07-20", dueDate: "2026-08-19", balance: "18000.00" },
  { id: "INV-102", customerId: "C-9", customerName: "Contoso", issueDate: "2026-08-01", dueDate: "2026-08-31", balance: "9500.00" },
];
const OPEN_BILLS: QboOpenBill[] = [
  { id: "BILL-201", vendorId: "V-3", vendorName: "Acme Supplies", txnDate: "2026-07-15", dueDate: "2026-08-14", balance: "6000.00" },
  { id: "BILL-202", vendorId: "V-8", vendorName: "Utilities Co", txnDate: "2026-08-05", dueDate: "2026-09-04", balance: "1450.00" },
];

// --- Stage 1: overlay ------------------------------------------------------

console.log("Stage 1 — overlay (RGNR8 on top of QBO):");
const snapshot: QboOverlaySnapshot = {
  asOf: "2026-08-31",
  bankAccounts: [
    { id: "35", name: "Checking", currentBalance: "48210.55" },
    { id: "36", name: "Savings", currentBalance: "15000.00" },
  ],
  openInvoices: OPEN_INVOICES,
  openBills: OPEN_BILLS,
};
const overlay = buildOverlayForecastInputs(snapshot, { currency: "USD" });
for (const n of overlay.notes) console.log(`  ${n}`);
write("overlay_inputs.json", JSON.stringify(overlay.dto, null, 2));

// --- Stage 2: migration ----------------------------------------------------

console.log("Stage 2 — migration (RGNR8 becomes the system of record):");
const EXPORT: QboExport = {
  accounts: [
    { name: "Checking", acctNum: "1000", type: "Bank" },
    { name: "Savings", acctNum: "1010", type: "Bank" },
    { name: "Accounts Receivable", acctNum: "1200", type: "Accounts Receivable" },
    { name: "Accounts Payable", acctNum: "2000", type: "Accounts Payable" },
    { name: "Owner Equity", acctNum: "3000", type: "Equity" },
    { name: "Sales", acctNum: "4000", type: "Income" },
  ],
  entries: [
    { id: "1", date: "2026-08-01", lines: [{ account: "Checking", debit: "50000.00" }, { account: "Owner Equity", credit: "50000.00" }] },
    { id: "2", date: "2026-08-10", lines: [{ account: "Savings", debit: "15000.00" }, { account: "Checking", credit: "15000.00" }] },
    { id: "3", date: "2026-08-15", lines: [{ account: "Checking", debit: "13210.55" }, { account: "Sales", credit: "13210.55" }] },
  ],
  reportedTrialBalance: [
    { account: "Checking", debit: "48210.55" },
    { account: "Savings", debit: "15000.00" },
    { account: "Owner Equity", credit: "50000.00" },
    { account: "Sales", credit: "13210.55" },
  ],
};

const migration = await runMigrationOnboarding(EXPORT, {
  tenantId: asTenantId("bright"),
  currency: USD,
  asOf: "2026-08-31",
  ingestedAt: "2026-08-31T00:00:00Z",
  postedAt: "2026-08-31T00:00:00Z",
  openInvoices: OPEN_INVOICES,
  openBills: OPEN_BILLS,
});
for (const n of migration.notes) console.log(`  ${n}`);
write("migration_inputs.json", JSON.stringify(migration.dto, null, 2));
if (migration.diff) write("migration_diff.html", renderDiffHtml(migration.diff));

// --- summary ---------------------------------------------------------------

const summary = {
  generatedFrom: "packages/prototype/onboard.ts",
  overlay: {
    stage: "overlay (QBO stays system of record)",
    opening: overlay.dto.opening.available,
    openingVerified: overlay.dto.opening.verified,
    invoices: overlay.dto.invoices.length,
    bills: overlay.dto.bills.length,
  },
  migration: {
    stage: "migration (RGNR8 becomes system of record)",
    posted: migration.importReport.posted,
    opening: migration.dto.opening.available,
    openingVerified: migration.dto.opening.verified,
    parallelClose: migration.verified ? "VERIFIED" : "NEEDS REVIEW",
    matched: migration.diff?.matched ?? 0,
    mismatches: migration.diff?.mismatches.length ?? 0,
    invoices: migration.dto.invoices.length,
    bills: migration.dto.bills.length,
  },
};
write("onboard_summary.json", JSON.stringify(summary, null, 2));
console.log("Onboarding TS half complete — both stages emitted forecast-inputs/1 DTOs.");
