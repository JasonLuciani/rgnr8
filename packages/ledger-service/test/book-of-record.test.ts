import { test } from "node:test";
import assert from "node:assert/strict";
import { asTenantId, type DraftEntry } from "@rgnr8/ledger-kernel";
import { newDb } from "pg-mem";
import { InMemoryBackend, LedgerService, PostgresBackend, type ServiceResponse } from "../src/index.js";
import {
  BookOfRecordError,
  PARALLEL_RUN_MESSAGE,
  guardBackend,
  isQuickBooksCopy,
} from "../src/bookOfRecord.js";
import { defaultSettings } from "../src/settings.js";

/**
 * Parallel run: while QuickBooks is the book of record, RGNR8's ledger is a copy
 * of it. A Refresh refuses to run once anything else is in the journal, so a
 * single entry typed into RGNR8 used to block every later Refresh. Nothing but
 * the mirror may post now, and nobody else may claim to be the mirror.
 */

const NOW = "2026-10-28T00:00:00Z";

const call = (
  s: LedgerService, method: string, path: string, body: unknown = "",
): Promise<ServiceResponse> =>
  s.handle({
    method, path, query: {},
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: {},
  });

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;

async function account(bookOfRecord: "rgnr8" | "quickbooks"): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  await call(s, "POST", "/t/acme/customers", { id: "cust-1", name: "Northwind Ltd", terms_days: 30 });
  const set = await call(s, "POST", "/t/acme/settings", {
    book_of_record: bookOfRecord,
    ...(bookOfRecord === "quickbooks" ? { mirror_realm_id: "9341453432535209" } : {}),
  });
  assert.equal(set.status, 200);
  return s;
}

const INVOICE = {
  id: "INV-1", party_id: "cust-1", date: "2026-10-01",
  lines: [{ unit_amount_minor: "100000", account_code: "4100" }],
};
const ENTRY = {
  date: "2026-10-05",
  lines: [
    { code: "1000", side: "DEBIT", amount_minor: "5000" },
    { code: "4000", side: "CREDIT", amount_minor: "5000" },
  ],
};

async function journal(s: LedgerService): Promise<Array<Record<string, unknown>>> {
  return obj(await call(s, "GET", "/t/acme/entries"))["entries"] as Array<Record<string, unknown>>;
}

test("with RGNR8 as the book of record, posting works as it always has", async () => {
  const s = await account("rgnr8");
  assert.equal((await call(s, "POST", "/t/acme/invoices", INVOICE)).status, 201);
  assert.equal((await call(s, "POST", "/t/acme/entries", ENTRY)).status, 201);
});

test("during a parallel run an invoice is refused with 409, and nothing is saved", async () => {
  const s = await account("quickbooks");
  const r = await call(s, "POST", "/t/acme/invoices", INVOICE);
  assert.equal(r.status, 409);
  assert.equal(obj(r)["code"], "book_of_record");
  assert.equal(obj(r)["error"], PARALLEL_RUN_MESSAGE);
  // Refused at the door: no document half-saved behind the refusal.
  assert.equal((await call(s, "GET", "/t/acme/invoices/INV-1")).status, 404);
  assert.deepEqual(await journal(s), []);
});

test("during a parallel run every posting route is refused", async () => {
  const s = await account("quickbooks");
  const routes = [
    "/t/acme/entries/acme:1/reverse",
    "/t/acme/ingest",
    "/t/acme/invoices", "/t/acme/bills",
    "/t/acme/invoices/INV-1/payments", "/t/acme/bills/B-1/credits", "/t/acme/invoices/INV-1/refunds",
    "/t/acme/purchase-orders/PO-1/bill", "/t/acme/purchase-orders/PO-1/receipts",
    "/t/acme/sales-orders/SO-1/invoice", "/t/acme/estimates/E-1/invoice",
    "/t/acme/jobs/J-1/bill/progress", "/t/acme/jobs/J-1/retainage/release",
    "/t/acme/jobs/J-1/deposits", "/t/acme/jobs/J-1/deposits/apply",
    "/t/acme/feed/1000", "/t/acme/feed/bulk-accept", "/t/acme/feed/txn/T-1/accept",
    "/t/acme/feed/txn/T-1/match", "/t/acme/feed/txn/T-1/undo",
    "/t/acme/inventory/receipts", "/t/acme/inventory/issues", "/t/acme/inventory/counts",
    "/t/acme/debt/loans", "/t/acme/debt/payments", "/t/acme/debt/draws",
    "/t/acme/assets", "/t/acme/assets/A-1/depreciate", "/t/acme/assets/A-1/usage",
    "/t/acme/assets/A-1/dispose",
    "/t/acme/wip/post", "/t/acme/work-orders/WO-1/entries", "/t/acme/recurring/run",
    "/t/acme/payroll/runs/R-1/post", "/t/acme/payroll/runs/R-1/void", "/t/acme/payroll/remit",
  ];
  for (const path of routes) {
    const r = await call(s, "POST", path, {});
    assert.equal(r.status, 409, path);
    assert.equal(obj(r)["code"], "book_of_record", path);
  }
  assert.deepEqual(await journal(s), []);
});

