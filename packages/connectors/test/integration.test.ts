import { test } from "node:test";
import assert from "node:assert/strict";
import { BankPlaidLikeAdapter, IngestionPipeline } from "@rgnr8/ingestion";
import {
  ConnectorRunner,
  FakeHttpClient,
  InMemoryConnectionStore,
  PlaidConnector,
  type Connection,
} from "../src/index.js";

const noSleep = () => Promise.resolve();

function conn(): Connection {
  return {
    id: "c1", provider: "plaid", tenantId: "acme", accessToken: "tok",
    baseUrl: "https://sandbox.plaid.com", secrets: { client_id: "cid", secret: "sec" }, health: "NEW",
  };
}

// Full Plaid transactions (provider sign: positive = money out).
const page1 = {
  added: [
    { transaction_id: "p1", account_id: "chk", date: "2026-08-05", pending: false, amount: "-5000.00", name: "Client ACH", category: ["Deposit"] },
    { transaction_id: "p2", account_id: "chk", date: "2026-08-06", pending: false, amount: "1200.00", name: "AWS", category: ["Service"] },
  ],
  next_cursor: "c1", has_more: true,
};
const page2 = {
  added: [{ transaction_id: "p3", account_id: "chk", date: "2026-08-08", pending: false, amount: "800.00", name: "Contractor", category: ["Service"] }],
  next_cursor: "c2", has_more: false,
};

test("connector sync feeds the ingestion pipeline end to end", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const http = new FakeHttpClient([
    { status: 200, json: page1 },
    { status: 200, json: page2 },
  ]);
  const runner = new ConnectorRunner(store, http, [new PlaidConnector()], { sleep: noSleep });

  const report = await runner.sync("c1", "2026-08-31T00:00:00Z");
  assert.equal(report.rawRecords.length, 3);
  assert.equal(report.health, "ACTIVE");

  // Feed the synced raw records straight into ingestion.
  const pipeline = new IngestionPipeline().register(new BankPlaidLikeAdapter("acme"));
  const ingest = pipeline.ingest(report.rawRecords);
  assert.equal(ingest.added.length, 3);
  assert.equal(pipeline.active().length, 3);

  // The deposit was sign-normalized (provider negative -> our positive inflow).
  const deposit = pipeline.active().find((t) => t.externalId === "p1");
  assert.equal(deposit?.amount.minorUnits, 500000n);
  assert.equal(deposit?.direction, "INFLOW");

  // Re-ingesting the same sync is idempotent.
  const again = pipeline.ingest(report.rawRecords);
  assert.equal(again.added.length, 0);
});
