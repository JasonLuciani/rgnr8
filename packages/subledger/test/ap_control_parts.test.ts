import { test } from "node:test";
import assert from "node:assert/strict";
import { Money, USD } from "@rgnr8/ledger-kernel";
import {
  APSubledger,
  ARSubledger,
  reconcileControl,
  subledgerForecastParts,
} from "../src/index.js";

const usd = (s: string) => Money.fromDecimal(s, USD);

test("AP tracks open bills, partials, and control balance", () => {
  const ap = new APSubledger(USD);
  ap.addBill({ id: "B1", vendorId: "aws", billDate: "2026-08-01", dueDate: "2026-08-20", amount: usd("1200.00") });
  ap.addBill({ id: "B2", vendorId: "rent", billDate: "2026-08-01", dueDate: "2026-08-31", amount: usd("7000.00") });
  ap.applyPayment({ documentId: "B1", date: "2026-08-18", amount: usd("1200.00") });
  assert.equal(ap.get("B1")?.status, "PAID");
  assert.equal(ap.controlBalance().toDecimalString(), "7000.00");
  assert.equal(ap.openBills().length, 1);
});

test("control reconciliation ties subledger to the GL control account", () => {
  const ar = new ARSubledger(USD);
  ar.addInvoice({ id: "INV-1", customerId: "acme", issueDate: "2026-08-01", dueDate: "2026-08-31", amount: usd("10000.00") });

  const clean = reconcileControl("AR", ar.controlBalance(), usd("10000.00"));
  assert.equal(clean.balanced, true);
  assert.equal(clean.difference.toDecimalString(), "0.00");

  const drift = reconcileControl("AR", ar.controlBalance(), usd("9500.00"));
  assert.equal(drift.balanced, false);
  assert.equal(drift.difference.toDecimalString(), "500.00");
});

test("forecast parts emit open invoices, histories, and open bills in contract shape", () => {
  const ar = new ARSubledger(USD);
  ar.addInvoice({ id: "INV-1", customerId: "acme", issueDate: "2026-07-01", dueDate: "2026-07-31", amount: usd("10000.00") });
  ar.addInvoice({ id: "INV-2", customerId: "acme", issueDate: "2026-08-01", dueDate: "2026-08-31", amount: usd("6000.00") });
  // settle INV-1 to produce a payment history; partially pay INV-2
  ar.applyPayment({ documentId: "INV-1", date: "2026-08-07", amount: usd("10000.00") });
  ar.applyPayment({ documentId: "INV-2", date: "2026-08-15", amount: usd("2000.00") });

  const ap = new APSubledger(USD);
  ap.addBill({ id: "B1", vendorId: "aws", billDate: "2026-08-01", dueDate: "2026-08-20", amount: usd("1200.00") });

  const parts = subledgerForecastParts(ar, ap);

  // Only the open invoice (INV-2, now PARTIAL with 4000 open) is emitted.
  assert.equal(parts.invoices.length, 1);
  assert.equal(parts.invoices[0]?.id, "INV-2");
  assert.equal(parts.invoices[0]?.status, "PARTIAL");
  assert.deepEqual(parts.invoices[0]?.open_amount, { minor: 400000, currency: "USD" });

  // History from the settled invoice.
  assert.equal(parts.customer_histories.length, 1);
  assert.deepEqual(parts.customer_histories[0]?.observations, [
    { due_date: "2026-07-31", paid_date: "2026-08-07" },
  ]);

  // Open bill.
  assert.equal(parts.bills.length, 1);
  assert.deepEqual(parts.bills[0]?.amount, { minor: 120000, currency: "USD" });
});
