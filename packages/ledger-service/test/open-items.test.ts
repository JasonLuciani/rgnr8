import { test } from "node:test";
import assert from "node:assert/strict";
import { newDb } from "pg-mem";
import { InMemoryBackend, LedgerService, PostgresBackend, type LedgerBackend, type ServiceResponse } from "../src/index.js";

/**
 * QuickBooks' open invoices and bills, kept as a non-posting copy so Invoices,
 * Collections and the forecast can see what is owed during a parallel run,
 * when the books themselves hold balances but no documents.
 */

const NOW = "2026-10-29T00:00:00Z";

const call = (s: LedgerService, method: string, path: string, body: unknown = ""): Promise<ServiceResponse> =>
  s.handle({ method, path, query: {}, body: typeof body === "string" ? body : JSON.stringify(body), headers: {} });

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;

const SNAPSHOT = {
  as_of: "2026-10-29",
  invoices: [
    { qbo_id: "130", number: "1037", party: "Northwind", date: "2026-10-01", due_date: "2026-10-31", open_minor: "28995" },
    { qbo_id: "131", number: "1038", party: "Fabrikam", date: "2026-10-15", due_date: "2026-11-14", open_minor: "10000" },
  ],
  bills: [
    { qbo_id: "77", party: "Copyshop", date: "2026-10-10", due_date: "2026-11-09", open_minor: "4500" },
  ],
};

function pgBackend(): PostgresBackend {
  const pool = new (newDb().adapters.createPg().Pool)();
  return new PostgresBackend(pool as never, { enforceRls: false });
}

for (const [name, make] of [
  ["in-memory", () => new InMemoryBackend()],
  ["postgres", () => pgBackend()],
] as Array<[string, () => LedgerBackend]>) {
  test(`${name}: a snapshot round-trips with each side's total`, async () => {
    const backend = make();
    await backend.migrate();
    const s = new LedgerService(backend, { now: () => NOW });
    assert.equal(obj(await call(s, "GET", "/t/acme/mirror/open-items"))["as_of"], "");
    assert.equal((await call(s, "POST", "/t/acme/mirror/open-items", SNAPSHOT)).status, 200);
    const got = obj(await call(s, "GET", "/t/acme/mirror/open-items"));
    assert.equal(got["as_of"], "2026-10-29");
    assert.equal(got["invoice_total_minor"], "38995");
    assert.equal(got["bill_total_minor"], "4500");
    assert.deepEqual((got["invoices"] as Array<Record<string, unknown>>).map((i) => i["number"]), ["1037", "1038"]);
  });

  test(`${name}: a refresh replaces the snapshot; what was paid is gone`, async () => {
    const backend = make();
    await backend.migrate();
    const s = new LedgerService(backend, { now: () => NOW });
    await call(s, "POST", "/t/acme/mirror/open-items", SNAPSHOT);
    await call(s, "POST", "/t/acme/mirror/open-items", {
      as_of: "2026-10-30", invoices: [SNAPSHOT.invoices[1]], bills: [],
    });
    const got = obj(await call(s, "GET", "/t/acme/mirror/open-items"));
    assert.equal(got["as_of"], "2026-10-30");
    assert.equal(got["invoice_total_minor"], "10000");
    assert.deepEqual(got["bills"], []);
    // ...and another tenant never sees it
    assert.deepEqual(obj(await call(s, "GET", "/t/other/mirror/open-items"))["invoices"], []);
  });
}

test("it posts nothing, so it is allowed while QuickBooks is the book of record", async () => {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/settings", { book_of_record: "quickbooks" });
  assert.equal((await call(s, "POST", "/t/acme/mirror/open-items", SNAPSHOT)).status, 200);
  assert.deepEqual(obj(await call(s, "GET", "/t/acme/entries"))["entries"], []);
});

test("a malformed snapshot is refused and the old one kept", async () => {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/mirror/open-items", SNAPSHOT);
  const bad = [
    { ...SNAPSHOT, as_of: "yesterday" },
    { ...SNAPSHOT, invoices: [{ ...SNAPSHOT.invoices[0], open_minor: "12.50" }] },
    { ...SNAPSHOT, invoices: [{ ...SNAPSHOT.invoices[0], open_minor: "0" }] },
    { ...SNAPSHOT, invoices: [SNAPSHOT.invoices[0], SNAPSHOT.invoices[0]] },
    { ...SNAPSHOT, bills: [{ ...SNAPSHOT.bills[0], qbo_id: "" }] },
    { ...SNAPSHOT, invoices: "all of them" },
  ];
  for (const body of bad) {
    assert.equal((await call(s, "POST", "/t/acme/mirror/open-items", body)).status, 400, JSON.stringify(body));
  }
  assert.equal(obj(await call(s, "GET", "/t/acme/mirror/open-items"))["invoice_total_minor"], "38995");
});
