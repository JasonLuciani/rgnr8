import { test } from "node:test";
import assert from "node:assert/strict";
import { Money, USD, asAccountId, type Provenance } from "@rgnr8/ledger-kernel";
import {
  AuditLog,
  build1099Report,
  discountDeadlineFor,
  discountMinor,
  documentTotal,
  dueDateFor,
  estimateToInvoice,
  inventoryAdjustmentCommand,
  materializeRecurring,
  occurrencesBetween,
  purchaseOrderToBill,
  NonPostingError,
  type DocPostContext,
  type Estimate,
  type PaymentTerms,
  type PurchaseOrder,
  type Vendor,
} from "../src/index.js";

const usd = (s: string) => Money.fromDecimal(s, USD);
const PROV: Provenance = {
  sourceSystem: "test", sourceObject: "x", sourceVersion: "1",
  effectiveDate: "2026-08-01", postedDate: "2026-08-01", ingestedAt: "2026-08-01T00:00:00Z",
  normalizationVersion: "n1", mappingVersion: "m1",
};
const CTX: DocPostContext = { tenantId: "acme", currency: USD, provenance: PROV };

// --- 1099 -------------------------------------------------------------------

test("1099 report files only 1099 vendors over the threshold with a tax id", () => {
  const vendors: Vendor[] = [
    { id: "v1", name: "Freelance Dev", is1099: true, taxId: "12-3456789" },
    { id: "v2", name: "Design Co", is1099: true, taxId: "98-7654321" },
    { id: "v3", name: "No TIN LLC", is1099: true }, // missing tax id
    { id: "v4", name: "Staples", is1099: false }, // not 1099
  ];
  const payments = [
    { vendorId: "v1", date: "2026-03-01", amount: usd("4000.00") },
    { vendorId: "v1", date: "2026-06-01", amount: usd("2500.00") },
    { vendorId: "v2", date: "2026-05-01", amount: usd("400.00") }, // under $600
    { vendorId: "v3", date: "2026-05-01", amount: usd("5000.00") },
    { vendorId: "v4", date: "2026-05-01", amount: usd("9999.00") }, // ignored (not 1099)
    { vendorId: "v1", date: "2025-12-31", amount: usd("1000.00") }, // wrong year
  ];
  const report = build1099Report(vendors, payments, 2026, USD);
  assert.equal(report.rows.length, 1);
  assert.equal(report.rows[0]!.vendorId, "v1");
  assert.equal(report.rows[0]!.nonemployeeCompensation.toDecimalString(), "6500.00");
  assert.deepEqual([...report.belowThreshold], ["v2"]);
  assert.deepEqual([...report.missingTaxId], ["v3"]);
  assert.equal(report.total.toDecimalString(), "6500.00");
});

// --- recurring --------------------------------------------------------------

test("monthly recurrence generates clamped month-end occurrences in a window", () => {
  const dates = occurrencesBetween(
    { frequency: "MONTHLY", interval: 1, startDate: "2026-01-31" },
    "2026-01-01",
    "2026-04-30",
  );
  assert.deepEqual(dates, ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]);
});

test("recurrence respects count and interval, and materializes idempotency keys", () => {
  const biweekly = occurrencesBetween(
    { frequency: "WEEKLY", interval: 2, startDate: "2026-08-03", count: 3 },
    "2026-01-01",
    "2026-12-31",
  );
  assert.deepEqual(biweekly, ["2026-08-03", "2026-08-17", "2026-08-31"]);

  const mat = materializeRecurring(
    { id: "rent", schedule: { frequency: "MONTHLY", interval: 1, startDate: "2026-08-01", count: 2 }, payload: { amount: "2400" } },
    "2026-01-01",
    "2026-12-31",
  );
  assert.deepEqual(mat.map((o) => o.idempotencyKey), ["rent:2026-08-01", "rent:2026-09-01"]);
});

// --- audit log --------------------------------------------------------------

