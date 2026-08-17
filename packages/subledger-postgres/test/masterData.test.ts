import { test } from "node:test";
import assert from "node:assert/strict";
import { newDb } from "pg-mem";
import { Money, USD, asAccountId } from "@rgnr8/ledger-kernel";
import { InMemoryMasterDataStore } from "@rgnr8/subledger";
import { PgMasterDataStore, type Pool } from "../src/index.js";

function makePool(): Pool {
  const db = newDb();
  const pg = db.adapters.createPg();
  return new pg.Pool() as unknown as Pool;
}
async function fresh(): Promise<PgMasterDataStore> {
  const store = new PgMasterDataStore(makePool());
  await store.migrate();
  return store;
}
const T = "acme";

test("persists and reloads a customer with nested address", async () => {
  const store = await fresh();
  await store.upsertCustomer(T, {
    id: "c1", name: "Acme Corp", email: "ap@acme.com",
    billingAddress: { line1: "1 Main St", city: "Austin", region: "TX", country: "US" },
    termsDays: 30, active: true,
  });
  const c = await store.getCustomer(T, "c1");
  assert.equal(c?.name, "Acme Corp");
  assert.equal(c?.billingAddress?.city, "Austin");
  assert.equal(c?.termsDays, 30);
});

test("persists a vendor with 1099 flag and reloads it", async () => {
  const store = await fresh();
  await store.upsertVendor(T, {
    id: "v1", name: "Freelance Dev", is1099: true, taxId: "12-3456789",
    defaultExpenseAccountId: asAccountId("gl.contractors"),
  });
  const v = await store.getVendor(T, "v1");
  assert.equal(v?.is1099, true);
  assert.equal(v?.taxId, "12-3456789");
  assert.equal(String(v?.defaultExpenseAccountId), "gl.contractors");
});

test("persists an item with a Money unit price (exact minor units)", async () => {
  const store = await fresh();
  await store.upsertItem(T, {
    id: "i1", name: "Consulting hour", type: "SERVICE",
    unitPrice: Money.fromDecimal("150.00", USD), incomeAccountId: asAccountId("gl.income"), taxable: true,
  });
  const i = await store.getItem(T, "i1");
  assert.equal(i?.unitPrice?.toDecimalString(), "150.00");
  assert.equal(i?.type, "SERVICE");
  assert.equal(i?.taxable, true);
});

test("upsert updates in place and hydrate loads into an in-memory store", async () => {
  const store = await fresh();
  await store.upsertCustomer(T, { id: "c1", name: "Old Name" });
  await store.upsertCustomer(T, { id: "c1", name: "New Name" });
  assert.equal((await store.listCustomers(T)).length, 1);

  await store.upsertVendor(T, { id: "v1", name: "V" });
  await store.upsertItem(T, { id: "i1", name: "I", type: "NON_INVENTORY" });

  const mem = new InMemoryMasterDataStore();
  await store.hydrate(T, mem);
  assert.equal(mem.getCustomer(T, "c1")?.name, "New Name");
  assert.equal(mem.getVendor(T, "v1")?.name, "V");
  assert.equal(mem.getItem(T, "i1")?.type, "NON_INVENTORY");
});

test("master data is isolated per tenant", async () => {
  const store = await fresh();
  await store.upsertCustomer("acme", { id: "c1", name: "Acme Cust" });
  await store.upsertCustomer("beta", { id: "c1", name: "Beta Cust" });
  assert.equal((await store.getCustomer("acme", "c1"))?.name, "Acme Cust");
  assert.equal((await store.getCustomer("beta", "c1"))?.name, "Beta Cust");
});
