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
  QboApiClient,
  buildChartFromQbo,
  compareTrialBalances,
  importQbo,
  parseGeneralLedger,
  toPostCommands,
  type QboHttpClient,
  type QboHttpResponse,
  type QboExport,
} from "../src/index.js";

// A GeneralLedger report: three transactions across four accounts. Each txn
// appears once per account it touches, with a signed amount (debit +, credit −).
const GL_REPORT = {
  Header: { ReportName: "GeneralLedger" },
  Columns: {
    Column: [{ ColTitle: "Date" }, { ColTitle: "Transaction Type" }, { ColTitle: "Num" }, { ColTitle: "Amount" }, { ColTitle: "Balance" }],
  },
  Rows: {
    Row: [
      {
        Header: { ColData: [{ value: "Checking" }] },
        Rows: {
          Row: [
            { ColData: [{ value: "2026-08-01" }, { value: "Journal Entry", id: "148" }, { value: "1" }, { value: "80000.00" }, { value: "80000.00" }] },
            { ColData: [{ value: "2026-08-10" }, { value: "Expense", id: "150" }, { value: "55" }, { value: "-5000.00" }, { value: "75000.00" }] },
          ],
        },
        Summary: { ColData: [{ value: "Total Checking" }, {}, {}, { value: "75000.00" }] },
        type: "Section",
      },
      {
        Header: { ColData: [{ value: "Owner Equity" }] },
        Rows: { Row: [{ ColData: [{ value: "2026-08-01" }, { value: "Journal Entry", id: "148" }, { value: "1" }, { value: "-80000.00" }, { value: "-80000.00" }] }] },
        type: "Section",
      },
      {
        Header: { ColData: [{ value: "Accounts Receivable" }] },
        Rows: { Row: [{ ColData: [{ value: "2026-08-03" }, { value: "Invoice", id: "149" }, { value: "1001" }, { value: "12000.00" }, { value: "12000.00" }] }] },
        type: "Section",
      },
      {
        Header: { ColData: [{ value: "Sales" }] },
        Rows: { Row: [{ ColData: [{ value: "2026-08-03" }, { value: "Invoice", id: "149" }, { value: "1001" }, { value: "-12000.00" }, { value: "-12000.00" }] }] },
        type: "Section",
      },
      {
        Header: { ColData: [{ value: "Rent Expense" }] },
        Rows: { Row: [{ ColData: [{ value: "2026-08-10" }, { value: "Expense", id: "150" }, { value: "55" }, { value: "5000.00" }, { value: "5000.00" }] }] },
        type: "Section",
      },
    ],
  },
};

const ACCOUNTS = [
  { name: "Checking", acctNum: "1000", type: "Bank" as const },
  { name: "Owner Equity", acctNum: "3000", type: "Equity" as const },
  { name: "Accounts Receivable", acctNum: "1200", type: "Accounts Receivable" as const },
  { name: "Sales", acctNum: "4000", type: "Income" as const },
  { name: "Rent Expense", acctNum: "6000", type: "Expense" as const },
];

test("parseGeneralLedger groups by transaction id into balanced entries", () => {
  const entries = parseGeneralLedger(GL_REPORT);
  assert.equal(entries.length, 3); // txns 148, 149, 150

  const opening = entries.find((e) => e.id === "GL-148");
  assert.equal(opening?.lines.length, 2);
  assert.equal(opening?.lines.find((l) => l.account === "Checking")?.debit, "80000.00");
  assert.equal(opening?.lines.find((l) => l.account === "Owner Equity")?.credit, "80000.00");

  // every reconstructed entry balances (debits === credits)
  for (const e of entries) {
    const d = e.lines.reduce((s, l) => s + Number(l.debit ?? 0), 0);
    const c = e.lines.reduce((s, l) => s + Number(l.credit ?? 0), 0);
    assert.equal(d.toFixed(2), c.toFixed(2), `entry ${e.id} balances`);
  }
});

test("GL entries import into the ledger with no issues and tie to the reported TB", async () => {
  const exp: QboExport = {
    accounts: ACCOUNTS,
    entries: parseGeneralLedger(GL_REPORT),
    reportedTrialBalance: [
      { account: "Checking", debit: "75000.00" },
      { account: "Accounts Receivable", debit: "12000.00" },
      { account: "Rent Expense", debit: "5000.00" },
      { account: "Sales", credit: "12000.00" },
      { account: "Owner Equity", credit: "80000.00" },
    ],
  };
  const chart = buildChartFromQbo(exp.accounts, USD);

  // no bad rows dropped — every GL transaction reconstructed cleanly
  const { commands, issues } = toPostCommands(exp, chart, { tenantId: asTenantId("acme"), currency: USD, ingestedAt: "2026-09-01T00:00:00Z" });
  assert.equal(issues.length, 0);
  assert.equal(commands.length, 3);

  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart.coa, store, new PeriodRegistry());
  await importQbo(engine, exp, chart, { tenantId: asTenantId("acme"), currency: USD, ingestedAt: "2026-09-01T00:00:00Z" }, "2026-09-01T00:00:00Z");
  const tb = await computeTrialBalance(store, asTenantId("acme"), chart.coa, USD);
  const diff = compareTrialBalances(tb, exp.reportedTrialBalance!, { currency: USD });
  assert.equal(diff.inAgreement, true, JSON.stringify(diff.mismatches.concat(diff.onlyInQbo, diff.onlyInRgnr8)));
});

function fakeGlApi(): QboHttpClient {
  return {
    async get(url: string): Promise<QboHttpResponse> {
      if (url.includes("reports/GeneralLedger")) return { status: 200, json: GL_REPORT };
      if (url.includes("Account")) return { status: 200, json: { QueryResponse: { Account: ACCOUNTS.map((a) => ({ Name: a.name, AcctNum: a.acctNum, AccountType: a.type })) } } };
      if (url.includes("reports/TrialBalance")) return { status: 200, json: { Columns: { Column: [{ ColTitle: "" }, { ColTitle: "Debit" }, { ColTitle: "Credit" }] }, Rows: { Row: [] } } };
      return { status: 404, json: {} };
    },
  };
}

test("fetchExportViaGeneralLedger pulls a full-history export", async () => {
  const client = new QboApiClient(fakeGlApi(), { realmId: "9130347", accessToken: "tok" });
  const exp = await client.fetchExportViaGeneralLedger("2026-08-01", "2026-08-31");
  assert.equal(exp.accounts.length, 5);
  assert.equal(exp.entries.length, 3); // reconstructed from the GL
  assert.equal(exp.entries.every((e) => e.lines.length >= 1), true);
});
