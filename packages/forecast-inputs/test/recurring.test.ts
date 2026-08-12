import { test } from "node:test";
import assert from "node:assert/strict";
import { TransactionKind } from "@rgnr8/ingestion";
import { detectRecurring } from "../src/index.js";
import { txn } from "./helpers.js";

test("detects a monthly recurring outflow", () => {
  const txns = [
    txn("chk", "2026-06-01", "-4000.00", "Rent Co", TransactionKind.PURCHASE),
    txn("chk", "2026-07-01", "-4000.00", "Rent Co", TransactionKind.PURCHASE),
    txn("chk", "2026-08-01", "-4000.00", "Rent Co", TransactionKind.PURCHASE),
  ];
  const found = detectRecurring(txns);
  assert.equal(found.length, 1);
  const r = found[0]!;
  assert.equal(r.recurrence.frequency, "MONTHLY");
  assert.equal(r.direction, "OUTFLOW");
  assert.equal(r.category, "OTHER_OUTFLOW");
  assert.equal(r.amount.minor, 400000);
  assert.equal(r.recurrence.anchor, "2026-08-01"); // most recent occurrence
  assert.equal(r.label, "Rent Co");
});

test("detects biweekly payroll and preserves the payroll category", () => {
  const txns = [
    txn("chk", "2026-07-10", "-16000.00", "Payroll", TransactionKind.PAYROLL_NET),
    txn("chk", "2026-07-24", "-16000.00", "Payroll", TransactionKind.PAYROLL_NET),
    txn("chk", "2026-08-07", "-16000.00", "Payroll", TransactionKind.PAYROLL_NET),
  ];
  const found = detectRecurring(txns);
  assert.equal(found.length, 1);
  assert.equal(found[0]?.recurrence.frequency, "BIWEEKLY");
  assert.equal(found[0]?.category, "PAYROLL_NET");
});

test("detects a recurring inflow (retainer deposits)", () => {
  const txns = [
    txn("chk", "2026-06-15", "9000.00", "Globex retainer", TransactionKind.DEPOSIT),
    txn("chk", "2026-07-15", "9000.00", "Globex retainer", TransactionKind.DEPOSIT),
    txn("chk", "2026-08-15", "9000.00", "Globex retainer", TransactionKind.DEPOSIT),
  ];
  const found = detectRecurring(txns);
  assert.equal(found[0]?.direction, "INFLOW");
  assert.equal(found[0]?.category, "CUSTOMER_RECEIPT");
});

test("does not flag one-offs or amount-unstable groups", () => {
  const txns = [
    txn("chk", "2026-08-02", "-99.00", "Random shop", TransactionKind.PURCHASE),
    // same vendor but wildly varying amounts -> not a stable recurring charge
    txn("chk", "2026-06-01", "-50.00", "Varies Inc", TransactionKind.PURCHASE),
    txn("chk", "2026-07-01", "-500.00", "Varies Inc", TransactionKind.PURCHASE),
    txn("chk", "2026-08-01", "-3000.00", "Varies Inc", TransactionKind.PURCHASE),
  ];
  assert.equal(detectRecurring(txns).length, 0);
});

test("does not flag inconsistent cadence", () => {
  const txns = [
    txn("chk", "2026-06-01", "-100.00", "Irregular", TransactionKind.PURCHASE),
    txn("chk", "2026-06-05", "-100.00", "Irregular", TransactionKind.PURCHASE),
    txn("chk", "2026-08-01", "-100.00", "Irregular", TransactionKind.PURCHASE),
  ];
  assert.equal(detectRecurring(txns).length, 0);
});
