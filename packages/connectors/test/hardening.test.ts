import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ConnectorRunner,
  InMemoryConnectionStore,
  InMemorySyncScheduleStore,
  SyncRuntime,
  type Connection,
  type Connector,
  type TokenRefresher,
} from "../src/index.js";

function conn(over: Partial<Connection> = {}): Connection {
  return {
    id: "c1",
    provider: "plaid",
    tenantId: "acme",
    accessToken: "old-token",
    baseUrl: "https://sandbox.plaid.com",
    secrets: {},
    health: "ACTIVE",
    ...over,
  };
}

const noHttp = { async request() { return { status: 200, json: {} }; } };

// A trivial connector whose sync always succeeds with zero records.
const okConnector: Connector = {
  provider: "plaid",
  async syncPage() {
    return { rawRecords: [], nextCursor: undefined, hasMore: false, removed: 0 };
  },
};

function runner(store: InMemoryConnectionStore): ConnectorRunner {
  return new ConnectorRunner(store, noHttp, [okConnector]);
}

test("persisted backoff state survives a runtime restart", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn({ health: "ACTIVE" }));
  // a runner whose sync reports ERROR by using a connector that throws a rate limit
  const failingConnector: Connector = {
    provider: "plaid",
    async syncPage() {
      throw new Error("boom");
    },
  };
  const failingRunner = new ConnectorRunner(store, noHttp, [failingConnector]);
  const schedules = new InMemorySyncScheduleStore();
  const rt1 = new SyncRuntime(store, failingRunner, () => {}, { schedules });
  await rt1.tick(1_000, "2026-09-02T00:00:00Z"); // fails → schedules a backoff
  const after = schedules.get("c1");
  assert.ok(after && after.failures === 1);

  // "restart": a brand-new runtime over the SAME schedule store keeps the backoff
  const rt2 = new SyncRuntime(store, failingRunner, () => {}, { schedules });
  const report = await rt2.tick(1_500, "2026-09-02T00:01:00Z"); // still within backoff window
  const out = report.outcomes[0];
  assert.equal(out?.synced, false);
  assert.equal(out?.reason, "not_due"); // backoff remembered across the restart
});

test("an expired connection is auto-refreshed and then syncs", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn({ health: "EXPIRED", accessToken: "dead", refreshToken: "rt-1" }));

  const refresher: TokenRefresher = {
    async refresh(c) {
      assert.equal(c.refreshToken, "rt-1");
      return { accessToken: "fresh-token", refreshToken: "rt-2", accessTokenExpiresAt: "2026-12-01T00:00:00Z" };
    },
  };
  const rt = new SyncRuntime(store, runner(store), () => {}, { refresher });
  const report = await rt.tick(10_000, "2026-09-02T00:00:00Z");
  assert.equal(report.outcomes[0]?.synced, true); // refreshed → synced same tick
  const updated = store.get("c1");
  assert.equal(updated?.accessToken, "fresh-token");
  assert.equal(updated?.refreshToken, "rt-2");
  assert.equal(updated?.health, "ACTIVE");
});

test("a dead refresh token leaves the connection for the owner to reconnect", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn({ health: "EXPIRED", refreshToken: "rt-dead" }));
  const refresher: TokenRefresher = { async refresh() { return null; } };
  const rt = new SyncRuntime(store, runner(store), () => {}, { refresher });
  const report = await rt.tick(10_000, "2026-09-02T00:00:00Z");
  assert.equal(report.outcomes[0]?.synced, false);
  assert.equal(report.outcomes[0]?.reason, "expired");
  assert.equal(store.get("c1")?.health, "EXPIRED"); // untouched — needs reconnect
});

test("without a refresher, expired connections are skipped as before", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn({ health: "EXPIRED" }));
  const rt = new SyncRuntime(store, runner(store), () => {});
  const report = await rt.tick(10_000, "2026-09-02T00:00:00Z");
  assert.equal(report.outcomes[0]?.reason, "expired");
});
