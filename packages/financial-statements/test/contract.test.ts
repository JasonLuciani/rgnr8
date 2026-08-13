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
  balanceSheet,
  cashFlow,
  financialStatementsJson,
  financialStatementsJsonString,
  fromKernelTrialBalance,
  incomeStatement,
  minorToNumber,
  type ContractLine,
} from "../src/index.js";

// --- fixture chart of accounts (mirrors statements.test.ts) -----------------

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

const opening = [{ key: "o1", lines: [dr("cash", 50000n), cr("capital", 50000n)] }];
const activity = [
  { key: "a1", lines: [dr("ar", 30000n), cr("rev", 30000n)] },
  { key: "a2", lines: [dr("cash", 20000n), cr("ar", 20000n)] },
  { key: "a3", lines: [dr("exp", 12000n), cr("cash", 12000n)] },
  { key: "a4", lines: [dr("exp", 5000n), cr("ap", 5000n)] },
  { key: "a5", lines: [dr("equip", 8000n), cr("cash", 8000n)] },
  { key: "a6", lines: [dr("cash", 10000n), cr("loan", 10000n)] },
];

function isContractLineArray(v: unknown): v is ContractLine[] {
  return (
    Array.isArray(v) &&
    v.every(
      (l) =>
        typeof l === "object" &&
        l !== null &&
        typeof (l as ContractLine).label === "string" &&
        typeof (l as ContractLine).amount_minor === "number" &&
        Number.isInteger((l as ContractLine).amount_minor),
    )
  );
}

async function buildStatements() {
  const tbStart = fromKernelTrialBalance(await ledgerFrom(opening));
  const tbEnd = fromKernelTrialBalance(await ledgerFrom([...opening, ...activity]));
  const income = incomeStatement(tbEnd);
  const bs = balanceSheet(tbEnd, income.netIncome);
  const cf = cashFlow(tbStart, tbEnd, income.netIncome);
  return { tbStart, tbEnd, income, bs, cf };
}

test("financialStatementsJson emits the exact financial-statements/1 shape", async () => {
  const { income, bs, cf } = await buildStatements();
  const json = financialStatementsJson({
    period: "2026-08",
    currency: "USD",
    income,
    balanceSheet: bs,
    cashFlow: cf,
  });

  assert.equal(json.contract, "financial-statements/1");
  assert.equal(json.period, "2026-08");
  assert.equal(json.currency, "USD");

  // Line arrays are all {label:string, amount_minor:number(int)}.
  assert.ok(isContractLineArray(json.income_statement.revenue));
  assert.ok(isContractLineArray(json.income_statement.expenses));
  assert.ok(isContractLineArray(json.balance_sheet.assets));
  assert.ok(isContractLineArray(json.balance_sheet.liabilities));
  assert.ok(isContractLineArray(json.balance_sheet.equity));
  assert.ok(isContractLineArray(json.cash_flow.operating));
  assert.ok(isContractLineArray(json.cash_flow.investing));
  assert.ok(isContractLineArray(json.cash_flow.financing));
});

