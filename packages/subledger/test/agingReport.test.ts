import { test } from "node:test";
import assert from "node:assert/strict";
import { Money, USD } from "@rgnr8/ledger-kernel";
import { APSubledger, ARSubledger, agingReportJson, apAgingReport, arAgingReport } from "../src/index.js";

const usd = (s: string) => Money.fromDecimal(s, USD);
const ASOF = "2026-08-31";

test("AR aging report breaks the balance out by customer and bucket", () => {
  const ar = new ARSubledger(USD);
  // Acme: one current, one 31-60 days late. Beta: one 90+ late.
  ar.addInvoice({ id: "i1", customerId: "acme", issueDate: "2026-08-20", dueDate: "2026-09-10", amount: usd("1000.00") });
  ar.addInvoice({ id: "i2", customerId: "acme", issueDate: "2026-06-15", dueDate: "2026-07-15", amount: usd("400.00") });
  ar.addInvoice({ id: "i3", customerId: "beta", issueDate: "2026-04-01", dueDate: "2026-05-01", amount: usd("250.00") });

  const report = arAgingReport(ar, ASOF, USD);
  assert.equal(report.rows.length, 2);
  const acme = report.rows.find((r) => r.partyId === "acme")!;
  assert.equal(acme.total.toDecimalString(), "1400.00");
  // Current bucket holds the not-yet-due invoice.
  const currentIdx = report.bucketLabels.indexOf("Current");
  assert.equal(acme.buckets[currentIdx]!.toDecimalString(), "1000.00");

  assert.equal(report.grandTotal.toDecimalString(), "1650.00");
  // Column totals sum to the grand total.
  const colSum = report.columnTotals.reduce((a, m) => a.plus(m), usd("0.00"));
  assert.equal(colSum.toDecimalString(), "1650.00");
});

test("AP aging report ages vendor bills and excludes paid ones", () => {
  const ap = new APSubledger(USD);
  ap.addBill({ id: "b1", vendorId: "supplyco", billDate: "2026-08-01", dueDate: "2026-08-15", amount: usd("600.00") });
  ap.addBill({ id: "b2", vendorId: "supplyco", billDate: "2026-08-05", dueDate: "2026-09-05", amount: usd("300.00") });
  ap.applyPayment({ documentId: "b1", amount: usd("600.00"), date: "2026-08-20" }); // fully paid → excluded

  const report = apAgingReport(ap, ASOF, USD);
  assert.equal(report.rows.length, 1);
  assert.equal(report.grandTotal.toDecimalString(), "300.00");
});

test("agingReportJson serializes to the aging/1 contract", () => {
  const ar = new ARSubledger(USD);
  ar.addInvoice({ id: "i1", customerId: "acme", issueDate: "2026-08-20", dueDate: "2026-09-10", amount: usd("1000.00") });
  const json = agingReportJson(arAgingReport(ar, ASOF, USD), "AR");
  assert.equal(json.contract, "aging/1");
  assert.equal(json.kind, "AR");
  assert.equal(json.grand_total_minor, "100000");
  assert.equal(json.rows[0].party_id, "acme");
  assert.equal(json.bucket_labels.length, json.rows[0].buckets_minor.length);
});
