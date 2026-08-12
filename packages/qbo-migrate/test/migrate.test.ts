import { test } from "node:test";
import assert from "node:assert/strict";
import { USD, asTenantId } from "@rgnr8/ledger-kernel";
import {
  runMigrationOnboarding,
  type QboExport,
  type QboOpenBill,
  type QboOpenInvoice,
} from "../src/index.js";

// A small but real QBO export: an opening equity funding + a cash sale, plus
// QBO's own reported trial balance to verify the import against.
const EXPORT: QboExport = {
  accounts: [
    { name: "Checking", acctNum: "1000", type: "Bank" },
    { name: "Savings", acctNum: "1010", type: "Bank" },
    { name: "Accounts Receivable", acctNum: "1200", type: "Accounts Receivable" },
    { name: "Owner Equity", acctNum: "3000", type: "Equity" },
    { name: "Sales", acctNum: "4000", type: "Income" },
  ],
  entries: [
    {
      id: "1",
      date: "2026-08-01",
      lines: [
        { account: "Checking", debit: "50000.00" },
        { account: "Owner Equity", credit: "50000.00" },
      ],
    },
    {
      id: "2",
      date: "2026-08-15",
      lines: [
        { account: "Savings", debit: "13210.55" },
        { account: "Sales", credit: "13210.55" },
      ],
    },
  ],
  reportedTrialBalance: [
    { account: "Checking", debit: "50000.00" },
    { account: "Savings", debit: "13210.55" },
    { account: "Owner Equity", credit: "50000.00" },
    { account: "Sales", credit: "13210.55" },
  ],
};

const OPEN_INVOICES: QboOpenInvoice[] = [
  { id: "INV-101", customerId: "C-7", issueDate: "2026-07-20", dueDate: "2026-08-19", balance: "18000.00" },
];
const OPEN_BILLS: QboOpenBill[] = [
  { id: "BILL-201", vendorId: "V-3", txnDate: "2026-07-15", dueDate: "2026-08-14", balance: "6000.00" },
];

const OPTS = {
  tenantId: asTenantId("northwind"),
  currency: USD,
  asOf: "2026-08-31",
  ingestedAt: "2026-08-31T00:00:00Z",
  postedAt: "2026-08-31T00:00:00Z",
  openInvoices: OPEN_INVOICES,
  openBills: OPEN_BILLS,
};

test("migration imports the GL, verifies against QBO's TB, and derives opening from the RGNR8 ledger", async () => {
  const res = await runMigrationOnboarding(EXPORT, OPTS);
  assert.equal(res.importReport.posted, 2);
  assert.equal(res.importReport.skipped, 0);
  // parallel close agrees → verified
  assert.ok(res.diff);
  assert.equal(res.diff?.inAgreement, true);
  assert.equal(res.verified, true);
  // opening cash = Checking 50000 + Savings 13210.55, straight off the posted ledger
  assert.deepEqual(res.dto.opening.available, { minor: 6321055, currency: "USD" });
  assert.equal(res.dto.opening.verified, true); // because the parallel close agreed
  assert.equal(res.dto.contract, "forecast-inputs/1");
});

test("open AR/AP snapshot flows into the migration DTO for invoice-level timing", async () => {
  const res = await runMigrationOnboarding(EXPORT, OPTS);
  assert.equal(res.dto.invoices.length, 1);
  assert.deepEqual(res.dto.invoices[0]?.open_amount, { minor: 1800000, currency: "USD" });
  assert.equal(res.dto.bills.length, 1);
  assert.deepEqual(res.dto.bills[0]?.amount, { minor: 600000, currency: "USD" });
  assert.equal(res.dto.bills[0]?.scheduled_date, null);
});

test("a parallel-close disagreement leaves opening unverified", async () => {
  const drifted: QboExport = {
    ...EXPORT,
    // QBO claims more cash than the RGNR8 ledger posted → mismatch
    reportedTrialBalance: [
      { account: "Checking", debit: "99999.00" },
      { account: "Savings", debit: "13210.55" },
      { account: "Owner Equity", credit: "50000.00" },
      { account: "Sales", credit: "13210.55" },
    ],
  };
  const res = await runMigrationOnboarding(drifted, OPTS);
  assert.equal(res.verified, false);
  assert.equal(res.dto.opening.verified, false);
  assert.ok((res.diff?.mismatches.length ?? 0) >= 1);
});

test("re-running the migration is idempotent (same idempotency keys, nothing double-posted)", async () => {
  const first = await runMigrationOnboarding(EXPORT, OPTS);
  // reuse the same store → replaying the export posts nothing new
  const second = await runMigrationOnboarding(EXPORT, { ...OPTS, store: first.store });
  assert.deepEqual(second.dto.opening.available, first.dto.opening.available);
  assert.equal(second.diff?.inAgreement, true);
});
