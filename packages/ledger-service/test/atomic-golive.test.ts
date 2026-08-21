import { test } from "node:test";
import assert from "node:assert/strict";
import { newDb } from "pg-mem";
import {
  AccountType,
  ChartOfAccounts,
  USD,
  asAccountId,
  asTenantId,
  type Account,
} from "@rgnr8/ledger-kernel";
import { runInTransaction, type Pool, type PoolClient } from "@rgnr8/ledger-postgres";
import { InMemoryBackend, PostgresBackend } from "../src/index.js";

const TENANT = asTenantId("acme");

function pgBackend(): PostgresBackend {
  const db = newDb();
  const pg = db.adapters.createPg();
  const pool = new pg.Pool();
  return new PostgresBackend(pool as never, { enforceRls: false });
}

function chart(): ChartOfAccounts {
  const acct = (id: string, code: string, type: AccountType): Account => ({
    id: asAccountId(id), code, name: code, type, currency: USD,
  });
  return new ChartOfAccounts([
    acct("cash", "1000", AccountType.ASSET),
    acct("equity", "3000", AccountType.EQUITY),
  ]);
}

test("atomicGoLive COMMITS: a chart persisted inside the unit survives", async () => {
  const backend = pgBackend();
  await backend.migrate();
  assert.ok(backend.atomicGoLive, "the Postgres backend exposes atomicGoLive");

  await backend.atomicGoLive!(async (_store, _periods, chartStore) => {
    await chartStore.saveChart(TENANT, chart());
    return null;
  });

  const loaded = await backend.chart(TENANT);
  assert.equal(loaded.list().length, 2, "the chart committed");
});

/** A pool over one recording client — captures the exact BEGIN/COMMIT/ROLLBACK
 * sequence so we can prove the transaction boundary without depending on pg-mem's
 * (limited) rollback fidelity. */
function recordingPool(log: string[]): Pool {
  const client: PoolClient = {
    query: (text: string) => {
      log.push(text.trim().split(/\s+/)[0]!.toUpperCase());
      return Promise.resolve({ rows: [] });
    },
    release: () => log.push("RELEASE"),
  };
  return {
    query: (t: string) => client.query(t),
    connect: () => Promise.resolve(client),
  };
}

test("runInTransaction commits on success and rolls back on failure", async () => {
  // success → one BEGIN, then COMMIT (never ROLLBACK); inner store BEGIN/COMMITs
  // are suppressed by the shared-txn pool, so they don't appear on the client.
  const okLog: string[] = [];
  await runInTransaction(recordingPool(okLog), async (txPool) => {
    await txPool.query("INSERT INTO account VALUES (1)"); // a store write
    const c = await txPool.connect();
    await c.query("BEGIN"); // an inner store transaction — must be suppressed
    await c.query("INSERT INTO ledger VALUES (2)");
    await c.query("COMMIT");
  });
  assert.deepEqual(okLog, ["BEGIN", "INSERT", "INSERT", "COMMIT", "RELEASE"]);

  // failure → BEGIN then ROLLBACK, never COMMIT.
  const failLog: string[] = [];
  await assert.rejects(
    runInTransaction(recordingPool(failLog), async (txPool) => {
      await txPool.query("INSERT INTO account VALUES (1)");
      throw new Error("boom");
    }),
    /boom/,
  );
  assert.deepEqual(failLog, ["BEGIN", "INSERT", "ROLLBACK", "RELEASE"]);
});

test("the in-memory backend has no atomicGoLive (sequential path)", () => {
  const backend = new InMemoryBackend();
  assert.equal(backend.atomicGoLive, undefined);
});
