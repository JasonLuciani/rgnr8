import { test } from "node:test";
import assert from "node:assert/strict";
import { Money, USD } from "@rgnr8/ledger-kernel";
import { ARSubledger } from "../src/index.js";

const usd = (s: string) => Money.fromDecimal(s, USD);

function ledger(): ARSubledger {
  const ar = new ARSubledger(USD);
  ar.addInvoice({ id: "INV-1", customerId: "acme", issueDate: "2026-07-01", dueDate: "2026-07-31", amount: usd("10000.00") });
  ar.addInvoice({ id: "INV-2", customerId: "acme", issueDate: "2026-08-01", dueDate: "2026-08-31", amount: usd("6000.00") });
  ar.addInvoice({ id: "INV-3", customerId: "beta", issueDate: "2026-05-01", dueDate: "2026-05-31", amount: usd("4000.00") });
  return ar;
}

test("control balance is the sum of open receivables", () => {
  const ar = ledger();
  assert.equal(ar.controlBalance().toDecimalString(), "20000.00");
});

test("partial payment reduces open amount and sets PARTIAL", () => {
  const ar = ledger();
  const inv = ar.applyPayment({ documentId: "INV-1", date: "2026-08-05", amount: usd("4000.00") });
  assert.equal(inv.openAmount.toDecimalString(), "6000.00");
  assert.equal(inv.status, "PARTIAL");
  assert.equal(ar.controlBalance().toDecimalString(), "16000.00");
});

test("full payment settles the invoice and records the paid date", () => {
  const ar = ledger();
  ar.applyPayment({ documentId: "INV-1", date: "2026-08-05", amount: usd("4000.00") });
  const inv = ar.applyPayment({ documentId: "INV-1", date: "2026-08-12", amount: usd("6000.00") });
  assert.equal(inv.status, "PAID");
  assert.equal(inv.openAmount.toDecimalString(), "0.00");
  assert.equal(inv.settledDate, "2026-08-12");
  assert.ok(!ar.openInvoices().some((i) => i.id === "INV-1"));
});

test("overpayment is clamped to zero", () => {
  const ar = ledger();
  const inv = ar.applyPayment({ documentId: "INV-2", date: "2026-09-01", amount: usd("9999.00") });
  assert.equal(inv.openAmount.toDecimalString(), "0.00");
  assert.equal(inv.status, "PAID");
});

test("aging buckets open receivables by days past due", () => {
  const ar = ledger();
  const aging = ar.aging("2026-08-15");
  const by = new Map(aging.buckets.map((b) => [b.label, b.amount.toDecimalString()]));
  // INV-3 due 2026-05-31 -> ~76 days late (61-90); INV-1 due 2026-07-31 -> ~15 days (1-30);
  // INV-2 due 2026-08-31 -> not yet due (Current)
  assert.equal(by.get("61-90"), "4000.00");
  assert.equal(by.get("1-30"), "10000.00");
  assert.equal(by.get("Current"), "6000.00");
  assert.equal(aging.total.toDecimalString(), "20000.00");
});

test("customer histories come from fully settled invoices", () => {
  const ar = ledger();
  ar.applyPayment({ documentId: "INV-1", date: "2026-08-07", amount: usd("10000.00") }); // due 07-31, paid 08-07
  const hist = ar.customerHistories();
  assert.equal(hist.length, 1);
  assert.equal(hist[0]?.customerId, "acme");
  assert.deepEqual(hist[0]?.observations, [{ dueDate: "2026-07-31", paidDate: "2026-08-07" }]);
});

test("disputed invoices are marked and stay disputed on partial payment", () => {
  const ar = new ARSubledger(USD);
  ar.addInvoice({ id: "D1", customerId: "acme", issueDate: "2026-08-01", dueDate: "2026-08-31", amount: usd("5000.00"), disputed: true });
  assert.equal(ar.get("D1")?.status, "DISPUTED");
  const inv = ar.applyPayment({ documentId: "D1", date: "2026-09-01", amount: usd("1000.00") });
  assert.equal(inv.status, "DISPUTED");
});

test("write-off clears the open balance", () => {
  const ar = ledger();
  const inv = ar.writeOff("INV-3", "2026-09-01");
  assert.equal(inv.status, "WRITTEN_OFF");
  assert.equal(ar.controlBalance().toDecimalString(), "16000.00");
});
