import { test } from "node:test";
import assert from "node:assert/strict";
import {
  InMemoryLedgerStore,
  PeriodRegistry,
  PostingEngine,
  USD,
  asTenantId,
  computeTrialBalance,
} from "@rgnr8/ledger-kernel";
import {
  buildChartFromQbo,
  compareTrialBalances,
  importQbo,
  normalizeQboAccountType,
  parseAccountsCsv,
  parseAmount,
  parseCsv,
  parseJournalCsv,
  parseQboCsvExport,
  parseTrialBalanceCsv,
} from "../src/index.js";

const ACCOUNTS_CSV = `Chart of Accounts
Account name,Account number,Type
Checking,1000,Bank
Accounts Receivable,1200,Accounts receivable (A/R)
Owner Equity,3000,Equity
Sales,4000,Income
Rent Expense,6000,Expenses
`;

const JOURNAL_CSV = `Journal
Date,Transaction Type,Num,Name,Memo/Description,Account,Debit,Credit
2026-07-01,Journal Entry,1,,Opening,Checking,"50,000.00",
,,,,,Owner Equity,,"50,000.00"
2026-07-05,Invoice,1001,Acme,,Accounts Receivable,"12,000.00",
,,,,,Sales,,"12,000.00"
2026-07-10,Expense,55,Landlord,July rent,Rent Expense,"4,000.00",
,,,,,Checking,,"4,000.00"
,,,,,,,
TOTAL,,,,,,"66,000.00","66,000.00"
`;

const TB_CSV = `Trial Balance
,Debit,Credit
Checking,"46,000.00",
Accounts Receivable,"12,000.00",
Sales,,"12,000.00"
Rent Expense,"4,000.00",
Owner Equity,,"50,000.00"
TOTAL,"62,000.00","62,000.00"
`;

test("parseCsv handles quoted commas and skips blank rows", () => {
  const rows = parseCsv(`a,b\n"1,000",x\n\n"y","z"\n`);
  assert.deepEqual(rows, [
    ["a", "b"],
    ["1,000", "x"],
    ["y", "z"],
  ]);
});

test("parseAmount strips thousands separators and currency", () => {
  assert.equal(parseAmount('"1,234.56"'.replace(/"/g, "")), "1234.56");
  assert.equal(parseAmount("$2,000.00"), "2000.00");
  assert.equal(parseAmount("(50.00)"), "-50.00");
  assert.equal(parseAmount(""), undefined);
  assert.equal(parseAmount("  "), undefined);
});

test("normalizeQboAccountType maps QBO's verbose type strings", () => {
  assert.equal(normalizeQboAccountType("Bank"), "Bank");
  assert.equal(normalizeQboAccountType("Accounts receivable (A/R)"), "Accounts Receivable");
  assert.equal(normalizeQboAccountType("Expenses"), "Expense");
  assert.equal(normalizeQboAccountType("Income"), "Income");
  assert.equal(normalizeQboAccountType("Cost of Goods Sold"), "Cost of Goods Sold");
  assert.equal(normalizeQboAccountType("Long Term Liabilities"), "Long Term Liability");
});

test("parseAccountsCsv reads names, numbers, and mapped types", () => {
  const accts = parseAccountsCsv(ACCOUNTS_CSV);
  assert.equal(accts.length, 5);
  const checking = accts.find((a) => a.name === "Checking");
  assert.equal(checking?.acctNum, "1000");
  assert.equal(checking?.type, "Bank");
  assert.equal(accts.find((a) => a.name === "Sales")?.type, "Income");
});

test("parseJournalCsv groups lines into balanced entries and drops totals", () => {
  const entries = parseJournalCsv(JOURNAL_CSV);
  assert.equal(entries.length, 3);
  const opening = entries[0];
  assert.equal(opening?.date, "2026-07-01");
  assert.equal(opening?.lines.length, 2);
  assert.equal(opening?.lines[0]?.account, "Checking");
  assert.equal(opening?.lines[0]?.debit, "50000.00");
  assert.equal(opening?.lines[1]?.credit, "50000.00");
  // every parsed entry balances
  for (const e of entries) {
    const d = e.lines.reduce((s, l) => s + Number(l.debit ?? 0), 0);
    const c = e.lines.reduce((s, l) => s + Number(l.credit ?? 0), 0);
    assert.equal(d.toFixed(2), c.toFixed(2));
  }
});

test("parseTrialBalanceCsv reads rows and skips the TOTAL line", () => {
  const tb = parseTrialBalanceCsv(TB_CSV);
  assert.equal(tb.length, 5);
  assert.equal(tb.find((r) => r.account === "Checking")?.debit, "46000.00");
  assert.equal(tb.find((r) => r.account === "Sales")?.credit, "12000.00");
});

test("full CSV export imports into the ledger and ties to QBO's reported TB", async () => {
  const exp = parseQboCsvExport({ accountsCsv: ACCOUNTS_CSV, journalCsv: JOURNAL_CSV, trialBalanceCsv: TB_CSV });
  const chart = buildChartFromQbo(exp.accounts, USD);
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart.coa, store, new PeriodRegistry());
  const report = await importQbo(
    engine,
    exp,
    chart,
    { tenantId: asTenantId("acme"), currency: USD, ingestedAt: "2026-08-06T00:00:00Z" },
    "2026-08-06T00:00:00Z",
  );
  assert.equal(report.posted, 3);
  assert.equal(report.skipped, 0);

  const tb = await computeTrialBalance(store, asTenantId("acme"), chart.coa, USD);
  const diff = compareTrialBalances(tb, exp.reportedTrialBalance!, { currency: USD });
  assert.equal(diff.inAgreement, true, JSON.stringify(diff.mismatches.concat(diff.onlyInQbo)));
});