test("contract totals match the statement totals (minor units as numbers)", async () => {
  const { income, bs, cf } = await buildStatements();
  const json = financialStatementsJson({
    period: "2026-08",
    currency: "USD",
    income,
    balanceSheet: bs,
    cashFlow: cf,
  });

  // Income statement totals.
  assert.equal(json.income_statement.total_revenue, minorToNumber(income.revenue));
  assert.equal(json.income_statement.total_expenses, minorToNumber(income.expenses));
  assert.equal(json.income_statement.net_income, minorToNumber(income.netIncome));
  assert.equal(json.income_statement.total_revenue, 30000); // $300.00
  assert.equal(json.income_statement.total_expenses, 17000); // $170.00
  assert.equal(json.income_statement.net_income, 13000); // $130.00

  // Balance sheet totals + balanced flag.
  assert.equal(json.balance_sheet.total_assets, minorToNumber(bs.assets));
  assert.equal(json.balance_sheet.total_liabilities, minorToNumber(bs.liabilities));
  assert.equal(json.balance_sheet.total_equity, minorToNumber(bs.equity));
  assert.equal(json.balance_sheet.total_assets, 78000); // $780.00
  assert.equal(json.balance_sheet.total_liabilities, 15000); // $150.00
  assert.equal(json.balance_sheet.total_equity, 63000); // $630.00
  assert.equal(json.balance_sheet.balanced, true);

  // The revenue/expense line amounts sum to the reported totals.
  const revSum = json.income_statement.revenue.reduce((a, l) => a + l.amount_minor, 0);
  const expSum = json.income_statement.expenses.reduce((a, l) => a + l.amount_minor, 0);
  assert.equal(revSum, json.income_statement.total_revenue);
  assert.equal(expSum, json.income_statement.total_expenses);

  // Balance sheet line amounts sum to totals (equity includes the NI line).
  const assetSum = json.balance_sheet.assets.reduce((a, l) => a + l.amount_minor, 0);
  const equitySum = json.balance_sheet.equity.reduce((a, l) => a + l.amount_minor, 0);
  assert.equal(assetSum, json.balance_sheet.total_assets);
  assert.equal(equitySum, json.balance_sheet.total_equity);
});

test("cash_flow.net_change equals the cash-account delta and ties to sections", async () => {
  const { tbStart, tbEnd, income, bs, cf } = await buildStatements();
  const json = financialStatementsJson({
    period: "2026-08",
    currency: "USD",
    income,
    balanceSheet: bs,
    cashFlow: cf,
  });

  // Independent cash-account movement from the two trial balances.
  const cashStart = tbStart.entries.find((e) => e.code === "1000")!.signed;
  const cashEnd = tbEnd.entries.find((e) => e.code === "1000")!.signed;
  const cashDelta = Number((cashEnd.minus(cashStart)).minorUnits);

  assert.equal(json.cash_flow.net_change, cashDelta);
  assert.equal(json.cash_flow.net_change, 10000); // $100.00
  assert.equal(json.cash_flow.ending_cash, minorToNumber(cf.endingCash));
  assert.equal(json.cash_flow.ending_cash, 60000); // $600.00

  // Sections sum to net change.
  const sections =
    json.cash_flow.operating.reduce((a, l) => a + l.amount_minor, 0) +
    json.cash_flow.investing.reduce((a, l) => a + l.amount_minor, 0) +
    json.cash_flow.financing.reduce((a, l) => a + l.amount_minor, 0);
  assert.equal(sections, json.cash_flow.net_change);
});

test("contract round-trips through JSON.parse(JSON.stringify(...))", async () => {
  const { income, bs, cf } = await buildStatements();
  const json = financialStatementsJson({
    period: "2026-08",
    currency: "USD",
    income,
    balanceSheet: bs,
    cashFlow: cf,
  });

  const roundTripped = JSON.parse(JSON.stringify(json));
  assert.deepEqual(roundTripped, json);
  assert.equal(roundTripped.contract, "financial-statements/1");

  // The sorted-key string producer also round-trips to the same object.
  const str = financialStatementsJsonString({
    period: "2026-08",
    currency: "USD",
    income,
    balanceSheet: bs,
    cashFlow: cf,
  });
  assert.deepEqual(JSON.parse(str), json);
});

test("minorToNumber rejects values beyond Number.MAX_SAFE_INTEGER", () => {
  const safe = Money.fromMinorUnits(BigInt(Number.MAX_SAFE_INTEGER), USD);
  assert.equal(minorToNumber(safe), Number.MAX_SAFE_INTEGER);
  const tooBig = Money.fromMinorUnits(BigInt(Number.MAX_SAFE_INTEGER) + 1n, USD);
  assert.throws(() => minorToNumber(tooBig), RangeError);
});
