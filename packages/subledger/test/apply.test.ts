import { test } from "node:test";
import assert from "node:assert/strict";
import { Money, USD } from "@rgnr8/ledger-kernel";
import { APSubledger, ARSubledger, applyPaymentsToAP, applyReceiptsToAR, type CashItem } from "../src/index.js";

const usd = (s: string) => Money.fromDecimal(s, USD);

function ar(): ARSubledger {
  const l = new ARSubledger(USD);
  l.addInvoice({ id: "INV-201", customerId: "northwind", issueDate: "2026-07-05", dueDate: "2026-08-20", amount: usd("22000.00") });
  l.addInvoice({ id: "INV-202", customerId: "contoso", issueDate: "2026-07-20", dueDate: "2026-08-31", amount: usd("16500.00") });
  return l;
}

test("applies a receipt to an invoice by remittance reference", () => {
  const l = ar();
  const receipts: CashItem[] = [
    { id: "rcpt-1", date: "2026-08-25", amount: usd("22000.00"), description: "ACH payment re INV-201" },
  ];
  const res = applyReceiptsToAR(l, receipts);
  assert.equal(res.applied.length, 1);
  assert.equal(res.applied[0]?.documentId, "INV-201");
  assert.equal(res.applied[0]?.reason, "reference");
  assert.equal(l.get("INV-201")?.status, "PAID");
  assert.equal(l.controlBalance().toDecimalString(), "16500.00");
});

test("applies by counterparty + exact amount when there is no reference", () => {
  const l = ar();
  const receipts: CashItem[] = [
    { id: "rcpt-2", date: "2026-09-01", amount: usd("16500.00"), description: "deposit", counterparty: "Contoso Ltd" },
  ];
  const res = applyReceiptsToAR(l, receipts, { "Contoso Ltd": "contoso" });
  assert.equal(res.applied[0]?.documentId, "INV-202");
  assert.equal(res.applied[0]?.reason, "counterparty_amount");
  assert.equal(l.get("INV-202")?.status, "PAID");
});

test("ambiguous amount for the same customer is left unmatched (never guessed)", () => {
  const l = new ARSubledger(USD);
  l.addInvoice({ id: "A1", customerId: "acme", issueDate: "2026-08-01", dueDate: "2026-08-31", amount: usd("5000.00") });
  l.addInvoice({ id: "A2", customerId: "acme", issueDate: "2026-08-02", dueDate: "2026-09-01", amount: usd("5000.00") });
  const res = applyReceiptsToAR(l, [{ id: "r", date: "2026-09-05", amount: usd("5000.00"), description: "pmt", counterparty: "Acme" }], { Acme: "acme" });
  assert.equal(res.applied.length, 0);
  assert.deepEqual(res.unmatched, ["r"]);
});

test("a receipt that matches nothing is reported unmatched", () => {
  const l = ar();
  const res = applyReceiptsToAR(l, [{ id: "x", date: "2026-09-01", amount: usd("999.00"), description: "mystery" }]);
  assert.deepEqual(res.unmatched, ["x"]);
  assert.equal(l.controlBalance().toDecimalString(), "38500.00");
});

test("AP payments apply to bills by reference", () => {
  const ap = new APSubledger(USD);
  ap.addBill({ id: "BILL-88", vendorId: "adobe", billDate: "2026-08-05", dueDate: "2026-09-05", amount: usd("1800.00") });
  const res = applyPaymentsToAP(ap, [{ id: "p1", date: "2026-09-04", amount: usd("1800.00"), description: "Paid BILL-88 to Adobe" }]);
  assert.equal(res.applied[0]?.documentId, "BILL-88");
  assert.equal(ap.get("BILL-88")?.status, "PAID");
  assert.equal(ap.controlBalance().toDecimalString(), "0.00");
});

test("a partial receipt by reference reduces the open balance", () => {
  const l = ar();
  const res = applyReceiptsToAR(l, [{ id: "r", date: "2026-08-25", amount: usd("10000.00"), description: "partial INV-201" }]);
  assert.equal(res.applied[0]?.reason, "reference");
  assert.equal(l.get("INV-201")?.openAmount.toDecimalString(), "12000.00");
  assert.equal(l.get("INV-201")?.status, "PARTIAL");
});
