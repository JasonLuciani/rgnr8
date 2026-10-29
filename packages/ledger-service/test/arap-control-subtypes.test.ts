import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * AR/AP control accounts are found by what they are, not by RGNR8's own codes.
 * A chart brought over from QuickBooks numbers accounts its own way: there is no
 * "1200" or "2000" in it, and posting to those codes either failed or, worse,
 * hit an unrelated account that happened to carry the number.
 */

const NOW = "2026-10-29T00:00:00Z";
const call = (s: LedgerService, method: string, path: string, body: unknown = ""): Promise<ServiceResponse> =>
  s.handle({ method, path, query: {}, body: typeof body === "string" ? body : JSON.stringify(body), headers: {} });

async function quickbooksChart(): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  const accounts = [
    { code: "10", name: "Operating Checking", type: "ASSET", subtype: "BANK" },
    { code: "84", name: "Accounts Receivable (A/R)", type: "ASSET", subtype: "ACCOUNTS_RECEIVABLE" },
    { code: "33", name: "Accounts Payable (A/P)", type: "LIABILITY", subtype: "ACCOUNTS_PAYABLE" },
    { code: "1200", name: "Prepaid Insurance", type: "ASSET" }, // a 1200 that is NOT A/R
    { code: "1", name: "Services", type: "REVENUE" },
    { code: "60", name: "Supplies", type: "EXPENSE" },
  ];
  for (const a of accounts) {
    assert.equal((await call(s, "POST", "/t/acme/accounts", a)).status, 201, a.code);
  }
  await call(s, "POST", "/t/acme/customers", { id: "c1", name: "Northwind" });
  await call(s, "POST", "/t/acme/vendors", { id: "v1", name: "Copyshop" });
  return s;
}

async function balance(s: LedgerService, code: string): Promise<bigint> {
  const tb = (await call(s, "GET", "/t/acme/trial-balance")).body as { rows: Array<Record<string, string>> };
  const row = tb.rows.find((r) => r["code"] === code);
  return row ? BigInt(row["debit_minor"]!) - BigInt(row["credit_minor"]!) : 0n;
}

test("invoices, bills and payments use the chart's own A/R, A/P and bank accounts", async () => {
  const s = await quickbooksChart();
  assert.equal((await call(s, "POST", "/t/acme/invoices", {
    id: "INV-1", party_id: "c1", date: "2026-10-01", lines: [{ unit_amount_minor: "50000", account_code: "1" }],
  })).status, 201);
  assert.equal((await call(s, "POST", "/t/acme/bills", {
    id: "B-1", party_id: "v1", date: "2026-10-02", lines: [{ unit_amount_minor: "12000", account_code: "60" }],
  })).status, 201);
  assert.equal(await balance(s, "84"), 50000n);
  assert.equal(await balance(s, "33"), -12000n);
  assert.equal(await balance(s, "1200"), 0n, "the unrelated 1200 is untouched");

  assert.equal((await call(s, "POST", "/t/acme/invoices/INV-1/payments", {
    date: "2026-10-10", amount_minor: "50000",
  })).status, 201);
  assert.equal(await balance(s, "84"), 0n);
  assert.equal(await balance(s, "10"), 50000n);
});

test("an explicit control account still wins", async () => {
  const s = await quickbooksChart();
  await call(s, "POST", "/t/acme/accounts", { code: "85", name: "A/R - Retail", type: "ASSET", subtype: "ACCOUNTS_RECEIVABLE" });
  await call(s, "POST", "/t/acme/invoices", {
    id: "INV-2", party_id: "c1", date: "2026-10-01", accounts: { arCode: "85" },
    lines: [{ unit_amount_minor: "1000", account_code: "1" }],
  });
  assert.equal(await balance(s, "85"), 1000n);
  // With two A/R accounts and no override, the lowest code is the one used.
  await call(s, "POST", "/t/acme/invoices", {
    id: "INV-3", party_id: "c1", date: "2026-10-01", lines: [{ unit_amount_minor: "700", account_code: "1" }],
  });
  assert.equal(await balance(s, "84"), 700n);
});

test("with several bank accounts, money in or out must say which one it went through", async () => {
  const s = await quickbooksChart();
  await call(s, "POST", "/t/acme/accounts", { code: "11", name: "Savings", type: "ASSET", subtype: "BANK" });
  // Creating the invoice needs no bank account, so it still works.
  assert.equal((await call(s, "POST", "/t/acme/invoices", {
    id: "INV-1", party_id: "c1", date: "2026-10-01", lines: [{ unit_amount_minor: "50000", account_code: "1" }],
  })).status, 201);
  const guessed = await call(s, "POST", "/t/acme/invoices/INV-1/payments", { date: "2026-10-10", amount_minor: "50000" });
  assert.equal(guessed.status, 400);
  assert.match(String((guessed.body as Record<string, unknown>)["error"]), /2 bank accounts.*bank_code: 10, 11/);
  assert.equal(await balance(s, "84"), 50000n, "nothing posted");
  const chosen = await call(s, "POST", "/t/acme/invoices/INV-1/payments", {
    date: "2026-10-10", amount_minor: "50000", bank_code: "11",
  });
  assert.equal(chosen.status, 201);
  assert.equal(await balance(s, "11"), 50000n);
});
