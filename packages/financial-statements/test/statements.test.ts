import { test } from "node:test";
import assert from "node:assert/strict";
import {
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
  type Provenance,
} from "@rgnr8/ledger-kernel";
import {
  assertBalanceSheetBalances,
  assertCashFlowReconciles,
  balanceSheet,
  cashFlow,
  fromKernelTrialBalance,
  incomeStatement,
} from "../src/index.js";

// --- fixture chart of accounts ----------------------------------------------

const tenant = asTenantId("acme");
const period = asPeriodKey("2026-08");

function acct(id: string, code: string, name: string, type: AccountType): Account {
  return { id: asAccountId(id), code, name, type, currency: USD };
}

const coa = new ChartOfAccounts([
  acct("cash", "1000", "Cash", AccountType.ASSET),
  acct("ar", "1100", "Accounts Receivable", AccountType.ASSET),
  acct("equip", "1500", "Equipment", AccountType.ASSET),
  acct("ap", "2000", "Accounts Payable", AccountType.LIABILITY),
  acct("loan", "2500", "Loan Payable", AccountType.LIABILITY),
  acct("capital", "3000", "Owner Capital", AccountType.EQUITY),
  acct("re", "3900", "Retained Earnings", AccountType.EQUITY),
  acct("rev", "4000", "Sales Revenue", AccountType.REVENUE),
  acct("exp", "5000", "Operating Expense", AccountType.EXPENSE),
]);

const prov: Provenance = {
  sourceSystem: "test",
  sourceObject: "fixture",
  sourceVersion: "1",
  effectiveDate: "2026-08-01",
  postedDate: "2026-08-01",
  ingestedAt: "2026-08-01",
  normalizationVersion: "1",
  mappingVersion: "1",
};

const m = (minor: bigint) => Money.fromMinorUnits(minor, USD);
const dr = (id: string, minor: bigint): JournalLineInput => ({
  accountId: asAccountId(id),
  side: "DEBIT",
  amount: m(minor),
});
const cr = (id: string, minor: bigint): JournalLineInput => ({
  accountId: asAccountId(id),
  side: "CREDIT",
  amount: m(minor),
});

async function ledgerFrom(entries: readonly { key: string; lines: JournalLineInput[] }[]) {
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(coa, store);
  for (const e of entries) {
    const cmd: PostCommand = {
      tenantId: tenant,
      idempotencyKey: asIdempotencyKey(e.key),
      periodKey: period,
      currency: USD,
      entryDate: "2026-08-15",
      lines: e.lines,
      provenance: prov,
    };
    await engine.post(cmd, { postedAt: "2026-08-15T00:00:00Z" });
  }
  return computeTrialBalance(store, tenant, coa, USD);
}

// Opening ledger: $500 cash contributed as owner capital.
const opening = [{ key: "o1", lines: [dr("cash", 50000n), cr("capital", 50000n)] }];

// Period activity layered on top of the opening.
const activity = [
  { key: "a1", lines: [dr("ar", 30000n), cr("rev", 30000n)] }, // sale on credit
  { key: "a2", lines: [dr("cash", 20000n), cr("ar", 20000n)] }, // collect AR
  { key: "a3", lines: [dr("exp", 12000n), cr("cash", 12000n)] }, // pay expense (cash)
  { key: "a4", lines: [dr("exp", 5000n), cr("ap", 5000n)] }, // accrue expense (AP)
  { key: "a5", lines: [dr("equip", 8000n), cr("cash", 8000n)] }, // buy equipment
  { key: "a6", lines: [dr("cash", 10000n), cr("loan", 10000n)] }, // take a loan
];

test("fixture ledger: trial balance is in balance", async () => {
  const ktb = await ledgerFrom([...opening, ...activity]);
  assert.equal(ktb.inBalance, true);
  const tb = fromKernelTrialBalance(ktb);
  // Signed balances sum to exactly zero on a sound ledger.
  const total = tb.entries.reduce((acc, e) => acc + e.signed.minorUnits, 0n);
  assert.equal(total, 0n);
});

