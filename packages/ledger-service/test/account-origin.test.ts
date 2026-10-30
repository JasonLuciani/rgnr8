import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { newDb } from "pg-mem";
import { asTenantId, type Account } from "@rgnr8/ledger-kernel";
import { InMemoryBackend, LedgerService, PostgresBackend, type ServiceResponse } from "../src/index.js";

/**
 * The one-time account-origin backfill. A tenant that went live from QuickBooks
 * before origins were recorded has none of its accounts marked; its first chart
 * read or write marks them, under its own RLS context, then sets a flag so it
 * never runs again. Each scenario below reproduces that pre-deploy state.
 */

const NOW = "2026-10-30T00:00:00Z";
const T = asTenantId("acme");

const call = (
  s: LedgerService, method: string, path: string, body: unknown = "",
): Promise<ServiceResponse> =>
  s.handle({
    method, path, query: {},
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: {},
  });

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;

async function chartRows(s: LedgerService): Promise<Array<Record<string, unknown>>> {
  return obj(await call(s, "GET", "/t/acme/accounts"))["accounts"] as Array<Record<string, unknown>>;
}

/** A QuickBooks go-live, as the web app sends it. */
async function goLiveFromQuickBooks(s: LedgerService): Promise<void> {
  const r = await call(s, "POST", "/t/acme/go-live", {
    contract: "go-live/1", tenant_id: "acme", source_system: "quickbooks",
    cutover_date: "2026-09-30", currency: "USD", coa_category: "PROFESSIONAL_SERVICES",
    opening_balance_equity_code: "3010",
    source_accounts: [
      { code: "1000", name: "Checking", balance_minor: "2500000", subtype: "BANK" },
      { code: "3900", name: "Retained Earnings", balance_minor: "-2500000", subtype: "RETAINED_EARNINGS" },
    ],
  });
  assert.equal(r.status, 200, JSON.stringify(r.body));
}

/**
 * Make a tenant look as it did before this deploy: every account with no recorded
 * origin, and the backfill not yet run.
 */
async function rewindToPreDeploy(backend: InMemoryBackend | PostgresBackend): Promise<void> {
  const chart = await backend.chart(T);
  for (const a of chart.list()) {
    const plain: Account = { ...a, origin: "rgnr8" };
    await backend.saveAccount(T, plain);
  }
  const s = await backend.settings().get(T);
  await backend.settings().save(T, { ...s, accountOriginBackfilled: false });
}

const quickBooksOfRecord = (s: LedgerService) =>
  call(s, "POST", "/t/acme/settings", { book_of_record: "quickbooks", mirror_realm_id: "9341453432535209" });

