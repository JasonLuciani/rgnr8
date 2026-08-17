import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountType,
  ChartOfAccounts,
  InMemoryLedgerStore,
  PeriodRegistry,
  PostingEngine,
  USD,
  accountBalances,
  asAccountId,
  asTenantId,
  type Account,
} from "@rgnr8/ledger-kernel";
import { QboLikeAdapter, defaultAccountMap } from "@rgnr8/ingestion";
import {
  ConnectorRunner,
  FakeHttpClient,
  InMemoryConnectionStore,
  LedgerPostingSink,
  QboSyncConnector,
  SyncRuntime,
  type Connection,
} from "../src/index.js";

const TENANT = asTenantId("acme");
const POST_AT = "2026-08-11T00:00:00Z";

function acct(code: string, type: AccountType): Account {
  return { id: asAccountId(`gl.${code}`), code, name: code, type, currency: USD };
}
function coa(): ChartOfAccounts {
  return new ChartOfAccounts([
    acct("cash", AccountType.ASSET),
    acct("income", AccountType.REVENUE),
    acct("expense", AccountType.EXPENSE),
    acct("fees", AccountType.EXPENSE),
    acct("payroll_expense", AccountType.EXPENSE),
    acct("payroll_tax", AccountType.EXPENSE),
    acct("interest_income", AccountType.REVENUE),
  ]);
}
function conn(over: Partial<Connection> = {}): Connection {
  return {
    id: "q1", provider: "qbo", tenantId: "acme",
    accessToken: "tok", baseUrl: "https://quickbooks.api.intuit.com",
    secrets: { realm_id: "realm-123" }, health: "NEW", ...over,
  };
}

const cdcPage = {
  CDCResponse: [
    {
      QueryResponse: [
        {
          Purchase: [
            { Id: "10", TxnDate: "2026-08-05", TotalAmt: 1200.0, AccountRef: { value: "chk" } },
            { Id: "11", status: "Deleted" }, // tombstone
          ],
        },
        {
          Deposit: [
            { Id: "20", TxnDate: "2026-08-06", TotalAmt: 5000.0, DepositToAccountRef: { value: "chk" } },
          ],
        },
      ],
    },
  ],
  time: "2026-08-11T00:00:00.000-07:00",
};

test("QBO CDC records flow through the pipeline and post to the ledger", async () => {
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(coa(), store, new PeriodRegistry());
  const sink = new LedgerPostingSink({
    adapters: [new QboLikeAdapter("acme")],
    engine,
    store,
    accountMap: defaultAccountMap(),
  });

  const http = new FakeHttpClient([{ status: 200, json: cdcPage }]);
  const page = await new QboSyncConnector().syncPage(conn(), http, POST_AT);
  const outcome = await sink.consume(
    { connectionId: "q1", rawRecords: page.rawRecords, pages: 1, removed: page.removed, health: "ACTIVE", truncated: false, issues: [] },
    POST_AT,
  );

  assert.equal(outcome.posting.posted, 2); // purchase + deposit (tombstone excluded)
  const bal = await accountBalances(store, TENANT, coa(), USD);
  // deposit 5000 in, purchase 1200 out -> cash 3800
  assert.equal(bal.get(asAccountId("gl.cash"))?.toDecimalString(), "3800.00");
  assert.equal(bal.get(asAccountId("gl.income"))?.toDecimalString(), "5000.00");
  assert.equal(bal.get(asAccountId("gl.expense"))?.toDecimalString(), "1200.00");
});

test("re-posting the same sync is idempotent (no double-post)", async () => {
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(coa(), store, new PeriodRegistry());
  const sink = new LedgerPostingSink({
    adapters: [new QboLikeAdapter("acme")],
    engine, store, accountMap: defaultAccountMap(),
  });
  const report = {
    connectionId: "q1", pages: 1, removed: 0, health: "ACTIVE" as const, truncated: false, issues: [],
    rawRecords: (await new QboSyncConnector().syncPage(conn(), new FakeHttpClient([{ status: 200, json: cdcPage }]), POST_AT)).rawRecords,
  };
  const first = await sink.consume(report, POST_AT);
  const second = await sink.consume(report, POST_AT);
  assert.equal(first.posting.posted, 2);
  assert.equal(second.posting.posted, 0);
  assert.equal(second.posting.alreadyPosted, 2);
});

test("SyncRuntime drives QBO sync into the ledger via the posting sink", async () => {
  const connStore = new InMemoryConnectionStore();
  connStore.put(conn());
  const http = new FakeHttpClient([{ status: 200, json: cdcPage }]);
  const runner = new ConnectorRunner(connStore, http, [new QboSyncConnector()]);

  const ledger = new InMemoryLedgerStore();
  const engine = new PostingEngine(coa(), ledger, new PeriodRegistry());
  const sink = new LedgerPostingSink({
    adapters: [new QboLikeAdapter("acme")],
    engine, store: ledger, accountMap: defaultAccountMap(),
  });

  const runtime = new SyncRuntime(connStore, runner, sink.asSink(POST_AT), { intervalMs: 3_600_000 });
  const tick = await runtime.tick(Date.parse(POST_AT), POST_AT);

  assert.equal(tick.synced, 1);
  const bal = await accountBalances(ledger, TENANT, coa(), USD);
  assert.equal(bal.get(asAccountId("gl.cash"))?.toDecimalString(), "3800.00");
});
