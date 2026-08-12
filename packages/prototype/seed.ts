/**
 * Prototype seed — the TypeScript accounting half of the end-to-end demo.
 *
 * Takes a sample QuickBooks CSV export for a demo business, imports it into the
 * ledger, computes statements, runs the month-end close gate, seals an immutable
 * financial package, and runs the parallel-close diff against QBO. Writes the
 * artifacts the Python half reads/serves:
 *   out/package.json            the sealed FinancialPackage (Python re-verifies its fingerprint)
 *   out/seed_summary.json       tenant + period + cash + net income + fingerprint
 *   out/qbo_trial_diff.html     RGNR8 vs QBO trial balance (should tie to the penny)
 *   out/qbo_statements_diff.html RGNR8 vs QBO P&L + balance sheet
 */

import { writeFileSync, mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  ChartOfAccounts,
  InMemoryLedgerStore,
  PeriodRegistry,
  PostingEngine,
  USD,
  asPeriodKey,
  asTenantId,
  computeTrialBalance,
} from "@rgnr8/ledger-kernel";
import { computeBalanceSheet, computeIncomeStatement } from "@rgnr8/statements";
import { buildCloseChecklist, closePeriod, buildFinancialPackage } from "@rgnr8/close";
import {
  buildChartFromQbo,
  compareStatements,
  compareTrialBalances,
  importQbo,
  parseQboCsvExport,
  renderDiffHtml,
  renderStatementsDiffHtml,
} from "@rgnr8/qbo-migrate";

const OUT = join(dirname(fileURLToPath(import.meta.url)), "out");
mkdirSync(OUT, { recursive: true });

const TENANT = "bright";
const TENANT_NAME = "Bright Agency";
const PERIOD = "2026-08";

// --- a demo QuickBooks Online export (three CSV reports) ---------------------
const ACCOUNTS_CSV = `Chart of Accounts
Account name,Account number,Type
Checking,1000,Bank
Accounts Receivable,1200,Accounts receivable (A/R)
Owner Equity,3000,Equity
Sales,4000,Income
Rent Expense,6000,Expenses
Payroll Expense,6100,Expenses
`;

const JOURNAL_CSV = `Journal
Date,Transaction Type,Num,Name,Memo/Description,Account,Debit,Credit
2026-08-01,Journal Entry,1,,Opening balance,Checking,"80,000.00",
,,,,,Owner Equity,,"80,000.00"
2026-08-03,Invoice,1001,Northwind,Retainer,Accounts Receivable,"24,000.00",
,,,,,Sales,,"24,000.00"
2026-08-06,Check,201,Staff,August payroll,Payroll Expense,"16,000.00",
,,,,,Checking,,"16,000.00"
2026-08-10,Expense,55,Landlord,August rent,Rent Expense,"5,000.00",
,,,,,Checking,,"5,000.00"
2026-08-15,Payment,1001,Northwind,Invoice payment,Checking,"12,000.00",
,,,,,Accounts Receivable,,"12,000.00"
`;

const TRIAL_BALANCE_CSV = `Trial Balance
,Debit,Credit
Checking,"71,000.00",
Accounts Receivable,"12,000.00",
Payroll Expense,"16,000.00",
Rent Expense,"5,000.00",
Sales,,"24,000.00"
Owner Equity,,"80,000.00"
TOTAL,"104,000.00","104,000.00"
`;

