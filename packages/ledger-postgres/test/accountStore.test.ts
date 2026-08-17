import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountSubtype,
  AccountType,
  BusinessCategory,
  ChartOfAccounts,
  USD,
  asAccountId,
  asTenantId,
  type Account,
} from "@rgnr8/ledger-kernel";
import { PgAccountStore } from "../src/index.js";
import { makePool } from "./support.js";

const T1 = asTenantId("acme");
const T2 = asTenantId("beta");

function acct(id: string, code: string, name: string, type: AccountType, subtype?: AccountSubtype): Account {
  return { id: asAccountId(id), code, name, type, currency: USD, ...(subtype ? { subtype } : {}) };
}

async function freshAccountStore(): Promise<PgAccountStore> {
  const store = new PgAccountStore(makePool());
  await store.migrate();
  return store;
}

test("persists a chart of accounts and rehydrates a validated ChartOfAccounts", async () => {
  const store = await freshAccountStore();
  const coa = new ChartOfAccounts([
    acct("cash", "1000", "Cash", AccountType.ASSET, AccountSubtype.BANK),
    acct("ar", "1100", "Accounts Receivable", AccountType.ASSET, AccountSubtype.ACCOUNTS_RECEIVABLE),
    acct("rev", "4000", "Sales", AccountType.REVENUE, AccountSubtype.INCOME),
  ]);
  await store.saveChart(T1, coa);

  const loaded = await store.loadChart(T1);
  assert.equal(loaded.list().length, 3);
  const cash = loaded.get(asAccountId("cash"))!;
  assert.equal(cash.code, "1000");
  assert.equal(cash.subtype, AccountSubtype.BANK);
});

test("upsert updates an existing account in place", async () => {
  const store = await freshAccountStore();
  await store.upsert(T1, acct("cash", "1000", "Cash", AccountType.ASSET));
  await store.upsert(T1, acct("cash", "1000", "Operating Cash", AccountType.ASSET, AccountSubtype.BANK));
  const accounts = await store.listAccounts(T1);
  assert.equal(accounts.length, 1);
  assert.equal(accounts[0]!.name, "Operating Cash");
  assert.equal(accounts[0]!.subtype, AccountSubtype.BANK);
});

test("deactivate soft-hides an account without deleting it", async () => {
  const store = await freshAccountStore();
  await store.upsert(T1, acct("old", "9999", "Legacy", AccountType.EXPENSE));
  await store.deactivate(T1, asAccountId("old"));
  const accounts = await store.listAccounts(T1);
  assert.equal(accounts.find((a) => a.id === asAccountId("old"))!.active, false);
});

test("accounts are isolated per tenant", async () => {
  const store = await freshAccountStore();
  await store.upsert(T1, acct("cash", "1000", "Acme Cash", AccountType.ASSET));
  await store.upsert(T2, acct("cash", "1000", "Beta Cash", AccountType.ASSET));
  assert.equal((await store.listAccounts(T1))[0]!.name, "Acme Cash");
  assert.equal((await store.listAccounts(T2))[0]!.name, "Beta Cash");
});

test("seedFromTemplate seeds a tenant's chart from a business-category template", async () => {
  const store = await freshAccountStore();
  const seeded = await store.seedFromTemplate(asTenantId("newco"), BusinessCategory.CONTRACTOR_TRADES);
  assert.ok(seeded.length >= 25);
  const loaded = await store.loadChart(asTenantId("newco"));
  assert.ok(loaded.list().some((a) => a.name === "Job Materials"));
  // rehydrated chart is valid (subtype/type consistent) and per-tenant
  assert.equal((await store.listAccounts(asTenantId("other"))).length, 0);
});