test("balance sheet balances to zero and net income ties into equity", async () => {
  const tb = fromKernelTrialBalance(await ledgerFrom([...opening, ...activity]));

  const is = incomeStatement(tb);
  assert.equal(is.revenue.toDecimalString(), "300.00");
  assert.equal(is.expenses.toDecimalString(), "170.00");
  assert.equal(is.netIncome.toDecimalString(), "130.00");

  const bs = balanceSheet(tb, is.netIncome);
  assert.equal(bs.assets.toDecimalString(), "780.00"); // 600 cash + 100 AR + 80 equip
  assert.equal(bs.liabilities.toDecimalString(), "150.00"); // 50 AP + 100 loan
  assert.equal(bs.equity.toDecimalString(), "630.00"); // 500 capital + 130 NI

  // Balanced to exactly zero.
  assert.equal(bs.balanced, true);
  assert.equal(bs.residual.toDecimalString(), "0.00");
  assert.doesNotThrow(() => assertBalanceSheetBalances(bs));

  // Net income ties: equity minus booked equity equals the P&L net income.
  const bookedEquity = bs.equity.minus(is.netIncome);
  assert.equal(bookedEquity.toDecimalString(), "500.00");
  assert.equal(bs.equity.minus(bookedEquity).equals(is.netIncome), true);
});

test("a mangled net income breaks the balance-sheet assertion", async () => {
  const tb = fromKernelTrialBalance(await ledgerFrom([...opening, ...activity]));
  const wrong = balanceSheet(tb, Money.fromMinorUnits(999n, USD));
  assert.equal(wrong.balanced, false);
  assert.throws(() => assertBalanceSheetBalances(wrong), /does not balance/);
});

test("folding in beginning retained earnings keeps a mid-period sheet balanced", async () => {
  // The as-of (cumulative) trial balance carries revenue/expense earned before
  // the reporting window; the *period* income statement does not. Passing only
  // the period net income leaves the sheet short by the prior earnings — unless
  // beginning retained earnings is folded in. This is the unit-level guard for
  // the mid-year balance-sheet fix.
  const endTb = fromKernelTrialBalance(await ledgerFrom([...opening, ...activity]));
  const cumulativeNetIncome = incomeStatement(endTb).netIncome;  // all earnings through `to`

  // Split it: pretend half the net income was earned before the window opened.
  const periodNi = Money.fromMinorUnits(cumulativeNetIncome.minorUnits / 2n, USD);
  const beginningRetained = cumulativeNetIncome.minus(periodNi);

  // Without beginning retained earnings the sheet is out of balance...
  assert.equal(balanceSheet(endTb, periodNi).balanced, false);
  // ...and folding it in makes it foot to exactly zero.
  const bs = balanceSheet(endTb, periodNi, beginningRetained);
  assert.equal(bs.balanced, true);
  assert.doesNotThrow(() => assertBalanceSheetBalances(bs));
  // total equity carries booked equity + prior retained + current period NI
  assert.equal(bs.equity.equals(
    balanceSheet(endTb, cumulativeNetIncome).equity), true);
});

test("cash flow net change equals the cash-account delta between two periods", async () => {
  const tbStart = fromKernelTrialBalance(await ledgerFrom(opening));
  const tbEnd = fromKernelTrialBalance(await ledgerFrom([...opening, ...activity]));

  // Period net income (rev/exp are zero at the opening).
  const netIncome = incomeStatement(tbEnd).netIncome;
  const cf = cashFlow(tbStart, tbEnd, netIncome);

  // Independently compute the cash-account movement from the two trial balances.
  const cashStart = tbStart.entries.find((e) => e.code === "1000")!.signed;
  const cashEnd = tbEnd.entries.find((e) => e.code === "1000")!.signed;
  const cashDelta = cashEnd.minus(cashStart);

  assert.equal(cf.netChange.equals(cashDelta), true);
  assert.equal(cf.netChange.toDecimalString(), "100.00"); // 500 -> 600
  assert.equal(cf.beginningCash.toDecimalString(), "500.00");
  assert.equal(cf.endingCash.toDecimalString(), "600.00");

  // Indirect method, sections exercised: operating + investing + financing.
  assert.equal(cf.operating.toDecimalString(), "80.00"); // 130 NI - 10 AR + 5 AP
  assert.equal(cf.investing.toDecimalString(), "-80.00"); // equipment purchase
  assert.equal(cf.financing.toDecimalString(), "100.00"); // loan proceeds

  assert.equal(cf.reconciled, true);
  assert.doesNotThrow(() => assertCashFlowReconciles(cf));

  // Sections sum to the net change.
  assert.equal(
    cf.operating.plus(cf.investing).plus(cf.financing).equals(cf.netChange),
    true,
  );
});
