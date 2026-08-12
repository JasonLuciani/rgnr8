import { test } from "node:test";
import assert from "node:assert/strict";
import { USD, asTenantId } from "@rgnr8/ledger-kernel";
import { ACCT, POST_AT, PROV, freshStore, makePool, usd } from "./support.js";
import { PgLedgerStore, type Pool, type QueryResult } from "../src/index.js";
import type { DraftEntry } from "@rgnr8/ledger-kernel";

interface RecordedQuery {
  text: string;
  values?: unknown[];
}

/** Wrap a Pool so every query text + params is recorded, on both the pool and
 * any client it hands out — lets us assert the tenant GUC is bound. */
function recordingPool(inner: Pool, log: RecordedQuery[]): Pool {
  return {
    query(text: string, values?: unknown[]): Promise<QueryResult> {
      log.push({ text, ...(values !== undefined ? { values } : {}) });
      return inner.query(text, values);
    },
    async connect() {
      const client = await inner.connect();
      return {
        query(text: string, values?: unknown[]): Promise<QueryResult> {
          log.push({ text, ...(values !== undefined ? { values } : {}) });
          return client.query(text, values);
        },
        release() {
          client.release();
        },
      };
    },
  };
}

function saleDraft(tenant: string, amount: string, key: string): DraftEntry {
  return {
    tenantId: asTenantId(tenant),
    idempotencyKey: key as never,
    periodKey: "2026-08" as never,
    currency: USD,
    entryDate: "2026-08-15",
    status: "POSTED",
    memo: "Cash sale",
    provenance: PROV,
    postedAt: POST_AT,
    lines: [
      { accountId: ACCT.cash, side: "DEBIT", amount: usd(amount) },
      { accountId: ACCT.revenue, side: "CREDIT", amount: usd(amount) },
    ],
  };
}

test("append then read back reconstructs the entry, money, and provenance exactly", async () => {
  const store = await freshStore();
  const posted = await store.append(saleDraft("acme", "1234.56", "k1"));

  assert.equal(posted.id, "acme:1");
  assert.equal(posted.sequence, 1);
  assert.equal(posted.status, "POSTED");
  assert.equal(posted.lines[0]?.amount.toDecimalString(), "1234.56");
  assert.equal(posted.lines[0]?.amount.minorUnits, 123456n);
  assert.equal(posted.provenance.sourceSystem, "test");

  const byId = await store.getById(asTenantId("acme"), posted.id);
  assert.equal(byId?.id, "acme:1");
  assert.equal(byId?.lines.length, 2);
  assert.equal(byId?.lines[1]?.amount.toDecimalString(), "1234.56");
  assert.equal(byId?.memo, "Cash sale");
});

test("sequences are contiguous and monotonic per tenant", async () => {
  const store = await freshStore();
  const a = await store.append(saleDraft("acme", "10.00", "a"));
  const b = await store.append(saleDraft("acme", "20.00", "b"));
  const c = await store.append(saleDraft("acme", "30.00", "c"));
  assert.deepEqual([a.sequence, b.sequence, c.sequence], [1, 2, 3]);
  assert.deepEqual([a.id, b.id, c.id], ["acme:1", "acme:2", "acme:3"]);
  assert.equal((await store.list(asTenantId("acme"))).length, 3);
});

test("idempotent append: same key returns the original and writes one row", async () => {
  const store = await freshStore();
  const first = await store.append(saleDraft("acme", "50.00", "dup"));
  const second = await store.append(saleDraft("acme", "50.00", "dup"));
  assert.equal(first.id, second.id);
  assert.equal((await store.list(asTenantId("acme"))).length, 1);
});

test("getBySequence and list return entries in order", async () => {
  const store = await freshStore();
  await store.append(saleDraft("acme", "10.00", "a"));
  await store.append(saleDraft("acme", "20.00", "b"));
  const second = await store.getBySequence(asTenantId("acme"), 2);
  assert.equal(second?.lines[0]?.amount.toDecimalString(), "20.00");
  const list = await store.list(asTenantId("acme"));
  assert.deepEqual(
    list.map((e) => e.sequence),
    [1, 2],
  );
});

test("tenant isolation: each tenant has its own sequence space and cannot see the other", async () => {
  const store = await freshStore();
  await store.append(saleDraft("acme", "10.00", "a1"));
  await store.append(saleDraft("beta", "99.00", "b1"));
  await store.append(saleDraft("acme", "20.00", "a2"));

  const acme = await store.list(asTenantId("acme"));
  const beta = await store.list(asTenantId("beta"));
  assert.deepEqual(acme.map((e) => e.id), ["acme:1", "acme:2"]);
  assert.deepEqual(beta.map((e) => e.id), ["beta:1"]);
  // Cross-tenant lookups return nothing.
  assert.equal(await store.getById(asTenantId("beta"), "acme:1" as never), undefined);
});

test("binds the app.tenant_id GUC on writes and reads so RLS engages", async () => {
  // pg-mem does not implement RLS, so we can't observe enforcement; instead we
  // assert the store issues the correct `set_config('app.tenant_id', ...)` call
  // (transaction-local) on both the write and the read path.
  const log: RecordedQuery[] = [];
  const store = new PgLedgerStore(recordingPool(makePool(), log));
  await store.migrate();

  log.length = 0; // ignore migration DDL
  await store.append(saleDraft("acme", "10.00", "k1"));
  const writeBinds = log.filter((q) => q.text.includes("set_config('app.tenant_id'"));
  assert.ok(writeBinds.length >= 1, "write path must bind the tenant GUC");
  assert.deepEqual(writeBinds[0]?.values, ["acme"]);
  assert.ok(
    log.some((q) => q.text === "BEGIN"),
    "the GUC binding runs inside a transaction",
  );

  log.length = 0;
  await store.list(asTenantId("acme"));
  const readBinds = log.filter((q) => q.text.includes("set_config('app.tenant_id'"));
  assert.ok(readBinds.length >= 1, "read path must bind the tenant GUC");
  assert.deepEqual(readBinds[0]?.values, ["acme"]);
});
