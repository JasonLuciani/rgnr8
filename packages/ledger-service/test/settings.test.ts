import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Per-account settings. These are the options an account administrator flips —
 * inventory costing method, base currency, multi-currency, data retention. The
 * store defaults every field so an account that never opens the screen behaves
 * exactly as it always did, and it validates each field on write.
 */

const NOW = "2026-09-01T00:00:00Z";

const call = (
  s: LedgerService, method: string, path: string, body: unknown = "",
): Promise<ServiceResponse> =>
  s.handle({
    method, path, query: {},
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: {},
  });

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;

function svc(): LedgerService {
  return new LedgerService(new InMemoryBackend(), { now: () => NOW });
}

test("an untouched account reports safe defaults", async () => {
  const s = svc();
  const r = await call(s, "GET", "/t/acme/settings");
  assert.equal(r.status, 200, JSON.stringify(r.body));
  const set = obj(r)["settings"] as Record<string, unknown>;
  assert.equal(set["inventory_costing_method"], "MOVING_AVERAGE");
  assert.equal(set["base_currency"], "USD");
  assert.equal(set["multi_currency_enabled"], false);
  assert.equal(set["retention_audit_days"], 0);
  assert.equal(set["retention_soft_delete_days"], 0);
});

test("a patch changes only the fields it names and persists", async () => {
  const s = svc();
  const saved = await call(s, "POST", "/t/acme/settings", {
    inventory_costing_method: "fifo", multi_currency_enabled: true, retention_audit_days: 365,
  });
  assert.equal(saved.status, 200, JSON.stringify(saved.body));
  const set = obj(saved)["settings"] as Record<string, unknown>;
  assert.equal(set["inventory_costing_method"], "FIFO", "normalised to upper case");
  assert.equal(set["multi_currency_enabled"], true);
  assert.equal(set["retention_audit_days"], 365);
  assert.equal(set["base_currency"], "USD", "untouched fields keep their value");

  // and it is durable
  const again = obj(await call(s, "GET", "/t/acme/settings"))["settings"] as Record<string, unknown>;
  assert.equal(again["inventory_costing_method"], "FIFO");
  assert.equal(again["retention_audit_days"], 365);
});

test("a bad value is refused, not coerced", async () => {
  const s = svc();
  assert.equal((await call(s, "POST", "/t/acme/settings", { inventory_costing_method: "guesswork" })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/settings", { base_currency: "dollars" })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/settings", { retention_audit_days: -5 })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/settings", { retention_audit_days: 1.5 })).status, 400);
});

test("one tenant's settings are invisible to another", async () => {
  const s = svc();
  await call(s, "POST", "/t/acme/settings", { inventory_costing_method: "FIFO" });
  const other = obj(await call(s, "GET", "/t/beta/settings"))["settings"] as Record<string, unknown>;
  assert.equal(other["inventory_costing_method"], "MOVING_AVERAGE", "beta still sees the default");
});
