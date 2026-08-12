import { test } from "node:test";
import assert from "node:assert/strict";
import { PeriodClosedError, asPeriodKey, asTenantId } from "@rgnr8/ledger-kernel";
import { PgLedgerStore, SqlPeriodStore, type Pool, type QueryResult } from "../src/index.js";
import { PROV, POST_AT, engineWith, makePool, saleCommand } from "./support.js";

const TENANT = asTenantId("acme");
const AUG = asPeriodKey("2026-08");
const SEP = asPeriodKey("2026-09");

interface RecordedQuery {
  text: string;
  values?: unknown[];
}

/** Wrap a Pool so every query text + params is recorded on the pool and any
 * client it hands out — lets us assert the tenant GUC is bound (RLS). */
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

test("status defaults to OPEN when no lock row exists", async () => {
  const pool = makePool();
  const periods = new SqlPeriodStore(pool);
  await periods.migrate();
  assert.equal(await periods.status(TENANT, AUG), "OPEN");
});

test("lock upserts LOCKED and status round-trips; unlock re-opens", async () => {
  const pool = makePool();
  const periods = new SqlPeriodStore(pool);
  await periods.migrate();

  await periods.lock(TENANT, AUG);
  assert.equal(await periods.status(TENANT, AUG), "LOCKED");

  // Re-locking is idempotent (upsert, not a duplicate-key error).
  await periods.lock(TENANT, AUG);
  assert.equal(await periods.status(TENANT, AUG), "LOCKED");

  await periods.unlock(TENANT, AUG);
  assert.equal(await periods.status(TENANT, AUG), "OPEN");
});

test("locks are scoped per tenant and per period", async () => {
  const pool = makePool();
  const periods = new SqlPeriodStore(pool);
  await periods.migrate();

  await periods.lock(TENANT, AUG);
  assert.equal(await periods.status(TENANT, SEP), "OPEN"); // other period
  assert.equal(await periods.status(asTenantId("beta"), AUG), "OPEN"); // other tenant
});

test("DURABILITY: a FRESH store over the same backing DB still sees the period LOCKED", async () => {
  // One shared pg-mem database == one durable Postgres. Locking through one
  // store, then constructing a brand-new store over the SAME pool, simulates a
  // process restart: the lock must survive because it lives in the table, not
  // in a process-local Map.
  const pool = makePool();

  const before = new SqlPeriodStore(pool);
  await before.migrate();
  await before.lock(TENANT, AUG);

  // Simulated restart: a new store instance, no shared in-process state.
  const afterRestart = new SqlPeriodStore(pool);
  assert.equal(await afterRestart.status(TENANT, AUG), "LOCKED");
});

test("the posting engine wired to SqlPeriodStore rejects posts and reversals into a locked period", async () => {
  const pool = makePool();
  const ledger = new PgLedgerStore(pool);
  await ledger.migrate(); // CORE_DDL creates ledger_period too
  const periods = new SqlPeriodStore(pool);
  const engine = engineWith(ledger, periods);

  // A pre-lock post succeeds and gives us an entry to try to reverse.
  const original = await engine.post(saleCommand("acme", "100.00", "orig"), { postedAt: POST_AT });

  await periods.lock(TENANT, AUG);

  await assert.rejects(
    () => engine.post(saleCommand("acme", "250.00", "after-lock"), { postedAt: POST_AT }),
    PeriodClosedError,
  );

  await assert.rejects(
    () =>
      engine.reverse(TENANT, original.id, {
        idempotencyKey: "rev-locked" as never,
        periodKey: AUG,
        entryDate: "2026-08-20",
        postedAt: "2026-08-20T00:00:00Z",
        provenance: PROV,
      }),
    PeriodClosedError,
  );

  // Only the pre-lock entry was ever written.
  assert.equal((await ledger.list(TENANT)).length, 1);

  // After unlock, a fresh engine over the same DB can post again.
  await periods.unlock(TENANT, AUG);
  const resumed = await engineWith(ledger, new SqlPeriodStore(pool)).post(
    saleCommand("acme", "300.00", "after-unlock"),
    { postedAt: POST_AT },
  );
  assert.equal(resumed.sequence, 2);
});

test("binds the app.tenant_id GUC on lock and status so RLS engages", async () => {
  const log: RecordedQuery[] = [];
  const periods = new SqlPeriodStore(recordingPool(makePool(), log));
  await periods.migrate();

  log.length = 0;
  await periods.lock(TENANT, AUG);
  const lockBinds = log.filter((q) => q.text.includes("set_config('app.tenant_id'"));
  assert.ok(lockBinds.length >= 1, "lock must bind the tenant GUC");
  assert.deepEqual(lockBinds[0]?.values, ["acme"]);
  assert.ok(log.some((q) => q.text === "BEGIN"), "the GUC binding runs inside a transaction");

  log.length = 0;
  await periods.status(TENANT, AUG);
  const statusBinds = log.filter((q) => q.text.includes("set_config('app.tenant_id'"));
  assert.ok(statusBinds.length >= 1, "status must bind the tenant GUC");
  assert.deepEqual(statusBinds[0]?.values, ["acme"]);
});
