import { test } from "node:test";
import assert from "node:assert/strict";
import {
  InMemoryPeriodStore,
  PeriodClosedError,
  PostingEngine,
  asIdempotencyKey,
  asPeriodKey,
} from "../src/index.js";
import { AUG, POST_AT, PROV, TENANT, saleCommand, standardCoa } from "./helpers.js";
import { InMemoryLedgerStore } from "../src/index.js";

const SEP = asPeriodKey("2026-09");

function engineOn(store: InMemoryLedgerStore, periods: InMemoryPeriodStore): PostingEngine {
  return new PostingEngine(standardCoa(), store, periods);
}

test("PeriodStore defaults to OPEN and posts normally", async () => {
  const periods = new InMemoryPeriodStore();
  assert.equal(await periods.status(TENANT, AUG), "OPEN");
  const store = new InMemoryLedgerStore();
  const entry = await engineOn(store, periods).post(saleCommand("100.00"), { postedAt: POST_AT });
  assert.equal(entry.sequence, 1);
});

test("a locked period rejects a post and appends nothing", async () => {
  const periods = new InMemoryPeriodStore();
  const store = new InMemoryLedgerStore();
  const engine = engineOn(store, periods);

  await periods.lock(TENANT, AUG);
  assert.equal(await periods.status(TENANT, AUG), "LOCKED");

  await assert.rejects(() => engine.post(saleCommand("100.00"), { postedAt: POST_AT }), PeriodClosedError);
  assert.equal((await store.list(TENANT)).length, 0);
});

test("a locked period rejects a reversal into it", async () => {
  const periods = new InMemoryPeriodStore();
  const store = new InMemoryLedgerStore();
  const engine = engineOn(store, periods);

  const original = await engine.post(saleCommand("100.00", "orig"), { postedAt: POST_AT });
  await periods.lock(TENANT, AUG);

  await assert.rejects(
    () =>
      engine.reverse(TENANT, original.id, {
        idempotencyKey: asIdempotencyKey("rev-1"),
        periodKey: AUG,
        entryDate: "2026-08-16",
        postedAt: "2026-08-16T00:00:00Z",
        provenance: PROV,
      }),
    PeriodClosedError,
  );
  assert.equal((await store.list(TENANT)).length, 1); // only the original
});

test("locking one period leaves other periods and tenants open", async () => {
  const periods = new InMemoryPeriodStore();
  const store = new InMemoryLedgerStore();
  const engine = engineOn(store, periods);

  await periods.lock(TENANT, AUG);

  // A different period for the same tenant still posts.
  const sep = await engine.post(
    { ...saleCommand("50.00", "sep"), periodKey: SEP },
    { postedAt: POST_AT },
  );
  assert.equal(sep.sequence, 1);
  assert.equal(await periods.status(TENANT, SEP), "OPEN");
});

test("unlock re-opens a sealed period for a prior-period adjustment", async () => {
  const periods = new InMemoryPeriodStore();
  const store = new InMemoryLedgerStore();
  const engine = engineOn(store, periods);

  await periods.lock(TENANT, AUG);
  await assert.rejects(() => engine.post(saleCommand("100.00"), { postedAt: POST_AT }), PeriodClosedError);

  await periods.unlock(TENANT, AUG);
  const entry = await engine.post(saleCommand("100.00"), { postedAt: POST_AT });
  assert.equal(entry.sequence, 1);
});
