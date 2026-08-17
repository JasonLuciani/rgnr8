import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountSubtype,
  AccountType,
  ChartOfAccounts,
  InMemoryLedgerStore,
  Money,
  PostingEngine,
  USD,
  asAccountId,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  computeTrialBalance,
  type Account,
  type JournalLineInput,
  type PostCommand,
  type PostedEntry,
  type Provenance,
} from "@rgnr8/ledger-kernel";
import {
  Budget,
  accountLedger,
  balanceSheet,
  budgetVsActual,
  cashBasisIncomeStatement,
  cashFlow,
  comparativeBalanceSheet,
  fromKernelTrialBalance,
  glDetail,
  incomeStatement,
  multiPeriodIncomeStatement,
  renderGlDetailAccount,
  renderRetainedEarnings,
  renderTrialBalance,
  retainedEarnings,
  subtypeCashFlowClassifier,
} from "../src/index.js";

const tenant = asTenantId("acme");

function acct(id: string, code: string, name: string, type: AccountType, subtype?: AccountSubtype): Account {
  return { id: asAccountId(id), code, name, type, currency: USD, ...(subtype ? { subtype } : {}) };
}

const coa = new ChartOfAccounts([
  acct("cash", "1000", "Cash", AccountType.ASSET, AccountSubtype.BANK),
  acct("ar", "1100", "Accounts Receivable", AccountType.ASSET, AccountSubtype.ACCOUNTS_RECEIVABLE),
  acct("equip", "1500", "Equipment", AccountType.ASSET, AccountSubtype.FIXED_ASSET),
  acct("ap", "2000", "Accounts Payable", AccountType.LIABILITY, AccountSubtype.ACCOUNTS_PAYABLE),
  acct("loan", "2500", "Loan Payable", AccountType.LIABILITY, AccountSubtype.LONG_TERM_LIABILITY),
  acct("capital", "3000", "Owner Capital", AccountType.EQUITY, AccountSubtype.EQUITY),
  acct("rev", "4000", "Sales Revenue", AccountType.REVENUE, AccountSubtype.INCOME),
  acct("exp", "5000", "Operating Expense", AccountType.EXPENSE, AccountSubtype.EXPENSE),
]);

const prov: Provenance = {
  sourceSystem: "test", sourceObject: "fixture", sourceVersion: "1",
  effectiveDate: "2026-08-01", postedDate: "2026-08-01", ingestedAt: "2026-08-01",
  normalizationVersion: "1", mappingVersion: "1",
};
const m = (minor: bigint) => Money.fromMinorUnits(minor, USD);
const dr = (id: string, minor: bigint): JournalLineInput => ({ accountId: asAccountId(id), side: "DEBIT", amount: m(minor) });
const cr = (id: string, minor: bigint): JournalLineInput => ({ accountId: asAccountId(id), side: "CREDIT", amount: m(minor) });

interface E { key: string; date: string; period: string; lines: JournalLineInput[]; memo?: string }

async function seed(entries: readonly E[]): Promise<InMemoryLedgerStore> {
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(coa, store);
  for (const e of entries) {
    const cmd: PostCommand = {
      tenantId: tenant, idempotencyKey: asIdempotencyKey(e.key), periodKey: asPeriodKey(e.period),
      currency: USD, entryDate: e.date, lines: e.lines, provenance: prov, ...(e.memo ? { memo: e.memo } : {}),
    };
    await engine.post(cmd, { postedAt: `${e.date}T00:00:00Z` });
  }
  return store;
}

const book: E[] = [
  { key: "o1", date: "2026-07-31", period: "2026-07", lines: [dr("cash", 50000n), cr("capital", 50000n)], memo: "Opening capital" },
  { key: "a1", date: "2026-08-03", period: "2026-08", lines: [dr("ar", 30000n), cr("rev", 30000n)], memo: "Invoice #1" },
  { key: "a2", date: "2026-08-10", period: "2026-08", lines: [dr("cash", 20000n), cr("ar", 20000n)], memo: "Collect #1" },
  { key: "a3", date: "2026-08-12", period: "2026-08", lines: [dr("exp", 12000n), cr("cash", 12000n)], memo: "Pay rent" },
  { key: "a4", date: "2026-08-20", period: "2026-08", lines: [dr("exp", 5000n), cr("ap", 5000n)], memo: "Accrue bill" },
  { key: "a5", date: "2026-08-22", period: "2026-08", lines: [dr("equip", 8000n), cr("cash", 8000n)], memo: "Buy laptop" },
  { key: "a6", date: "2026-08-25", period: "2026-08", lines: [dr("cash", 10000n), cr("loan", 10000n)], memo: "Loan draw" },
];

const AUG = { from: "2026-08-01", to: "2026-08-31" };

test("GL detail: cash account rolls opening + in-window rows to a running balance", async () => {
  const entries = await (await seed(book)).list(tenant) as readonly PostedEntry[];
  const detail = glDetail(entries, coa, USD, { window: AUG });
  const cash = detail.find((a) => a.accountId === asAccountId("cash"));
  assert.ok(cash);
  assert.equal(cash!.opening.toDecimalString(), "500.00"); // July capital
  // in-window: +200 -120 -80 +100 = +100 -> closing 600
  assert.equal(cash!.closing.toDecimalString(), "600.00");
  // running balance is monotonic-correct on the last row
  assert.equal(cash!.rows.at(-1)!.balance.toDecimalString(), "600.00");
  assert.equal(cash!.rows.length, 4);
});

