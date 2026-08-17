import { test } from "node:test";
import assert from "node:assert/strict";

import { computeTrialBalance, accountBalances, asIdempotencyKey, asPeriodKey } from "../src/index.js";
import type { PostCommand } from "../src/index.js";
import { ACCT, POST_AT, PROV, TENANT, fixture, usd } from "./helpers.js";
import { USD } from "../src/index.js";

function sale(amount: string, entryDate: string, period: string, k: string): PostCommand {
  return {
    tenantId: TENANT,
    idempotencyKey: asIdempotencyKey(k),
    periodKey: asPeriodKey(period),
    currency: USD,
    entryDate,
    memo: "Cash sale",
    provenance: PROV,
    lines: [
      { accountId: ACCT.cash, side: "DEBIT", amount: usd(amount) },
      { accountId: ACCT.revenue, side: "CREDIT", amount: usd(amount) },
    ],
  };
}

test("trial balance without a window sums every posted entry", async () => {
  const { engine, store, coa } = fixture();
  await engine.post(sale("100.00", "2026-07-10", "2026-07", "jul"), { postedAt: POST_AT });
  await engine.post(sale("40.00", "2026-08-10", "2026-08", "aug"), { postedAt: POST_AT });

  const tb = await computeTrialBalance(store, TENANT, coa, USD);
  const revenue = tb.rows.find((r) => r.accountId === ACCT.revenue);
  assert.equal(revenue?.credit.toDecimalString(), "140.00");
  assert.equal(tb.inBalance, true);
});

test("income-statement window scopes to a single period", async () => {
  const { engine, store, coa } = fixture();
  await engine.post(sale("100.00", "2026-07-10", "2026-07", "jul"), { postedAt: POST_AT });
  await engine.post(sale("40.00", "2026-08-10", "2026-08", "aug"), { postedAt: POST_AT });

  const aug = await computeTrialBalance(store, TENANT, coa, USD, {
    from: "2026-08-01",
    to: "2026-08-31",
  });
  const revenue = aug.rows.find((r) => r.accountId === ACCT.revenue);
  assert.equal(revenue?.credit.toDecimalString(), "40.00"); // July excluded
  assert.equal(aug.inBalance, true);
});

test("balance-sheet as-of window is cumulative up to the date", async () => {
  const { engine, store, coa } = fixture();
  await engine.post(sale("100.00", "2026-07-10", "2026-07", "jul"), { postedAt: POST_AT });
  await engine.post(sale("40.00", "2026-08-10", "2026-08", "aug"), { postedAt: POST_AT });

  // as of end of July: only the July sale has happened
  const asOfJul = await computeTrialBalance(store, TENANT, coa, USD, { to: "2026-07-31" });
  const cashJul = asOfJul.rows.find((r) => r.accountId === ACCT.cash);
  assert.equal(cashJul?.debit.toDecimalString(), "100.00");

  // as of end of August: cumulative both sales
  const asOfAug = await computeTrialBalance(store, TENANT, coa, USD, { to: "2026-08-31" });
  const cashAug = asOfAug.rows.find((r) => r.accountId === ACCT.cash);
  assert.equal(cashAug?.debit.toDecimalString(), "140.00");
});

test("accountBalances honors the same window", async () => {
  const { engine, store, coa } = fixture();
  await engine.post(sale("100.00", "2026-07-10", "2026-07", "jul"), { postedAt: POST_AT });
  await engine.post(sale("40.00", "2026-08-10", "2026-08", "aug"), { postedAt: POST_AT });

  const balances = await accountBalances(store, TENANT, coa, USD, {
    from: "2026-08-01",
    to: "2026-08-31",
  });
  // revenue normal balance is credit; oriented positive == 40.00 for August only
  assert.equal(balances.get(ACCT.revenue)?.toDecimalString(), "40.00");
});