async function main(): Promise<void> {
  const tenant = asTenantId(TENANT);
  const exp = parseQboCsvExport({
    accountsCsv: ACCOUNTS_CSV,
    journalCsv: JOURNAL_CSV,
    trialBalanceCsv: TRIAL_BALANCE_CSV,
  });
  const chart = buildChartFromQbo(exp.accounts, USD);
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart.coa, store, new PeriodRegistry());
  const importReport = await importQbo(
    engine,
    exp,
    chart,
    { tenantId: tenant, currency: USD, ingestedAt: "2026-09-01T00:00:00Z" },
    "2026-09-01T00:00:00Z",
  );

  const tb = await computeTrialBalance(store, tenant, chart.coa, USD);
  const is = await computeIncomeStatement(store, tenant, chart.coa, "2026-08-01", "2026-08-31", USD);
  const bs = await computeBalanceSheet(store, tenant, chart.coa, "2026-08-31", USD);

  // --- parallel-close compare vs QBO ---------------------------------------
  const tbDiff = compareTrialBalances(tb, exp.reportedTrialBalance ?? [], { currency: USD });
  const stmtDiff = compareStatements(
    is,
    bs,
    {
      income: { revenue: "24000.00", expenses: "21000.00", netIncome: "3000.00" },
      balance: { totalAssets: "83000.00", totalLiabilitiesAndEquity: "83000.00" },
    },
    { currency: USD },
  );
  writeFileSync(join(OUT, "qbo_trial_diff.html"), renderDiffHtml(tbDiff));
  writeFileSync(join(OUT, "qbo_statements_diff.html"), renderStatementsDiffHtml(stmtDiff));

  // --- month-end close gate + seal the immutable package -------------------
  const closeInputs = {
    reconciliations: [{ accountId: "1000", status: "BALANCED", signOff: { by: "controller" } }],
    controls: [{ name: "AR control", balanced: true }],
    trialBalanceBalanced: tb.inBalance,
    requireSignOff: true,
  };
  buildCloseChecklist(closeInputs); // (checklist also embedded in the ClosePackage)
  const closePkg = closePeriod(new PeriodRegistry(), asPeriodKey(PERIOD), closeInputs, {
    closedBy: "controller@bright",
    at: "2026-09-01T17:00:00Z",
  });

  const cashRow = tb.rows.find((r) => r.code === "1000");
  const pkg = buildFinancialPackage(
    {
      periodKey: PERIOD,
      currency: "USD",
      trialBalance: {
        rows: tb.rows.map((r) => ({
          code: r.code,
          name: r.name,
          debitMinor: r.debit.minorUnits.toString(),
          creditMinor: r.credit.minorUnits.toString(),
        })),
        totalDebitMinor: tb.totalDebit.minorUnits.toString(),
        totalCreditMinor: tb.totalCredit.minorUnits.toString(),
        inBalance: tb.inBalance,
      },
      incomeStatement: {
        revenueMinor: is.revenue.total.minorUnits.toString(),
        expensesMinor: is.expenses.total.minorUnits.toString(),
        netIncomeMinor: is.netIncome.minorUnits.toString(),
      },
      balanceSheet: {
        totalAssetsMinor: bs.totalAssets.minorUnits.toString(),
        totalLiabilitiesAndEquityMinor: bs.totalLiabilitiesAndEquity.minorUnits.toString(),
        netIncomeMinor: bs.netIncome.minorUnits.toString(),
        balances: bs.balances,
      },
      qboReconciliation: {
        inAgreement: tbDiff.inAgreement && stmtDiff.inAgreement,
        totalAbsDeltaMinor: tbDiff.totalAbsDelta.minorUnits.toString(),
        mismatchCount: tbDiff.mismatches.length,
        onlyInRgnr8Count: tbDiff.onlyInRgnr8.length,
        onlyInQboCount: tbDiff.onlyInQbo.length,
      },
    },
    { closedBy: "controller@bright", closedAt: "2026-09-01T17:00:00Z", packagedAt: "2026-09-01T17:00:05Z" },
  );

  writeFileSync(join(OUT, "package.json"), JSON.stringify(pkg));
  writeFileSync(
    join(OUT, "seed_summary.json"),
    JSON.stringify({
      tenantId: TENANT,
      tenantName: TENANT_NAME,
      periodKey: PERIOD,
      openingCashMinor: cashRow ? cashRow.debit.minorUnits.toString() : "0",
      netIncomeMinor: is.netIncome.minorUnits.toString(),
      fingerprint: pkg.fingerprint,
      imported: importReport.posted,
      importIssues: importReport.issues.length,
      closeReady: closePkg.ready,
      qboTiesOut: tbDiff.inAgreement && stmtDiff.inAgreement,
    }),
  );

  console.log(
    `[seed] imported ${importReport.posted} entries · trial balance ${tb.inBalance ? "balances" : "OUT OF BALANCE"} · ` +
      `QBO ${tbDiff.inAgreement && stmtDiff.inAgreement ? "ties to the penny" : "DIVERGES"} · ` +
      `close ${closePkg.ready ? "ready" : "blocked"} · package sealed (${pkg.fingerprint.slice(0, 12)}…)`,
  );
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