test("audit log records history and filters by entity", () => {
  const log = new AuditLog();
  log.record({ at: "2026-08-01T10:00:00Z", actor: "jason", action: "customer.create", entityType: "customer", entityId: "c1", after: { name: "Acme" } });
  log.record({ at: "2026-08-02T10:00:00Z", actor: "jason", action: "customer.update", entityType: "customer", entityId: "c1", before: { name: "Acme" }, after: { name: "Acme Inc" } });
  log.record({ at: "2026-08-03T10:00:00Z", actor: "sys", action: "period.close", entityType: "period", entityId: "2026-08" });
  const hist = log.history("customer", "c1");
  assert.equal(hist.length, 2);
  assert.equal(hist[0]!.id < hist[1]!.id, true);
  assert.equal(log.query({ actor: "sys" }).length, 1);
});

// --- inventory --------------------------------------------------------------

test("inventory write-down debits the adjustment account and credits inventory", () => {
  const cmd = inventoryAdjustmentCommand(
    { id: "adj1", date: "2026-08-31", itemId: "widget", quantityDelta: -5, valueDelta: usd("-125.00") },
    { inventoryAccountId: asAccountId("gl.inventory"), adjustmentAccountId: asAccountId("gl.shrinkage") },
    CTX,
  )!;
  const dr = cmd.lines.find((l) => l.side === "DEBIT")!;
  const cr = cmd.lines.find((l) => l.side === "CREDIT")!;
  assert.equal(String(dr.accountId), "gl.shrinkage");
  assert.equal(dr.amount.toDecimalString(), "125.00");
  assert.equal(String(cr.accountId), "gl.inventory");
  // zero-value adjustment posts nothing
  assert.equal(
    inventoryAdjustmentCommand({ id: "z", date: "2026-08-31", itemId: "w", quantityDelta: 1, valueDelta: usd("0.00") },
      { inventoryAccountId: asAccountId("gl.inventory"), adjustmentAccountId: asAccountId("gl.shrinkage") }, CTX),
    undefined,
  );
});

// --- terms & methods --------------------------------------------------------

test("payment terms drive due date, discount deadline, and exact discount", () => {
  const terms: PaymentTerms = { id: "2/10n30", name: "2/10 Net 30", netDays: 30, discountPpm: 20_000, discountDays: 10 };
  assert.equal(dueDateFor(terms, "2026-08-01"), "2026-08-31");
  assert.equal(discountDeadlineFor(terms, "2026-08-01"), "2026-08-11");
  // 2% of 1000.00 = 20.00
  assert.equal(discountMinor(terms, usd("1000.00").minorUnits), 2000n);
});

// --- non-posting docs -------------------------------------------------------

test("an accepted estimate converts to an invoice; a draft one refuses", () => {
  const est: Estimate = {
    id: "e1", customerId: "c1", date: "2026-08-01", status: "ACCEPTED",
    lines: [{ description: "Consulting", quantity: 10, unitAmount: usd("150.00"), accountId: asAccountId("gl.income") }],
  };
  const inv = estimateToInvoice(est, { id: "i1", date: "2026-08-05", dueDate: "2026-09-04" });
  assert.equal(inv.customerId, "c1");
  assert.equal(inv.lines.length, 1);
  assert.equal(documentTotal(est.lines, USD).toDecimalString(), "1500.00");

  const draft: Estimate = { ...est, status: "DRAFT" };
  assert.throws(() => estimateToInvoice(draft, { id: "i2", date: "2026-08-05", dueDate: "2026-09-04" }), NonPostingError);
});

test("a received purchase order converts to a bill", () => {
  const po: PurchaseOrder = {
    id: "po1", vendorId: "v1", date: "2026-08-01", status: "RECEIVED",
    lines: [{ description: "Parts", quantity: 4, unitAmount: usd("50.00"), accountId: asAccountId("gl.cogs") }],
  };
  const bill = purchaseOrderToBill(po, { id: "b1", date: "2026-08-10", dueDate: "2026-09-09" });
  assert.equal(bill.vendorId, "v1");
  assert.equal(bill.lines.length, 1);
});
