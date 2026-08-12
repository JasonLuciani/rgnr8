/**
 * Runnable demo: `node --import tsx examples/demo.ts`
 * Posts a few transactions for a fictional agency and prints the trial balance.
 */
import {
  AccountType,
  ChartOfAccounts,
  InMemoryLedgerStore,
  Money,
  PeriodRegistry,
  PostingEngine,
  USD,
  asAccountId,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  computeTrialBalance,
} from "../src/index.js";
import type { Account, Provenance } from "../src/index.js";

const usd = (d: string) => Money.fromDecimal(d, USD);
const A = {
  cash: asAccountId("cash"),
  ar: asAccountId("ar"),
  revenue: asAccountId("revenue"),
  payroll: asAccountId("payroll"),
};
const mk = (id: (typeof A)[keyof typeof A], code: string, name: string, type: AccountType): Account => ({
  id,
  code,
  name,
  type,
  currency: USD,
});

const coa = new ChartOfAccounts([
  mk(A.cash, "1000", "Cash", AccountType.ASSET),
  mk(A.ar, "1100", "Accounts Receivable", AccountType.ASSET),
  mk(A.revenue, "4000", "Revenue", AccountType.REVENUE),
  mk(A.payroll, "6000", "Payroll Expense", AccountType.EXPENSE),
]);

const store = new InMemoryLedgerStore();
const periods = new PeriodRegistry();
const engine = new PostingEngine(coa, store, periods);
const tenant = asTenantId("bright-agency");
const period = asPeriodKey("2026-08");

const prov: Provenance = {
  sourceSystem: "demo",
  sourceObject: "manual",
  sourceVersion: "1",
  effectiveDate: "2026-08-15",
  postedDate: "2026-08-15",
  ingestedAt: "2026-08-15T00:00:00Z",
  normalizationVersion: "n1",
  mappingVersion: "m1",
};

async function main() {
// 1) Invoice a client for $12,000 (Dr AR / Cr Revenue)
await engine.post(
  {
    tenantId: tenant,
    idempotencyKey: asIdempotencyKey("inv-1001"),
    periodKey: period,
    currency: USD,
    entryDate: "2026-08-03",
    memo: "Invoice #1001",
    provenance: prov,
    lines: [
      { accountId: A.ar, side: "DEBIT", amount: usd("12000.00") },
      { accountId: A.revenue, side: "CREDIT", amount: usd("12000.00") },
    ],
  },
  { postedAt: "2026-08-03T09:00:00Z" },
);

// 2) Client pays $12,000 (Dr Cash / Cr AR)
await engine.post(
  {
    tenantId: tenant,
    idempotencyKey: asIdempotencyKey("pay-1001"),
    periodKey: period,
    currency: USD,
    entryDate: "2026-08-20",
    memo: "Payment for #1001",
    provenance: prov,
    lines: [
      { accountId: A.cash, side: "DEBIT", amount: usd("12000.00") },
      { accountId: A.ar, side: "CREDIT", amount: usd("12000.00") },
    ],
  },
  { postedAt: "2026-08-20T14:00:00Z" },
);

// 3) Run payroll $7,500 (Dr Payroll / Cr Cash)
await engine.post(
  {
    tenantId: tenant,
    idempotencyKey: asIdempotencyKey("payroll-aug"),
    periodKey: period,
    currency: USD,
    entryDate: "2026-08-31",
    memo: "August payroll",
    provenance: prov,
    lines: [
      { accountId: A.payroll, side: "DEBIT", amount: usd("7500.00") },
      { accountId: A.cash, side: "CREDIT", amount: usd("7500.00") },
    ],
  },
  { postedAt: "2026-08-31T17:00:00Z" },
);

const tb = await computeTrialBalance(store, tenant, coa, USD);

console.log(`\nTrial balance — ${tenant} — period 2026-08 (${USD.code})`);
console.log("".padEnd(56, "-"));
console.log("Code  Account".padEnd(34) + "Debit".padStart(11) + "Credit".padStart(11));
for (const r of tb.rows) {
  console.log(
    `${r.code}  ${r.name}`.padEnd(34) +
      (r.debit.isZero() ? "" : r.debit.toDecimalString()).padStart(11) +
      (r.credit.isZero() ? "" : r.credit.toDecimalString()).padStart(11),
  );
}
console.log("".padEnd(56, "-"));
console.log(
  "TOTAL".padEnd(34) + tb.totalDebit.toDecimalString().padStart(11) + tb.totalCredit.toDecimalString().padStart(11),
);
console.log(`\nIn balance: ${tb.inBalance ? "YES" : "NO"}  ·  Entries posted: ${(await store.list(tenant)).length}\n`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