test("during a parallel run a hand-typed entry is refused, the mirror's catch-up is not", async () => {
  const s = await account("quickbooks");
  const typed = await call(s, "POST", "/t/acme/entries", { ...ENTRY, source: "owner" });
  assert.equal(typed.status, 409);
  assert.equal(obj(typed)["code"], "book_of_record");

  const mirror = await call(s, "POST", "/t/acme/entries", {
    ...ENTRY, source: "quickbooks", idempotency_key: "qbo-mirror:2026-10-05:abc",
    memo: "QuickBooks catch-up to 2026-10-05",
  });
  assert.equal(mirror.status, 201);
  assert.equal((await journal(s)).length, 1);
});

test("things that aren't postings still work during a parallel run", async () => {
  const s = await account("quickbooks");
  assert.equal((await call(s, "POST", "/t/acme/customers", { id: "cust-2", name: "Fabrikam" })).status, 201);
  assert.equal((await call(s, "POST", "/t/acme/accounts", {
    code: "6999", name: "Mirror-created", type: "EXPENSE",
  })).status, 201);
  assert.equal((await call(s, "GET", "/t/acme/trial-balance")).status, 200);
  // And the account can be put back: the switch itself is never locked out.
  assert.equal((await call(s, "POST", "/t/acme/settings", { book_of_record: "rgnr8" })).status, 200);
  assert.equal((await call(s, "POST", "/t/acme/invoices", INVOICE)).status, 201);
});

test("nobody but the mirror may claim a QuickBooks source, whatever the book of record", async () => {
  const s = await account("rgnr8");
  const reserved = /reserved for the QuickBooks mirror/;

  const entry = await call(s, "POST", "/t/acme/entries", { ...ENTRY, source: "QuickBooks" });
  assert.equal(entry.status, 400);
  assert.match(String(obj(entry)["error"]), reserved);

  const ingest = await call(s, "POST", "/t/acme/ingest", { source: "quickbooks", transactions: [] });
  assert.equal(ingest.status, 400);
  assert.match(String(obj(ingest)["error"]), reserved);

  const feed = await call(s, "POST", "/t/acme/feed/1000", { source: "quickbooks", transactions: [] });
  assert.equal(feed.status, 400);
  assert.match(String(obj(feed)["error"]), reserved);

  assert.deepEqual(await journal(s), []);
});

test("the store refuses on its own, for any route the route check misses", async () => {
  const backend = new InMemoryBackend();
  await backend.migrate();
  await backend.settings().save("acme", { ...defaultSettings(), bookOfRecord: "quickbooks" });
  const store = guardBackend(backend).store(asTenantId("acme"));
  const typed = {
    tenantId: asTenantId("acme"), idempotencyKey: "je:2026-10-05:x",
    provenance: { sourceSystem: "rgnr8" },
  } as unknown as DraftEntry;
  await assert.rejects(store.append(typed), BookOfRecordError);
  // Reads pass straight through.
  assert.deepEqual(await store.list(asTenantId("acme")), []);
});

test("a QuickBooks copy is the opening entry or a mirror catch-up, and nothing else", () => {
  const entry = (key: string, sourceSystem: string) =>
    ({ idempotencyKey: key, provenance: { sourceSystem } }) as unknown as DraftEntry;
  assert.equal(isQuickBooksCopy(entry("qbo-mirror:2026-10-05:abc", "quickbooks")), true);
  assert.equal(isQuickBooksCopy(entry("cutover:opening:2026-09-30", "quickbooks")), true);
  assert.equal(isQuickBooksCopy(entry("qbo-mirror:2026-10-05:abc", "owner")), false);
  assert.equal(isQuickBooksCopy(entry("je:2026-10-05:abc", "quickbooks")), false);
});

test("during a parallel run only a QuickBooks go-live may write the opening entry", async () => {
  // On Postgres the go-live posts through its own transaction's store, outside
  // the store guard, so the handler itself has to refuse.
  const pool = new (newDb().adapters.createPg().Pool)();
  const backend = new PostgresBackend(pool as never, { enforceRls: false });
  await backend.migrate();
  assert.ok(backend.atomicGoLive, "exercises the Postgres transaction path");
  const s = new LedgerService(backend, { now: () => NOW });
  await call(s, "POST", "/t/acme/settings", { book_of_record: "quickbooks" });
  const goLive = (source: string) => call(s, "POST", "/t/acme/go-live", {
    contract: "go-live/1", tenant_id: "acme", source_system: source,
    cutover_date: "2026-09-30", currency: "USD", coa_category: "PROFESSIONAL_SERVICES",
    opening_balance_equity_code: "3010",
    source_accounts: [
      { code: "1000", name: "Checking", balance_minor: "2500000", subtype: "BANK" },
      { code: "3900", name: "Retained Earnings", balance_minor: "-2500000", subtype: "RETAINED_EARNINGS" },
    ],
  });
  const xero = await goLive("xero");
  assert.equal(xero.status, 409);
  assert.equal(obj(xero)["code"], "book_of_record");
  assert.deepEqual(await journal(s), []);
  assert.equal((await goLive("quickbooks")).status, 200);
  assert.equal((await journal(s)).length, 1);
});
