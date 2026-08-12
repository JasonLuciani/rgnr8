import { test } from "node:test";
import assert from "node:assert/strict";
import { Money, USD } from "@rgnr8/ledger-kernel";
import { TransactionKind } from "@rgnr8/ingestion";
import { reconcileStatement, type BookItem, type Statement } from "@rgnr8/reconciliation";
import { buildForecastInputs, toJson } from "../src/index.js";
import { txn } from "./helpers.js";

const usd = (s: string) => Money.fromDecimal(s, USD);

function cleanRecon(accountId: string, closing: string) {
  const stmt: Statement = {
    accountId,
    periodStart: "2026-08-01",
    periodEnd: "2026-08-31",
    openingBalance: usd("10000.00"),
    closingBalance: usd(closing),
    lines: [{ id: "l1", date: "2026-08-05", amount: usd(closing).minus(usd("10000.00")), description: "x", externalId: "c1" }],
  };
  const book: BookItem[] = [
    { id: "b1", date: "2026-08-05", amount: usd(closing).minus(usd("10000.00")), description: "x", externalId: "c1" },
  ];
  return reconcileStatement(stmt, book);
}

function dirtyRecon(accountId: string) {
  const stmt: Statement = {
    accountId,
    periodStart: "2026-08-01",
    periodEnd: "2026-08-31",
    openingBalance: usd("5000.00"),
    closingBalance: usd("6500.00"),
    lines: [
      { id: "l1", date: "2026-08-05", amount: usd("1000.00"), description: "ok", externalId: "d1" },
      { id: "l2", date: "2026-08-07", amount: usd("500.00"), description: "missing from books" },
    ],
  };
  const book: BookItem[] = [{ id: "b1", date: "2026-08-05", amount: usd("1000.00"), description: "ok", externalId: "d1" }];
  return reconcileStatement(stmt, book);
}

test("opening cash comes only from clean-reconciled accounts; dirty ones are excluded", () => {
  const chk = cleanRecon("chk", "13800.00");
  const sav = dirtyRecon("sav");

  const { dto, excludedAccounts, notes } = buildForecastInputs({
    reconciliations: [chk, sav],
    transactions: [],
  });

  assert.equal(dto.opening.available.minor, 1380000); // only chk
  assert.equal(dto.opening.verified, false); // an account was excluded
  assert.equal(dto.opening.as_of, "2026-08-31");
  assert.equal(excludedAccounts.length, 1);
  assert.equal(excludedAccounts[0]?.accountId, "sav");
  assert.ok(notes.some((n) => n.includes("excluded")));
});

test("only transactions from clean accounts feed recurring detection", () => {
  const chk = cleanRecon("chk", "13800.00");
  const sav = dirtyRecon("sav");
  const txns = [
    // chk (clean) — a monthly recurring charge
    txn("chk", "2026-06-01", "-4000.00", "Rent Co", TransactionKind.PURCHASE),
    txn("chk", "2026-07-01", "-4000.00", "Rent Co", TransactionKind.PURCHASE),
    txn("chk", "2026-08-01", "-4000.00", "Rent Co", TransactionKind.PURCHASE),
    // sav (dirty) — a recurring pattern that must NOT be trusted
    txn("sav", "2026-06-02", "-250.00", "Savings fee", TransactionKind.FEE),
    txn("sav", "2026-07-02", "-250.00", "Savings fee", TransactionKind.FEE),
    txn("sav", "2026-08-02", "-250.00", "Savings fee", TransactionKind.FEE),
  ];

  const { dto } = buildForecastInputs({ reconciliations: [chk, sav], transactions: txns });
  assert.equal(dto.recurring.length, 1);
  assert.equal(dto.recurring[0]?.label, "Rent Co");
});

test("fully clean set produces a verified opening position", () => {
  const chk = cleanRecon("chk", "13800.00");
  const { dto } = buildForecastInputs({ reconciliations: [chk], transactions: [], restrictedMinor: 800000n });
  assert.equal(dto.opening.verified, true);
  assert.equal(dto.opening.restricted.minor, 800000);
});

test("subledger parts are merged into the DTO", () => {
  const chk = cleanRecon("chk", "13800.00");
  const { dto, notes } = buildForecastInputs({
    reconciliations: [chk],
    transactions: [],
    subledger: {
      invoices: [
        { id: "INV-2", customer_id: "acme", issue_date: "2026-08-01", due_date: "2026-08-31", open_amount: { minor: 400000, currency: "USD" }, status: "PARTIAL" },
      ],
      customer_histories: [
        { customer_id: "acme", observations: [{ due_date: "2026-07-31", paid_date: "2026-08-07" }] },
      ],
      bills: [{ id: "B1", vendor_id: "aws", due_date: "2026-08-20", amount: { minor: 120000, currency: "USD" } }],
    },
  });
  assert.equal(dto.invoices.length, 1);
  assert.equal(dto.customer_histories.length, 1);
  assert.equal(dto.bills.length, 1);
  assert.ok(notes.some((n) => n.includes("subledger")));
});

test("emitted JSON carries the contract version and parses", () => {
  const chk = cleanRecon("chk", "13800.00");
  const { dto } = buildForecastInputs({ reconciliations: [chk], transactions: [] });
  const parsed = JSON.parse(toJson(dto)) as { contract: string; opening: { available: { minor: number } } };
  assert.equal(parsed.contract, "forecast-inputs/1");
  assert.equal(parsed.opening.available.minor, 1380000);
});