describe("account origin backfill", () => {
  test("(a) a QuickBooks tenant from before the origin column: its existing accounts are locked on first read", async () => {
    const backend = new InMemoryBackend();
    const s = new LedgerService(backend, { now: () => NOW });
    await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
    await goLiveFromQuickBooks(s);
    await rewindToPreDeploy(backend);
    assert.equal((await quickBooksOfRecord(s)).status, 200);

    const rows = await chartRows(s);
    const bank = rows.find((a) => a["code"] === "1000")!;
    assert.equal(bank["origin"], "quickbooks");
    assert.equal(bank["locked_by"], "quickbooks");
    // Every account that existed, not just the ones with a balance.
    assert.ok(rows.filter((a) => a["code"] === "6000").every((a) => a["origin"] === "quickbooks"));
    assert.equal((await backend.settings().get(T)).accountOriginBackfilled, true);
    assert.equal((await call(s, "PATCH", "/t/acme/accounts/1000", { name: "Operating" })).status, 409);
  });

  test("(b) an account created after the backfill stays RGNR8's and is editable while QuickBooks is the book", async () => {
    const backend = new InMemoryBackend();
    const s = new LedgerService(backend, { now: () => NOW });
    await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
    await goLiveFromQuickBooks(s);
    await rewindToPreDeploy(backend);
    await quickBooksOfRecord(s);
    await chartRows(s); // the backfill runs here

    const created = await call(s, "POST", "/t/acme/accounts", {
      code: "7010", name: "Parking", subtype: "EXPENSE",
    });
    assert.equal(created.status, 201);
    assert.equal((obj(created)["account"] as Record<string, unknown>)["origin"], "rgnr8");
    assert.equal((await call(s, "PATCH", "/t/acme/accounts/7010", { name: "Parking Garage" })).status, 200);
  });

  test("(c) a tenant that never went live from QuickBooks is flagged, with no locks", async () => {
    const backend = new InMemoryBackend();
    const s = new LedgerService(backend, { now: () => NOW });
    await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
    await rewindToPreDeploy(backend);

    const rows = await chartRows(s);
    assert.equal((await backend.settings().get(T)).accountOriginBackfilled, true);
    assert.ok(rows.every((a) => a["origin"] === "rgnr8"));
    assert.ok(rows.every((a) => a["locked_by"] !== "quickbooks"));
    // Its system accounts are still system-locked, which is a different thing.
    assert.equal(rows.find((a) => a["code"] === "1200")!["locked_by"], "system");
  });

  test("(d) the backfill runs once: later accounts aren't re-marked, and a settings save doesn't reset the flag", async () => {
    const backend = new InMemoryBackend();
    const s = new LedgerService(backend, { now: () => NOW });
    await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
    await goLiveFromQuickBooks(s);
    await rewindToPreDeploy(backend);
    await quickBooksOfRecord(s);
    await chartRows(s);

    assert.equal((await call(s, "POST", "/t/acme/accounts", {
      code: "7020", name: "Mileage", subtype: "EXPENSE",
    })).status, 201);
    // An owner's unrelated settings change must carry the flag forward.
    assert.equal((await call(s, "POST", "/t/acme/settings", { retention_audit_days: 30 })).status, 200);
    assert.equal((await backend.settings().get(T)).accountOriginBackfilled, true);

    const again = await chartRows(s);
    assert.equal(again.find((a) => a["code"] === "7020")!["origin"], "rgnr8", "not re-marked");
    assert.equal(again.find((a) => a["code"] === "1000")!["origin"], "quickbooks", "still marked");
  });

  test("a go-live settles the flag, so accounts it just made are never mistaken for pre-existing ones", async () => {
    const backend = new InMemoryBackend();
    const s = new LedgerService(backend, { now: () => NOW });
    await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
    await goLiveFromQuickBooks(s);
    assert.equal((await backend.settings().get(T)).accountOriginBackfilled, true);
  });

  test("the backfill runs under Postgres row-level security, through the settings row lock", async () => {
    const pool = new (newDb().adapters.createPg().Pool)();
    const backend = new PostgresBackend(pool as never, { enforceRls: false });
    await backend.migrate();
    const s = new LedgerService(backend, { now: () => NOW });
    await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
    await goLiveFromQuickBooks(s);
    await rewindToPreDeploy(backend);
    await quickBooksOfRecord(s);

    // A second service over the same database, as a second process would be.
    const other = new LedgerService(new PostgresBackend(pool as never, { enforceRls: false }), { now: () => NOW });
    const rows = (obj(await call(other, "GET", "/t/acme/accounts"))["accounts"]) as Array<Record<string, unknown>>;
    assert.equal(rows.find((a) => a["code"] === "1000")!["locked_by"], "quickbooks");
    assert.equal(rows.find((a) => a["code"] === "1000")!["origin"], "quickbooks");
    assert.equal((await new PostgresBackend(pool as never, { enforceRls: false }).settings().get(T))
      .accountOriginBackfilled, true);

    // And once flagged, a new account on Postgres is RGNR8's.
    assert.equal((await call(other, "POST", "/t/acme/accounts", {
      code: "7030", name: "Postage", subtype: "EXPENSE",
    })).status, 201);
    const after = (obj(await call(other, "GET", "/t/acme/accounts"))["accounts"]) as Array<Record<string, unknown>>;
    assert.equal(after.find((a) => a["code"] === "7030")!["origin"], "rgnr8");
  });
});

