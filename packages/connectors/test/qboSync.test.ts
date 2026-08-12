import { test } from "node:test";
import assert from "node:assert/strict";
import {
  FakeHttpClient,
  QboSyncConnector,
  type Connection,
  type HttpRequest,
} from "../src/index.js";

function conn(over: Partial<Connection> = {}): Connection {
  return {
    id: "q1",
    provider: "qbo",
    tenantId: "acme",
    accessToken: "qbo-access-tok",
    baseUrl: "https://quickbooks.api.intuit.com",
    secrets: { realm_id: "realm-123" },
    health: "NEW",
    ...over,
  };
}

// A CDC response with changes across two entity types plus one tombstone.
const cdcPage = {
  CDCResponse: [
    {
      QueryResponse: [
        {
          Purchase: [
            {
              Id: "10",
              TxnDate: "2026-08-05",
              TotalAmt: 1200.0,
              AccountRef: { value: "chk", name: "Checking" },
              MetaData: { LastUpdatedTime: "2026-08-10T12:00:00-07:00" },
            },
            // A deleted Purchase is a tombstone, not a record.
            { Id: "11", status: "Deleted" },
          ],
        },
        {
          Deposit: [
            {
              Id: "20",
              TxnDate: "2026-08-06",
              TotalAmt: 5000.0,
              DepositToAccountRef: { value: "chk", name: "Checking" },
              MetaData: { LastUpdatedTime: "2026-08-10T12:05:00-07:00" },
            },
          ],
        },
      ],
    },
  ],
  time: "2026-08-11T00:00:00.000-07:00",
};

test("QBO CDC sync maps changed records to canonical RawRecords", async () => {
  const http = new FakeHttpClient([{ status: 200, json: cdcPage }]);
  const page = await new QboSyncConnector().syncPage(
    conn({ cursor: "2026-08-01T00:00:00-07:00" }),
    http,
    "2026-08-11T10:00:00Z",
  );

  // Two real records (Purchase 10, Deposit 20); the deleted Purchase is removed.
  assert.equal(page.rawRecords.length, 2);
  assert.equal(page.removed, 1);
  assert.equal(page.hasMore, false);

  const purchase = page.rawRecords.find((r) => r.externalId === "Purchase:10");
  assert.ok(purchase);
  assert.equal(purchase?.provider, "qbo");
  assert.equal(purchase?.accountId, "chk");
  assert.equal(purchase?.sourceVersion, "qbo-cdc-1");
  assert.equal(purchase?.fetchedAt, "2026-08-11T10:00:00Z");
  assert.deepEqual((purchase?.payload as { entity: string }).entity, "Purchase");

  const deposit = page.rawRecords.find((r) => r.externalId === "Deposit:20");
  assert.ok(deposit);
  assert.equal(deposit?.accountId, "chk");
});

test("QBO sync advances the cursor to the CDC server time", async () => {
  const http = new FakeHttpClient([{ status: 200, json: cdcPage }]);
  const page = await new QboSyncConnector().syncPage(conn(), http, "2026-08-11T10:00:00Z");
  assert.equal(page.nextCursor, "2026-08-11T00:00:00.000-07:00");
});

test("QBO sync uses the connection cursor as changedSince and bearer auth", async () => {
  const http = new FakeHttpClient([{ status: 200, json: cdcPage }]);
  await new QboSyncConnector().syncPage(
    conn({ cursor: "2026-08-01T00:00:00-07:00" }),
    http,
    "2026-08-11T10:00:00Z",
  );
  const req = http.requests[0] as HttpRequest;
  assert.equal(req.method, "GET");
  assert.match(req.url, /\/v3\/company\/realm-123\/cdc/);
  assert.match(req.url, /entities=Purchase,Deposit,Payment/);
  // The prior cursor is passed (url-encoded) as changedSince.
  assert.match(req.url, /changedSince=2026-08-01T00%3A00%3A00-07%3A00/);
  assert.equal(req.headers?.["Authorization"], "Bearer qbo-access-tok");
});

test("QBO sync handles an empty change-set: no records, cursor still advances", async () => {
  const empty = { CDCResponse: [{ QueryResponse: [{}] }], time: "2026-08-11T09:00:00.000-07:00" };
  const http = new FakeHttpClient([{ status: 200, json: empty }]);
  const page = await new QboSyncConnector().syncPage(conn(), http, "2026-08-11T10:00:00Z");
  assert.equal(page.rawRecords.length, 0);
  assert.equal(page.removed, 0);
  assert.equal(page.hasMore, false);
  assert.equal(page.nextCursor, "2026-08-11T09:00:00.000-07:00");
});

test("QBO sync with no server time falls back to fetchedAt (advance to now)", async () => {
  const http = new FakeHttpClient([{ status: 200, json: { CDCResponse: [] } }]);
  const page = await new QboSyncConnector().syncPage(conn(), http, "2026-08-11T10:00:00Z");
  assert.equal(page.rawRecords.length, 0);
  assert.equal(page.nextCursor, "2026-08-11T10:00:00Z");
});

test("QBO sync maps auth/rate-limit/other HTTP failures to typed errors", async () => {
  const c = new QboSyncConnector();
  const at = "2026-08-11T10:00:00Z";

  await assert.rejects(
    () => c.syncPage(conn(), new FakeHttpClient([{ status: 429, json: {} }]), at),
    /rate limited/,
  );
  await assert.rejects(
    () => c.syncPage(conn(), new FakeHttpClient([{ status: 401, json: {} }]), at),
    /token expired/,
  );
  await assert.rejects(
    () =>
      c.syncPage(
        conn(),
        new FakeHttpClient([{ status: 403, json: { Fault: { type: "AUTHENTICATION" } } }]),
        at,
      ),
    /revoked/,
  );
  await assert.rejects(
    () => c.syncPage(conn(), new FakeHttpClient([{ status: 500, json: {} }]), at),
    /QBO error 500/,
  );
});