test("drill-down: accountLedger returns one account's ledger", async () => {
  const entries = await (await seed(book)).list(tenant) as readonly PostedEntry[];
  const ar = accountLedger(entries, asAccountId("ar"), coa, USD, AUG);
  assert.ok(ar);
  assert.equal(ar!.rows.length, 2); // invoice + collection
  assert.equal(ar!.closing.toDecimalString(), "100.00"); // 300 - 200 outstanding
});

test("statement of retained earnings rolls beginning + NI − distributions", () => {
  const re = retainedEarnings({ beginning: m(20000n), netIncome: m(13000n), distributions: m(5000n) });
  assert.equal(re.ending.toDecimalString(), "280.00"); // 200 + 130 - 50
  assert.ok(renderRetainedEarnings(re).includes("Retained Earnings"));
});

test("subtype cash-flow classifier keys off account subtype, not code regex", async () => {
  const store = await seed(book);
  const tbEnd = fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, { to: "2026-08-31" }));
  const tbStart = fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, { to: "2026-07-31" }));
  const is = incomeStatement(fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, AUG)));
  const cf = cashFlow(tbStart, tbEnd, is.netIncome, subtypeCashFlowClassifier(coa));
  assert.ok(cf.reconciled);
  // Equipment (FIXED_ASSET) is investing: -80; loan (LONG_TERM_LIABILITY) financing: +100
  assert.equal(cf.investing.toDecimalString(), "-80.00");
  assert.equal(cf.financing.toDecimalString(), "100.00");
});

test("cash-basis income statement backs out the un-collected accrual", async () => {
  const store = await seed(book);
  const periodTb = fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, AUG));
  const startTb = fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, { to: "2026-07-31" }));
  const endTb = fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, { to: "2026-08-31" }));
  const cb = cashBasisIncomeStatement(periodTb, startTb, endTb, {
    receivableIds: [asAccountId("ar")],
    payableIds: [asAccountId("ap")],
  });
  // Accrual revenue 300; ΔAR +100 backed out -> cash-basis revenue 200.
  assert.equal(cb.accrualRevenue.toDecimalString(), "300.00");
  assert.equal(cb.receivableChange.toDecimalString(), "100.00");
  assert.equal(cb.revenue.toDecimalString(), "200.00");
  // Accrual expense 170; ΔAP +50 backed out -> cash-basis expense 120.
  assert.equal(cb.expenses.toDecimalString(), "120.00");
  assert.equal(cb.netIncome.toDecimalString(), "80.00");
});

test("multi-period income statement aligns accounts across columns", async () => {
  const store = await seed(book);
  const jul = fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, { from: "2026-07-01", to: "2026-07-31" }));
  const aug = fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, AUG));
  const mp = multiPeriodIncomeStatement([{ label: "Jul", tb: jul }, { label: "Aug", tb: aug }]);
  assert.deepEqual([...mp.columnLabels], ["Jul", "Aug"]);
  assert.equal(mp.netIncome[0]!.toDecimalString(), "0.00"); // no P&L in July
  assert.equal(mp.netIncome[1]!.toDecimalString(), "130.00");
});

test("comparative balance sheet reports each column balanced", async () => {
  const store = await seed(book);
  const jul = fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, { to: "2026-07-31" }));
  const aug = fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, { to: "2026-08-31" }));
  const isJul = incomeStatement(fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, { from: "2026-07-01", to: "2026-07-31" })));
  const isAug = incomeStatement(fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, AUG)));
  const cmp = comparativeBalanceSheet([
    { label: "Jul", tb: jul, netIncome: isJul.netIncome },
    { label: "Aug", tb: aug, netIncome: isAug.netIncome },
  ]);
  assert.deepEqual([...cmp.balanced], [true, true]);
});

test("budget vs actual computes variance and favorability", async () => {
  const store = await seed(book);
  const augTb = fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, AUG));
  const period = asPeriodKey("2026-08");
  const budget = new Budget()
    .set(asAccountId("rev"), period, m(25000n)) // budgeted 250, actual 300 -> favorable
    .set(asAccountId("exp"), period, m(20000n)); // budgeted 200, actual 170 -> favorable (under)
  const report = budgetVsActual(budget, augTb, period);
  const rev = report.lines.find((l) => l.accountId === asAccountId("rev"))!;
  const exp = report.lines.find((l) => l.accountId === asAccountId("exp"))!;
  assert.equal(rev.variance.toDecimalString(), "50.00");
  assert.equal(rev.favorable, true);
  assert.equal(exp.variance.toDecimalString(), "-30.00");
  assert.equal(exp.favorable, true);
});

test("renderers produce branded HTML fragments", async () => {
  const store = await seed(book);
  const augTb = fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, { to: "2026-08-31" }));
  assert.ok(renderTrialBalance(augTb).includes("Trial Balance"));
  const entries = await store.list(tenant) as readonly PostedEntry[];
  const ar = accountLedger(entries, asAccountId("ar"), coa, USD, AUG)!;
  assert.ok(renderGlDetailAccount(ar).includes("GL Detail"));
  // balance sheet still balances with subtypes present
  const is = incomeStatement(fromKernelTrialBalance(await computeTrialBalance(store, tenant, coa, USD, AUG)));
  const bs = balanceSheet(augTb, is.netIncome);
  assert.equal(bs.balanced, true);
});
