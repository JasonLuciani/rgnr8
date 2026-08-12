import { test } from "node:test";
import assert from "node:assert/strict";
import {
  computeTrialBalance,
  asIdempotencyKey,
  DuplicateIdempotencyKeyError,
  USD,
} from "../src/index.js";
import { AUG, POST_AT, PROV, TENANT, fixture, saleCommand } from "./helpers.js";

test("reversal appends a new entry with swapped sides and never mutates the original", async () => {
  const { engine, store } = fixture();
  const original = await engine.post(saleCommand("100.00", "orig"), { postedAt: POST_AT });

  const reversal = await engine.reverse(TENANT, original.id, {
    idempotencyKey: asIdempotencyKey("rev-1"),
    periodKey: AUG,
    entryDate: "2026-08-16",
    postedAt: "2026-08-16T00:00:00Z",
    provenance: PROV,
  });

  assert.equal(reversal.status, "REVERSAL");
  assert.equal(reversal.reversalOf, original.id);
  assert.equal(reversal.sequence, 2);
  assert.equal((await store.list(TENANT)).length, 2);

  const originalNow = await store.getById(TENANT, original.id);
  assert.equal(originalNow?.status, "POSTED");
  assert.deepEqual(
    originalNow?.lines.map((l) => l.side),
    ["DEBIT", "CREDIT"],
  );
  assert.deepEqual(
    reversal.lines.map((l) => l.side),
    ["CREDIT", "DEBIT"],
  );
});

test("after a reversal the ledger nets to zero", async () => {
  const { engine, coa, store } = fixture();
  const original = await engine.post(saleCommand("100.00", "orig"), { postedAt: POST_AT });
  await engine.reverse(TENANT, original.id, {
    idempotencyKey: asIdempotencyKey("rev-1"),
    periodKey: AUG,
    entryDate: "2026-08-16",
    postedAt: "2026-08-16T00:00:00Z",
    provenance: PROV,
  });

  const tb = await computeTrialBalance(store, TENANT, coa, USD);
  assert.ok(tb.inBalance);
  assert.equal(tb.totalDebit.toDecimalString(), "0.00");
  assert.equal(tb.totalCredit.toDecimalString(), "0.00");
});

test("reversal is idempotent on its own key", async () => {
  const { engine, store } = fixture();
  const original = await engine.post(saleCommand("100.00", "orig"), { postedAt: POST_AT });
  const opts = {
    idempotencyKey: asIdempotencyKey("rev-1"),
    periodKey: AUG,
    entryDate: "2026-08-16",
    postedAt: "2026-08-16T00:00:00Z",
    provenance: PROV,
  };
  const r1 = await engine.reverse(TENANT, original.id, opts);
  const r2 = await engine.reverse(TENANT, original.id, opts);
  assert.equal(r1.id, r2.id);
  assert.equal((await store.list(TENANT)).length, 2);
});

test("reversal rejects a reused idempotency key with a divergent payload", async () => {
  const { engine, store } = fixture();
  const first = await engine.post(saleCommand("100.00", "orig-1"), { postedAt: POST_AT });
  const second = await engine.post(saleCommand("250.00", "orig-2"), { postedAt: POST_AT });

  await engine.reverse(TENANT, first.id, {
    idempotencyKey: asIdempotencyKey("rev-key"),
    periodKey: AUG,
    entryDate: "2026-08-16",
    postedAt: "2026-08-16T00:00:00Z",
    provenance: PROV,
  });

  // Same idempotency key, but now reversing a DIFFERENT original (different
  // amounts) → the fingerprint diverges, so it must throw rather than silently
  // returning the first reversal (matching post()'s guard).
  await assert.rejects(
    engine.reverse(TENANT, second.id, {
      idempotencyKey: asIdempotencyKey("rev-key"),
      periodKey: AUG,
      entryDate: "2026-08-16",
      postedAt: "2026-08-16T00:00:00Z",
      provenance: PROV,
    }),
    DuplicateIdempotencyKeyError,
  );

  // A divergence on entryDate for the same original is likewise rejected.
  await assert.rejects(
    engine.reverse(TENANT, first.id, {
      idempotencyKey: asIdempotencyKey("rev-key"),
      periodKey: AUG,
      entryDate: "2026-08-20", // differs from the sealed reversal's entryDate
      postedAt: "2026-08-20T00:00:00Z",
      provenance: PROV,
    }),
    DuplicateIdempotencyKeyError,
  );

  // No extra rows were written by the rejected replays.
  assert.equal((await store.list(TENANT)).length, 3);
});
