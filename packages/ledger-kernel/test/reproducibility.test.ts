import { test } from "node:test";
import assert from "node:assert/strict";
import { computeTrialBalance, USD } from "../src/index.js";
import { POST_AT, TENANT, fixture, saleCommand } from "./helpers.js";

/**
 * The kernel uses no wall clock or randomness internally (timestamps and keys
 * are supplied by the caller), so the same command stream always yields the
 * same ids, sequences, and balances.
 */
test("the same command stream reproduces identical ids, sequences, and trial balance", async () => {
  async function run() {
    const { engine, coa, store } = fixture();
    const ids: string[] = [];
    for (const amt of ["100.00", "250.50", "13.37"]) {
      ids.push((await engine.post(saleCommand(amt, `s-${amt}`), { postedAt: POST_AT })).id);
    }
    const tb = await computeTrialBalance(store, TENANT, coa, USD);
    return { ids, total: tb.totalDebit.toDecimalString() };
  }
  const a = await run();
  const b = await run();
  assert.deepEqual(a.ids, b.ids);
  assert.deepEqual(a.ids, ["acme:1", "acme:2", "acme:3"]);
  assert.equal(a.total, b.total);
  assert.equal(a.total, "363.87");
});
