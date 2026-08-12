import { test } from "node:test";
import assert from "node:assert/strict";
import {
  InMemoryConnectionStore,
  buildHealthReport,
  opsConnectorStatus,
  type Connection,
} from "../src/index.js";

function conn(over: Partial<Connection> = {}): Connection {
  return {
    id: "c1",
    provider: "plaid",
    tenantId: "acme",
    accessToken: "access-tok",
    baseUrl: "https://sandbox.plaid.com",
    secrets: {},
    health: "ACTIVE",
    ...over,
  };
}

test("opsConnectorStatus summarizes a health report into the ops-status/1 fragment", () => {
  const store = new InMemoryConnectionStore();
  store.put(conn({ id: "a", health: "ACTIVE", lastSyncedAt: "2026-09-02T06:00:00Z" }));
  store.put(conn({ id: "b", health: "REVOKED", lastSyncedAt: "2026-09-01T06:00:00Z" }));
  store.put(conn({ id: "c", health: "ACTIVE", lastSyncedAt: "2026-08-01T06:00:00Z" })); // stale
  const report = buildHealthReport(store, "2026-09-02T10:00:00Z");
  const s = opsConnectorStatus(report);
  assert.equal(s.total, 3);
  assert.equal(s.needs_attention, 1); // the revoked one
  assert.ok(s.stale >= 1); // the month-old sync
  assert.equal(s.last_sync, "2026-09-02T06:00:00Z"); // most recent across connections
});

test("opsConnectorStatus reports null last_sync when nothing has ever synced", () => {
  const store = new InMemoryConnectionStore();
  store.put(conn({ id: "a", health: "NEW" }));
  const s = opsConnectorStatus(buildHealthReport(store, "2026-09-02T10:00:00Z"));
  assert.equal(s.total, 1);
  assert.equal(s.last_sync, null);
});
