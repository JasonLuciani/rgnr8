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
  QboApiError,
  buildChartFromQbo,
  compareTrialBalances,
  importQbo,
  type QboHttpClient,
  type QboHttpResponse,
} from "../src/index.js";

// --- a fake QBO API: route by URL to fixture JSON ---------------------------

const ACCOUNTS_JSON = {
  QueryResponse: {
    Account: [
      { Name: "Checking", AcctNum: "1000", AccountType: "Bank" },
      { Name: "Accounts Receivable", AcctNum: "1200", AccountType: "Accounts Receivable" },
      { Name: "Owner Equity", AcctNum: "3000", AccountType: "Equity" },
      { Name: "Sales", AcctNum: "4000", AccountType: "Income" },
      { Name: "Rent Expense", AcctNum: "6000", AccountType: "Expense" },
    ],
  },
};

const TRIAL_BALANCE_JSON = {
  Header: { ReportName: "TrialBalance" },
  Columns: { Column: [{ ColTitle: "" }, { ColTitle: "Debit" }, { ColTitle: "Credit" }] },
  Rows: {
    Row: [
      { ColData: [{ value: "Checking", id: "35" }, { value: "46000.00" }, { value: "" }], type: "Data" },
      { ColData: [{ value: "Accounts Receivable", id: "84" }, { value: "12000.00" }, { value: "" }], type: "Data" },
      { ColData: [{ value: "Sales", id: "1" }, { value: "" }, { value: "12000.00" }], type: "Data" },
      { ColData: [{ value: "Rent Expense", id: "7" }, { value: "4000.00" }, { value: "" }], type: "Data" },
      { ColData: [{ value: "Owner Equity", id: "3" }, { value: "" }, { value: "50000.00" }], type: "Data" },
      { Summary: { ColData: [{ value: "TOTAL" }, { value: "62000.00" }, { value: "62000.00" }] }, type: "Section" },
    ],
  },
};

const JOURNAL_JSON = {
  QueryResponse: {
    JournalEntry: [
      {
        Id: "148",
        TxnDate: "2026-08-01",
        Line: [
          { DetailType: "JournalEntryLineDetail", Amount: 50000.0, JournalEntryLineDetail: { PostingType: "Debit", AccountRef: { name: "Checking" } } },
          { DetailType: "JournalEntryLineDetail", Amount: 50000.0, JournalEntryLineDetail: { PostingType: "Credit", AccountRef: { name: "Owner Equity" } } },
        ],
      },
    ],
  },
};

function fakeQbo(): QboHttpClient {
  return {
    async get(url: string): Promise<QboHttpResponse> {
      if (url.includes("from%20Account") || url.includes("from Account")) return { status: 200, json: ACCOUNTS_JSON };
      if (url.includes("reports/TrialBalance")) return { status: 200, json: TRIAL_BALANCE_JSON };
      if (url.includes("from%20JournalEntry") || url.includes("from JournalEntry")) return { status: 200, json: JOURNAL_JSON };
      return { status: 404, json: {} };
    },
  };
}

const OPTS = { realmId: "9130347", accessToken: "tok-abc" };

test("fetchAccounts maps QBO account list + normalizes types", async () => {
  const client = new QboApiClient(fakeQbo(), OPTS);
  const accts = await client.fetchAccounts();
  assert.equal(accts.length, 5);
  const chk = accts.find((a) => a.name === "Checking");
  assert.equal(chk?.acctNum, "1000");
  assert.equal(chk?.type, "Bank");
  assert.equal(accts.find((a) => a.name === "Sales")?.type, "Income");
});

test("fetchTrialBalance parses report rows and drops the TOTAL summary", async () => {
  const client = new QboApiClient(fakeQbo(), OPTS);
  const tb = await client.fetchTrialBalance();
  assert.equal(tb.length, 5);
  assert.equal(tb.find((r) => r.account === "Checking")?.debit, "46000.00");
  assert.equal(tb.find((r) => r.account === "Sales")?.credit, "12000.00");
});

test("fetchJournalEntries maps debit/credit posting lines", async () => {
  const client = new QboApiClient(fakeQbo(), OPTS);
  const entries = await client.fetchJournalEntries();
  assert.equal(entries.length, 1);
  assert.equal(entries[0]?.id, "JE-148");
  assert.equal(entries[0]?.lines[0]?.account, "Checking");
  assert.equal(entries[0]?.lines[0]?.debit, "50000");
  assert.equal(entries[0]?.lines[1]?.credit, "50000");
});

test("fetchExport assembles a QboExport whose reported TB drives the parallel-close compare", async () => {
  const client = new QboApiClient(fakeQbo(), OPTS);
  const exp = await client.fetchExport();
  assert.equal(exp.accounts.length, 5);
  assert.ok(exp.reportedTrialBalance && exp.reportedTrialBalance.length === 5);

  // import the manual JE into a ledger, then compare our TB to QBO's reported TB
  const chart = buildChartFromQbo(exp.accounts, USD);
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart.coa, store, new PeriodRegistry());
  await importQbo(engine, exp, chart, { tenantId: asTenantId("acme"), currency: USD, ingestedAt: "2026-09-01T00:00:00Z" }, "2026-09-01T00:00:00Z");
  const ourTb = await computeTrialBalance(store, asTenantId("acme"), chart.coa, USD);
  const diff = compareTrialBalances(ourTb, exp.reportedTrialBalance!, { currency: USD });
  // only the opening JE was imported here, so QBO's full TB legitimately shows more —
  // the compare surfaces exactly which accounts differ (proving the plumbing works).
  assert.equal(diff.rows.length >= 5, true);
  assert.equal(diff.rows.find((r) => r.account === "Owner Equity")?.status, "match");
});

test("a non-2xx response raises QboApiError", async () => {
  const http: QboHttpClient = { async get() { return { status: 401, json: { fault: "unauthorized" } }; } };
  const client = new QboApiClient(http, OPTS);
  await assert.rejects(() => client.fetchAccounts(), (e) => e instanceof QboApiError && e.status === 401);
});
