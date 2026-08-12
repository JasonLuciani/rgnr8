import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ConnectorRunner,
  FakeHttpClient,
  InMemoryConnectionStore,
  PlaidConnector,
  type Connection,
  type HttpResponse,
} from "../src/index.js";

const noSleep = () => Promise.resolve();

function conn(): Connection {
  return {
    id: "c1",
    provider: "plaid",
    tenantId: "acme",
    accessToken: "access-tok",
    baseUrl: "https://sandbox.plaid.com",
    secrets: { client_id: "cid", secret: "sec" },
    health: "NEW",
  };
}

function ok(body: object): HttpResponse {
  return { status: 200, json: body };
}

function runner(store: InMemoryConnectionStore, http: FakeHttpClient, opts = {}) {
  return new ConnectorRunner(store, http, [new PlaidConnector()], { sleep: noSleep, ...opts });
}

test("paginates across cursors and returns all records", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const http = new FakeHttpClient([
    ok({ added: [{ transaction_id: "t1", account_id: "chk" }, { transaction_id: "t2", account_id: "chk" }], next_cursor: "c1", has_more: true }),
    ok({ added: [{ transaction_id: "t3", account_id: "chk" }], modified: [], removed: [{ transaction_id: "old" }], next_cursor: "c2", has_more: false }),
  ]);
  const report = await runner(store, http).sync("c1", "2026-08-31T00:00:00Z");

  assert.equal(report.pages, 2);
  assert.equal(report.rawRecords.length, 3);
  assert.equal(report.removed, 1);
  assert.equal(report.health, "ACTIVE");
  // cursor advanced and persisted; access token sent
  assert.equal(store.get("c1")?.cursor, "c2");
  assert.equal(store.get("c1")?.lastSyncedAt, "2026-08-31T00:00:00Z");
  assert.equal((http.requests[0]?.body as { cursor: string }).cursor, ""); // first page: empty cursor
  assert.equal((http.requests[1]?.body as { cursor: string }).cursor, "c1"); // second page uses c1
});

test("expired auth marks the connection EXPIRED", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const http = new FakeHttpClient([{ status: 400, json: { error_code: "ITEM_LOGIN_REQUIRED" } }]);
  const report = await runner(store, http).sync("c1", "2026-08-31T00:00:00Z");
  assert.equal(report.health, "EXPIRED");
  assert.equal(store.get("c1")?.health, "EXPIRED");
  assert.ok(report.issues.some((i) => i.kind === "expired"));
});

test("revoked token marks REVOKED and subsequent syncs are skipped without calling the API", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const http = new FakeHttpClient([{ status: 400, json: { error_code: "INVALID_ACCESS_TOKEN" } }]);
  await runner(store, http).sync("c1", "2026-08-31T00:00:00Z");
  assert.equal(store.get("c1")?.health, "REVOKED");

  const callsBefore = http.requests.length;
  const again = await runner(store, http).sync("c1", "2026-09-01T00:00:00Z");
  assert.equal(again.health, "REVOKED");
  assert.equal(http.requests.length, callsBefore); // no new API call
});

test("rate limits are retried with backoff, then succeed", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const http = new FakeHttpClient([
    { status: 429, json: {} },
    ok({ added: [{ transaction_id: "t1", account_id: "chk" }], has_more: false }),
  ]);
  const report = await runner(store, http, { maxRetries: 3 }).sync("c1", "2026-08-31T00:00:00Z");
  assert.equal(report.health, "ACTIVE");
  assert.equal(report.rawRecords.length, 1);
  assert.ok(report.issues.some((i) => i.kind === "rate_limited"));
});

test("exhausting retries on rate limit ends in ERROR", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const http = new FakeHttpClient(() => ({ status: 429, json: {} }));
  const report = await runner(store, http, { maxRetries: 2 }).sync("c1", "2026-08-31T00:00:00Z");
  assert.equal(report.health, "ERROR");
});

test("hitting the page cap reports truncated but healthy", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const http = new FakeHttpClient(() => ok({ added: [{ transaction_id: "t", account_id: "chk" }], next_cursor: "c", has_more: true }));
  const report = await runner(store, http, { maxPages: 1 }).sync("c1", "2026-08-31T00:00:00Z");
  assert.equal(report.pages, 1);
  assert.ok(report.truncated);
  assert.equal(report.health, "ACTIVE");
});
