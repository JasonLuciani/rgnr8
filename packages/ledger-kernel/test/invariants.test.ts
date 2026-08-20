import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DuplicateIdempotencyKeyError,
  PeriodClosedError,
  computeTrialBalance,
  accountBalances,
  USD,
} from "../src/index.js";
import { ACCT, AUG, POST_AT, TENANT, fixture, saleCommand, usd } from "./helpers.js";

test("idempotency: replaying the same key returns the original and appends nothing", async () => {
  const { engine, store } = fixture();
  const first = await engine.post(saleCommand("100.00", "dup"), { postedAt: POST_AT });
  const second = await engine.post(saleCommand("100.00", "dup"), { postedAt: POST_AT });
  assert.equal(first.id, second.id);
  assert.equal((await store.list(TENANT)).length, 1);
});

test("idempotency: reusing a key with a DIFFERENT payload is rejected", async () => {
  const { engine } = fixture();
  await engine.post(saleCommand("100.00", "same-key"), { postedAt: POST_AT });
  await assert.rejects(
    () => engine.post(saleCommand("250.00", "same-key"), { postedAt: POST_AT }),
    DuplicateIdempotencyKeyError,
  );
});

test("idempotency: reusing a key with different DIMENSIONS is rejected", async () => {
  const { engine } = fixture();
  const withDim = (dept: string): typeof base => {
    const base = saleCommand("100.00", "dim-key");
    return {
      ...base,
      lines: base.lines.map((l, i) =>
        i === 0 ? { ...l, dimensions: { department: dept } } : l,
      ),
    };
  };
  await engine.post(withDim("sales"), { postedAt: POST_AT });
  // Same amounts/accounts, different dimensional coding — must NOT silently dedupe.
  await assert.rejects(
    () => engine.post(withDim("marketing"), { postedAt: POST_AT }),
    DuplicateIdempotencyKeyError,
  );
  // The identical re-post (same dimensions) still dedupes.
  const replay = await engine.post(withDim("sales"), { postedAt: POST_AT });
  assert.ok(replay.id);
});

test("idempotency: reusing a key with a different LINE MEMO is rejected", async () => {
  const { engine } = fixture();
  const withLineMemo = (memo: string): typeof base => {
    const base = saleCommand("100.00", "linememo-key");
    return { ...base, lines: base.lines.map((l, i) => (i === 0 ? { ...l, memo } : l)) };
  };
  await engine.post(withLineMemo("card"), { postedAt: POST_AT });
  await assert.rejects(
    () => engine.post(withLineMemo("wire"), { postedAt: POST_AT }),
    DuplicateIdempotencyKeyError,
  );
});

test("idempotency: reusing a key with different PROVENANCE is rejected", async () => {
  const { engine } = fixture();
  const base = saleCommand("100.00", "prov-key");
  await engine.post(base, { postedAt: POST_AT });
  const reMapped = {
    ...base,
    provenance: { ...base.provenance, mappingVersion: "map-2" },
  };
  await assert.rejects(
    () => engine.post(reMapped, { postedAt: POST_AT }),
    DuplicateIdempotencyKeyError,
  );
});

test("period lock: posting into a closed period is rejected until reopened", async () => {
  const { engine, periods, store } = fixture();
  periods.close(AUG);
  await assert.rejects(() => engine.post(saleCommand("100.00"), { postedAt: POST_AT }), PeriodClosedError);
  assert.equal((await store.list(TENANT)).length, 0);

  periods.reopen(AUG);
  const entry = await engine.post(saleCommand("100.00"), { postedAt: POST_AT });
  assert.equal(entry.sequence, 1);
});

test("immutability: a posted entry and its lines are deeply frozen", async () => {
  const { engine } = fixture();
  const entry = (await engine.post(saleCommand("100.00"), { postedAt: POST_AT })) as unknown as {
    sequence: number;
    lines: unknown[];
    provenance: unknown;
  };
  assert.ok(Object.isFrozen(entry));
  assert.ok(Object.isFrozen(entry.lines));
  assert.ok(Object.isFrozen(entry.lines[0]));
  assert.ok(Object.isFrozen(entry.provenance));
  assert.throws(() => {
    "use strict";
    (entry as { sequence: number }).sequence = 99;
  }, TypeError);
});

test("immutability: a stored entry cannot be mutated through the returned reference", async () => {
  const { engine, store } = fixture();
  const returned = await engine.post(saleCommand("100.00"), { postedAt: POST_AT });
  try {
    (returned as unknown as { memo: string }).memo = "tampered";
  } catch {
    /* frozen — throws in strict mode, ignored otherwise */
  }
  assert.equal((await store.getById(TENANT, returned.id))?.memo, "Cash sale");
});

test("trial balance ties and reflects correct debit/credit balances", async () => {
  const { engine, coa, store } = fixture();
  // Sale $1,000 (Dr Cash / Cr Revenue); Payroll $600 (Dr Payroll / Cr Cash).
  await engine.post(saleCommand("1000.00", "s1"), { postedAt: POST_AT });
  await engine.post(
    {
      ...saleCommand("600.00", "p1"),
      memo: "Payroll",
      lines: [
        { accountId: ACCT.payroll, side: "DEBIT", amount: usd("600.00") },
        { accountId: ACCT.cash, side: "CREDIT", amount: usd("600.00") },
      ],
    },
    { postedAt: POST_AT },
  );

  const tb = await computeTrialBalance(store, TENANT, coa, USD);
  assert.ok(tb.inBalance);
  assert.equal(tb.totalDebit.toDecimalString(), tb.totalCredit.toDecimalString());

  const byCode = new Map(tb.rows.map((r) => [r.code, r]));
  assert.equal(byCode.get("1000")?.debit.toDecimalString(), "400.00"); // Cash 1000 - 600
  assert.equal(byCode.get("4000")?.credit.toDecimalString(), "1000.00"); // Revenue
  assert.equal(byCode.get("6000")?.debit.toDecimalString(), "600.00"); // Payroll
});

test("account balances are expressed in normal-balance orientation", async () => {
  const { engine, coa, store } = fixture();
  await engine.post(saleCommand("1000.00", "s1"), { postedAt: POST_AT });
  const bal = await accountBalances(store, TENANT, coa, USD);
  assert.equal(bal.get(ACCT.cash)?.toDecimalString(), "1000.00"); // asset, debit-normal
  assert.equal(bal.get(ACCT.revenue)?.toDecimalString(), "1000.00"); // revenue, credit-normal, positive
});
