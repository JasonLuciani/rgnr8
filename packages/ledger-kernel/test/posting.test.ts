import { test } from "node:test";
import assert from "node:assert/strict";
import {
  EmptyEntryError,
  LineCurrencyError,
  NonPositiveAmountError,
  UnbalancedEntryError,
  UnknownAccountError,
  Money,
  USD,
  defineCurrency,
  asAccountId,
  asIdempotencyKey,
} from "../src/index.js";
import { ACCT, POST_AT, PROV, TENANT, AUG, fixture, saleCommand, usd } from "./helpers.js";

test("a balanced entry posts and retains its lines, provenance, and sequence", async () => {
  const { engine, store } = fixture();
  const entry = await engine.post(saleCommand("100.00"), { postedAt: POST_AT });

  assert.equal(entry.sequence, 1);
  assert.equal(entry.id, "acme:1");
  assert.equal(entry.status, "POSTED");
  assert.equal(entry.lines.length, 2);
  assert.equal(entry.provenance.sourceSystem, "test");
  assert.equal((await store.list(TENANT)).length, 1);
});

test("sequences are monotonic per tenant", async () => {
  const { engine } = fixture();
  const a = await engine.post(saleCommand("10.00", "a"), { postedAt: POST_AT });
  const b = await engine.post(saleCommand("20.00", "b"), { postedAt: POST_AT });
  const c = await engine.post(saleCommand("30.00", "c"), { postedAt: POST_AT });
  assert.deepEqual([a.sequence, b.sequence, c.sequence], [1, 2, 3]);
});

test("an unbalanced entry is rejected", async () => {
  const { engine } = fixture();
  const cmd = saleCommand("100.00");
  const bad = {
    ...cmd,
    lines: [
      { accountId: ACCT.cash, side: "DEBIT" as const, amount: usd("100.00") },
      { accountId: ACCT.revenue, side: "CREDIT" as const, amount: usd("90.00") },
    ],
  };
  await assert.rejects(() => engine.post(bad, { postedAt: POST_AT }), UnbalancedEntryError);
});

test("an entry with fewer than two lines is rejected", async () => {
  const { engine } = fixture();
  const cmd = saleCommand("100.00");
  const bad = { ...cmd, lines: [{ accountId: ACCT.cash, side: "DEBIT" as const, amount: usd("100.00") }] };
  await assert.rejects(() => engine.post(bad, { postedAt: POST_AT }), EmptyEntryError);
});

test("a line referencing an unknown account is rejected", async () => {
  const { engine } = fixture();
  const cmd = saleCommand("100.00");
  const bad = {
    ...cmd,
    lines: [
      { accountId: asAccountId("ghost"), side: "DEBIT" as const, amount: usd("100.00") },
      { accountId: ACCT.revenue, side: "CREDIT" as const, amount: usd("100.00") },
    ],
  };
  await assert.rejects(() => engine.post(bad, { postedAt: POST_AT }), UnknownAccountError);
});

test("a non-positive line amount is rejected (sign is carried by side, not amount)", async () => {
  const { engine } = fixture();
  const cmd = saleCommand("100.00");
  const bad = {
    ...cmd,
    lines: [
      { accountId: ACCT.cash, side: "DEBIT" as const, amount: Money.zero(USD) },
      { accountId: ACCT.revenue, side: "CREDIT" as const, amount: usd("100.00") },
    ],
  };
  await assert.rejects(() => engine.post(bad, { postedAt: POST_AT }), NonPositiveAmountError);
});

test("a line whose currency differs from the entry is rejected", async () => {
  const { engine } = fixture();
  const eur = defineCurrency("EUR", 2);
  const cmd = saleCommand("100.00");
  const bad = {
    ...cmd,
    lines: [
      { accountId: ACCT.cash, side: "DEBIT" as const, amount: Money.fromDecimal("100.00", eur) },
      { accountId: ACCT.revenue, side: "CREDIT" as const, amount: usd("100.00") },
    ],
  };
  await assert.rejects(() => engine.post(bad, { postedAt: POST_AT }), LineCurrencyError);
});

test("nothing is appended when validation fails", async () => {
  const { engine, store } = fixture();
  const cmd = saleCommand("100.00");
  const bad = {
    ...cmd,
    lines: [
      { accountId: ACCT.cash, side: "DEBIT" as const, amount: usd("100.00") },
      { accountId: ACCT.revenue, side: "CREDIT" as const, amount: usd("1.00") },
    ],
  };
  await assert.rejects(() => engine.post(bad, { postedAt: POST_AT }));
  assert.equal((await store.list(TENANT)).length, 0);
});

test("a multi-line entry (payroll: expense + cash + liability) balances and posts", async () => {
  const { engine } = fixture();
  const entry = await engine.post(
    {
      tenantId: TENANT,
      idempotencyKey: asIdempotencyKey("payroll-1"),
      periodKey: AUG,
      currency: USD,
      entryDate: "2026-08-15",
      provenance: PROV,
      memo: "Payroll run",
      lines: [
        { accountId: ACCT.payroll, side: "DEBIT", amount: usd("5000.00") },
        { accountId: ACCT.cash, side: "CREDIT", amount: usd("4000.00") },
        { accountId: ACCT.loan, side: "CREDIT", amount: usd("1000.00") },
      ],
    },
    { postedAt: POST_AT },
  );
  assert.equal(entry.lines.length, 3);
  assert.equal(entry.status, "POSTED");
});
