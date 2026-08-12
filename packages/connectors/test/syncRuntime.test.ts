import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ConnectorRunner,
  FakeHttpClient,
  InMemoryConnectionStore,
  PlaidConnector,
  SyncRuntime,
  serveSync,
  type Connection,
  type HttpResponse,
  type SyncReport,
} from "../src/index.js";

const noSleep = () => Promise.resolve();

function conn(over: Partial<Connection> = {}): Connection {
  return {
    id: "c1",
    provider: "plaid",
    tenantId: "acme",
    accessToken: "tok",
    baseUrl: "https://sandbox.plaid.com",
    secrets: { client_id: "cid", secret: "sec" },
    health: "NEW",
    ...over,
  };
}

function onePage(): HttpResponse {
  return {
    status: 200,
    json: {
      added: [{ transaction_id: "t1", account_id: "chk", amount: 10, date: "2026-08-01", name: "X" }],
      modified: [],
      removed: [],
      next_cursor: "cur-1",
      has_more: false,
    },
  };
}

function runtimeWith(store: InMemoryConnectionStore, http: FakeHttpClient, sink: (r: SyncReport) => void, opts = {}) {
  const runner = new ConnectorRunner(store, http, [new PlaidConnector()], { sleep: noSleep });
  return new SyncRuntime(store, runner, sink, { intervalMs: 1000, backoffBaseMs: 100, ...opts });
}

test("a never-synced connection is due immediately and its records reach the sink", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const http = new FakeHttpClient([onePage()]);
  const got: SyncReport[] = [];
  const rt = runtimeWith(store, http, (r) => got.push(r));

  const report = await rt.tick(10_000, "2026-08-06T10:00:00Z");
  assert.equal(report.synced, 1);
  assert.equal(got.length, 1);
  assert.equal(got[0]?.rawRecords.length, 1);
  assert.equal(store.get("c1")?.health, "ACTIVE");
});

test("a freshly-synced connection is not due again until the interval elapses", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const http = new FakeHttpClient([onePage(), onePage()]);
  const rt = runtimeWith(store, http, () => {});

  await rt.tick(0, "2026-08-06T10:00:00Z"); // syncs; next due at 0 + 1000
  const before = http.requests.length;

  const midTick = await rt.tick(500, "2026-08-06T10:05:00Z"); // within interval → skip
  assert.equal(midTick.synced, 0);
  assert.equal(midTick.outcomes[0]?.reason, "not_due");
  assert.equal(http.requests.length, before); // API not called again

  const later = await rt.tick(1500, "2026-08-06T11:00:00Z"); // past interval → sync
  assert.equal(later.synced, 1);
});

test("a revoked connection is skipped without touching the API", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn({ health: "REVOKED" }));
  const http = new FakeHttpClient([]); // any call would throw (no scripted response)
  const rt = runtimeWith(store, http, () => {});

  const report = await rt.tick(10_000, "2026-08-06T10:00:00Z");
  assert.equal(report.synced, 0);
  assert.equal(report.outcomes[0]?.reason, "revoked");
  assert.equal(http.requests.length, 0);
});

test("an errored connection backs off and retries only after the delay", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  // first sync: a 500 (ProviderHttpError → ERROR); then a good page on retry
  const http = new FakeHttpClient([{ status: 500, json: {} }, onePage()]);
  const rt = runtimeWith(store, http, () => {}, { backoffBaseMs: 100 });

  const t1 = await rt.tick(0, "2026-08-06T10:00:00Z");
  assert.equal(t1.outcomes[0]?.health, "ERROR");
  assert.equal(store.get("c1")?.health, "ERROR");

  // still within backoff (100ms) → not retried
  const t2 = await rt.tick(50, "2026-08-06T10:01:00Z");
  assert.equal(t2.outcomes[0]?.reason, "not_due");

  // after backoff → retried, succeeds
  const t3 = await rt.tick(150, "2026-08-06T10:02:00Z");
  assert.equal(t3.synced, 1);
  assert.equal(store.get("c1")?.health, "ACTIVE");
});

test("backoff grows with consecutive failures", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const http = new FakeHttpClient(() => ({ status: 500, json: {} })); // always errors
  const rt = runtimeWith(store, http, () => {}, { backoffBaseMs: 100, backoffMaxMs: 10_000 });

  await rt.tick(0, "t"); // fail #1 → next at 0+100
  const afterFirst = await rt.tick(100, "t"); // fail #2 → next at 100+200
  assert.equal(afterFirst.outcomes[0]?.health, "ERROR");
  // at 250 (< 100+200=300) still not due
  assert.equal((await rt.tick(250, "t")).outcomes[0]?.reason, "not_due");
  // at 300 due again
  assert.equal((await rt.tick(300, "t")).outcomes[0]?.health, "ERROR");
});

test("serveSync ticks a bounded number of times with an injected clock", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const http = new FakeHttpClient(() => onePage());
  let sinkCalls = 0;
  const rt = runtimeWith(store, http, () => {
    sinkCalls++;
  });

  let t = 0;
  await serveSync(rt, {
    intervalMs: 1000,
    now: () => {
      const v = t;
      t += 1000; // each tick advances a full interval so the connection is due
      return v;
    },
    sleep: noSleep,
    maxTicks: 3,
  });
  assert.equal(sinkCalls, 3);
});
