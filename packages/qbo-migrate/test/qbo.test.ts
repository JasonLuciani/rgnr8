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
  mapQboType,
  renderDiffHtml,
  toPostCommands,
  type QboExport,
} from "../src/index.js";

const TENANT = asTenantId("acme");
const OPTS = { tenantId: TENANT, currency: USD, ingestedAt: "2026-08-06T00:00:00Z" };

function sampleExport(): QboExport {
  return {
    accounts: [
      { name: "Checking", acctNum: "1000", type: "Bank" },
      { name: "Accounts Receivable", acctNum: "1200", type: "Accounts Receivable" },
      { name: "Sales", acctNum: "4000", type: "Income" },
      { name: "Rent Expense", acctNum: "6000", type: "Expense" },
      { name: "Owner Equity", acctNum: "3000", type: "Equity" },
    ],
    entries: [
      // opening equity into checking
      { id: "e1", date: "2026-07-01", lines: [
        { account: "Checking", debit: "50000.00" },
        { account: "Owner Equity", credit: "50000.00" },
      ] },
      // a sale on account
      { id: "e2", date: "2026-07-05", lines: [
        { account: "Accounts Receivable", debit: "12000.00" },
        { account: "Sales", credit: "12000.00" },
      ] },
      // pay rent
      { id: "e3", date: "2026-07-10", lines: [
        { account: "Rent Expense", debit: "4000.00" },
        { account: "Checking", credit: "4000.00" },
      ] },
    ],
    reportedTrialBalance: [
      { account: "Checking", debit: "46000.00" },
      { account: "Accounts Receivable", debit: "12000.00" },
      { account: "Rent Expense", debit: "4000.00" },
      { account: "Sales", credit: "12000.00" },
      { account: "Owner Equity", credit: "50000.00" },
    ],
  };
}

test("mapQboType collapses QBO types onto the kernel's five", () => {
  assert.equal(mapQboType("Bank"), "ASSET");
  assert.equal(mapQboType("Accounts Payable"), "LIABILITY");
  assert.equal(mapQboType("Credit Card"), "LIABILITY");
  assert.equal(mapQboType("Income"), "REVENUE");
  assert.equal(mapQboType("Cost of Goods Sold"), "EXPENSE");
  assert.equal(mapQboType("Equity"), "EQUITY");
});

test("buildChartFromQbo maps names→ids and uses acctNum as the code", () => {
  const { coa, byName } = buildChartFromQbo(sampleExport().accounts, USD);
  assert.equal(coa.list().length, 5);
  const chk = coa.getByCode("1000");
  assert.equal(chk?.name, "Checking");
  assert.equal(chk?.type, "ASSET");
  assert.ok(byName.get("Sales"));
});

test("toPostCommands builds balanced commands and collects bad rows as issues", () => {
  const chart = buildChartFromQbo(sampleExport().accounts, USD);
  const exp: QboExport = {
    accounts: sampleExport().accounts,
    entries: [
      ...sampleExport().entries,
      { id: "bad-unbalanced", date: "2026-07-11", lines: [
        { account: "Checking", debit: "10.00" },
        { account: "Sales", credit: "9.00" },
      ] },
      { id: "bad-unknown", date: "2026-07-12", lines: [
        { account: "Nonexistent", debit: "1.00" },
        { account: "Sales", credit: "1.00" },
      ] },
    ],
  };
  const { commands, issues } = toPostCommands(exp, chart, OPTS);
  assert.equal(commands.length, 3); // the three good entries
  const kinds = issues.map((i) => i.kind).sort();
  assert.deepEqual(kinds, ["unbalanced", "unknown_account"]);
  // period key derived from the date
  assert.equal(commands[0]?.periodKey as unknown as string, "2026-07");
});

async function importSample(exp: QboExport) {
  const chart = buildChartFromQbo(exp.accounts, USD);
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart.coa, store, new PeriodRegistry());
  const report = await importQbo(engine, exp, chart, OPTS, "2026-08-06T00:00:00Z");
  const tb = await computeTrialBalance(store, TENANT, chart.coa, USD);
  return { chart, store, engine, report, tb };
}

test("importQbo posts into the ledger and the trial balance ties out", async () => {
  const exp = sampleExport();
  const { report, tb } = await importSample(exp);
  assert.equal(report.posted, 3);
  assert.equal(report.skipped, 0);
  assert.deepEqual(report.periods, ["2026-07"]);
  assert.equal(tb.inBalance, true); // debits == credits
});

test("parallel-close compare: imported ledger agrees with QBO's reported TB", async () => {
  const exp = sampleExport();
  const { tb } = await importSample(exp);
  const diff = compareTrialBalances(tb, exp.reportedTrialBalance!, { currency: USD });
  assert.equal(diff.inAgreement, true, JSON.stringify(diff.mismatches));
  assert.equal(diff.totalAbsDelta.isZero(), true);
  assert.equal(diff.matched, 5);
});

test("compare flags a mismatch and an account only on one side", async () => {
  const exp = sampleExport();
  const { tb } = await importSample(exp);
  const tampered = [
    { account: "Checking", debit: "46000.01" }, // off by a penny
    { account: "Accounts Receivable", debit: "12000.00" },
    { account: "Rent Expense", debit: "4000.00" },
    { account: "Sales", credit: "12000.00" },
    { account: "Owner Equity", credit: "50000.00" },
    { account: "Suspense", debit: "0.01" }, // only in QBO
  ];
  const diff = compareTrialBalances(tb, tampered, { currency: USD });
  assert.equal(diff.inAgreement, false);
  assert.equal(diff.mismatches.length, 1);
  assert.equal(diff.mismatches[0]?.account, "Checking");
  assert.equal(diff.onlyInQbo.length, 1);
  assert.equal(diff.onlyInQbo[0]?.account, "Suspense");
});

test("a penny tolerance absorbs a rounding difference", async () => {
  const exp = sampleExport();
  const { tb } = await importSample(exp);
  const off = exp.reportedTrialBalance!.map((r) =>
    r.account === "Checking" ? { account: "Checking", debit: "46000.01" } : r,
  );
  const diff = compareTrialBalances(tb, off, { currency: USD, toleranceMinor: 1n });
  assert.equal(diff.inAgreement, true);
});

test("re-importing the same export is idempotent (no double-posting)", async () => {
  const exp = sampleExport();
  const chart = buildChartFromQbo(exp.accounts, USD);
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart.coa, store, new PeriodRegistry());
  await importQbo(engine, exp, chart, OPTS, "2026-08-06T00:00:00Z");
  await importQbo(engine, exp, chart, OPTS, "2026-08-06T00:00:00Z");
  const tb = await computeTrialBalance(store, TENANT, chart.coa, USD);
  const checking = tb.rows.find((r) => r.name === "Checking");
  assert.equal(checking?.debit.toDecimalString(), "46000.00"); // not doubled
});

test("renderDiffHtml renders an agreement banner and a row per account", async () => {
  const exp = sampleExport();
  const { tb } = await importSample(exp);
  const html = renderDiffHtml(compareTrialBalances(tb, exp.reportedTrialBalance!, { currency: USD }));
  assert.match(html, /<!doctype html>/);
  assert.match(html, /ties to QuickBooks/);
  assert.match(html, /Checking/);
});
