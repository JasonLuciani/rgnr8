import { test } from "node:test";
import assert from "node:assert/strict";
import { computeTrialBalance, asIdempotencyKey, asTenantId, USD } from "@rgnr8/ledger-kernel";
import { ACCT, AUG, POST_AT, PROV, engineWith, freshStore, saleCommand, standardCoa, usd } from "./support.js";

/**
 * These exercise the full kernel posting engine on top of the PostgreSQL
 * adapter — proving the adapter satisfies the LedgerStore contract end to end.
 */

test("engine posts through the adapter and the trial balance ties", async () => {
  const store = await freshStore();
  const engine = engineWith(store);
  const tenant = asTenantId("acme");

  await engine.post(saleCommand("acme", "1000.00", "s1"), { postedAt: POST_AT });
  await engine.post(
    {
      ...saleCommand("acme", "600.00", "p1"),
      memo: "Payroll",
      lines: [
        { accountId: ACCT.payroll, side: "DEBIT", amount: usd("600.00") },
        { accountId: ACCT.cash, side: "CREDIT", amount: usd("600.00") },
      ],
    },
    { postedAt: POST_AT },
  );

  const tb = await computeTrialBalance(store, tenant, standardCoa(), USD);
  assert.ok(tb.inBalance);
  const byCode = new Map(tb.rows.map((r) => [r.code, r]));
  assert.equal(byCode.get("1000")?.debit.toDecimalString(), "400.00");
  assert.equal(byCode.get("4000")?.credit.toDecimalString(), "1000.00");
  assert.equal(byCode.get("6000")?.debit.toDecimalString(), "600.00");
});

test("engine reversal through the adapter nets the ledger to zero and preserves the original", async () => {
  const store = await freshStore();
  const engine = engineWith(store);
  const tenant = asTenantId("acme");

  const original = await engine.post(saleCommand("acme", "100.00", "orig"), { postedAt: POST_AT });
  const reversal = await engine.reverse(tenant, original.id, {
    idempotencyKey: asIdempotencyKey("rev-1"),
    periodKey: AUG,
    entryDate: "2026-08-16",
    postedAt: "2026-08-16T00:00:00Z",
    provenance: PROV,
  });

  assert.equal(reversal.status, "REVERSAL");
  assert.equal(reversal.reversalOf, original.id);

  const originalNow = await store.getById(tenant, original.id);
  assert.deepEqual(originalNow?.lines.map((l) => l.side), ["DEBIT", "CREDIT"]);
  assert.deepEqual(reversal.lines.map((l) => l.side), ["CREDIT", "DEBIT"]);

  const tb = await computeTrialBalance(store, tenant, standardCoa(), USD);
  assert.equal(tb.totalDebit.toDecimalString(), "0.00");
  assert.equal(tb.totalCredit.toDecimalString(), "0.00");
});

test("idempotent replay through the engine writes one row", async () => {
  const store = await freshStore();
  const engine = engineWith(store);
  const a = await engine.post(saleCommand("acme", "100.00", "dup"), { postedAt: POST_AT });
  const b = await engine.post(saleCommand("acme", "100.00", "dup"), { postedAt: POST_AT });
  assert.equal(a.id, b.id);
  assert.equal((await store.list(asTenantId("acme"))).length, 1);
});
