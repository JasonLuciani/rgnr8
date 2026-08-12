import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ConnectorRunner,
  FetchHttp,
  InMemoryConnectionStore,
  PlaidConnector,
  buildHealthReport,
  handlePlaidWebhook,
  renderHealthHtml,
  type Connection,
  type FetchLike,
} from "../src/index.js";

function conn(over: Partial<Connection> = {}): Connection {
  return {
    id: "c1",
    provider: "plaid",
    tenantId: "acme",
    accessToken: "access-tok",
    baseUrl: "https://sandbox.plaid.com",
    secrets: { client_id: "cid", secret: "sec", item_id: "item-1" },
    health: "NEW",
    ...over,
  };
}

// --- FetchHttp ---------------------------------------------------------------

test("FetchHttp serializes JSON body and parses JSON response", async () => {
  const calls: { url: string; init: unknown }[] = [];
  const fakeFetch: FetchLike = async (url, init) => {
    calls.push({ url, init });
    return { status: 200, text: async () => JSON.stringify({ ok: true, echo: init?.body }) };
  };
  const http = new FetchHttp({ fetch: fakeFetch });
  const res = await http.request({ method: "POST", url: "https://x/y", body: { a: 1 } });
  assert.equal(res.status, 200);
  assert.deepEqual(res.json, { ok: true, echo: JSON.stringify({ a: 1 }) });
  const [call] = calls;
  assert.equal(call?.url, "https://x/y");
});

test("FetchHttp treats a non-JSON/empty body as {} instead of throwing", async () => {
  const fakeFetch: FetchLike = async () => ({ status: 500, text: async () => "gateway error" });
  const http = new FetchHttp({ fetch: fakeFetch });
  const res = await http.request({ method: "GET", url: "https://x" });
  assert.equal(res.status, 500);
  assert.deepEqual(res.json, {});
});

test("FetchHttp drives a real PlaidConnector sync end-to-end (fake fetch)", async () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const fakeFetch: FetchLike = async () => ({
    status: 200,
    text: async () =>
      JSON.stringify({
        added: [{ transaction_id: "t1", account_id: "chk", amount: 12.5, date: "2026-08-01", name: "X" }],
        modified: [],
        removed: [],
        next_cursor: "cur-1",
        has_more: false,
      }),
  });
  const runner = new ConnectorRunner(store, new FetchHttp({ fetch: fakeFetch }), [new PlaidConnector()]);
  const report = await runner.sync("c1", "2026-08-06T10:00:00Z");
  assert.equal(report.health, "ACTIVE");
  assert.equal(report.rawRecords.length, 1);
  assert.equal(store.get("c1")?.cursor, "cur-1");
});

// --- webhooks ----------------------------------------------------------------

test("SYNC_UPDATES_AVAILABLE resolves to a sync action for the right connection", () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const out = handlePlaidWebhook(store, {
    webhook_type: "TRANSACTIONS",
    webhook_code: "SYNC_UPDATES_AVAILABLE",
    item_id: "item-1",
  });
  assert.equal(out.action, "sync");
  assert.equal(out.connectionId, "c1");
});

test("ITEM ERROR ITEM_LOGIN_REQUIRED marks the connection EXPIRED and asks to reconnect", () => {
  const store = new InMemoryConnectionStore();
  store.put(conn({ health: "ACTIVE" }));
  const out = handlePlaidWebhook(store, {
    webhook_type: "ITEM",
    webhook_code: "ERROR",
    item_id: "item-1",
    error: { error_code: "ITEM_LOGIN_REQUIRED" },
  });
  assert.equal(out.action, "reconnect");
  assert.equal(out.newHealth, "EXPIRED");
  assert.equal(store.get("c1")?.health, "EXPIRED");
});

test("USER_PERMISSION_REVOKED marks REVOKED", () => {
  const store = new InMemoryConnectionStore();
  store.put(conn({ health: "ACTIVE" }));
  const out = handlePlaidWebhook(store, {
    webhook_type: "ITEM",
    webhook_code: "USER_PERMISSION_REVOKED",
    item_id: "item-1",
  });
  assert.equal(out.action, "reconnect");
  assert.equal(store.get("c1")?.health, "REVOKED");
});

test("an unmatched item_id yields unknown_item and changes nothing", () => {
  const store = new InMemoryConnectionStore();
  store.put(conn({ health: "ACTIVE" }));
  const out = handlePlaidWebhook(store, {
    webhook_type: "ITEM",
    webhook_code: "ERROR",
    item_id: "nope",
    error: { error_code: "ITEM_LOGIN_REQUIRED" },
  });
  assert.equal(out.action, "unknown_item");
  assert.equal(store.get("c1")?.health, "ACTIVE");
});

// --- health report -----------------------------------------------------------

test("health report buckets connections and flags attention + stale", () => {
  const store = new InMemoryConnectionStore();
  store.put(conn({ id: "fresh", health: "ACTIVE", lastSyncedAt: "2026-08-06T06:00:00Z" }));
  store.put(conn({ id: "stale", health: "ACTIVE", lastSyncedAt: "2026-08-01T06:00:00Z" }));
  store.put(conn({ id: "dead", health: "EXPIRED", lastSyncedAt: "2026-08-05T06:00:00Z", lastError: "login required" }));
  store.put(conn({ id: "never", health: "NEW" }));

  const report = buildHealthReport(store, "2026-08-06T10:00:00Z");
  assert.equal(report.total, 4);
  assert.equal(report.byHealth.ACTIVE, 2);
  assert.equal(report.byHealth.EXPIRED, 1);
  assert.equal(report.byHealth.NEW, 1);

  const ids = (rows: readonly { id: string }[]) => rows.map((r) => r.id).sort();
  assert.deepEqual(ids(report.needsAttention), ["dead"]);
  // "stale" (old active) and "never" (NEW, never synced) both flagged stale
  assert.deepEqual(ids(report.stale), ["never", "stale"]);

  const fresh = report.rows.find((r) => r.id === "fresh");
  assert.equal(fresh?.stale, false);
  assert.ok((fresh?.hoursSinceSync ?? 0) > 3 && (fresh?.hoursSinceSync ?? 0) < 5);
});

test("health dashboard HTML renders a banner and a row per connection", () => {
  const store = new InMemoryConnectionStore();
  store.put(conn({ id: "dead", health: "REVOKED", lastError: "user revoked" }));
  const html = renderHealthHtml(buildHealthReport(store, "2026-08-06T10:00:00Z"));
  assert.match(html, /<!doctype html>/);
  assert.match(html, /need the owner to reconnect/);
  assert.match(html, /REVOKED/);
  assert.match(html, /user revoked/);
});
