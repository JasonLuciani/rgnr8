/**
 * Richer emit: opening cash from a reconciled bank account + AR/AP subledgers.
 *   node --import tsx examples/emit_with_ar.ts <out.json>
 * Now the forecast gets invoice-level receivables, real customer payment
 * histories, and open bills — not just detected recurring flows.
 */
import { writeFileSync } from "node:fs";
import { Money, USD } from "@rgnr8/ledger-kernel";
import { BankPlaidLikeAdapter, IngestionPipeline, type RawRecord } from "@rgnr8/ingestion";
import { bookItemsFromCanonical, reconcileStatement, type Statement } from "@rgnr8/reconciliation";
import { APSubledger, ARSubledger, reconcileControl, subledgerForecastParts } from "@rgnr8/subledger";
import { buildForecastInputs, toJson } from "../src/index.js";

const out = process.argv[2] ?? "/tmp/forecast_inputs_ar.json";
const usd = (s: string) => Money.fromDecimal(s, USD);

// --- bank feed: opening + recurring rent -----------------------------------
function raw(id: string, date: string, providerAmount: string, name: string): RawRecord {
  return {
    provider: "plaid",
    accountId: "chk",
    externalId: id,
    payload: { transaction_id: id, account_id: "chk", date, pending: false, amount: providerAmount, name, category: ["x"] },
    fetchedAt: "2026-08-31T00:00:00Z",
    sourceVersion: "1",
  };
}
const pipeline = new IngestionPipeline().register(new BankPlaidLikeAdapter("acme"));
pipeline.ingest([
  raw("r1", "2026-06-01", "7200.00", "Rent Co"),
  raw("r2", "2026-07-01", "7200.00", "Rent Co"),
  raw("r3", "2026-08-01", "7200.00", "Rent Co"),
]);
const active = pipeline.active();
const augBook = bookItemsFromCanonical(active, "chk").filter((b) => b.date >= "2026-08-01");
const sumAug = augBook.reduce((a, b) => a.plus(b.amount), Money.zero(USD));
const statement: Statement = {
  accountId: "chk",
  periodStart: "2026-08-01",
  periodEnd: "2026-08-31",
  openingBalance: usd("45000.00"),
  closingBalance: usd("45000.00").plus(sumAug),
  lines: augBook.map((b) => ({ id: `s-${b.id}`, date: b.date, amount: b.amount, description: b.description, externalId: b.externalId })),
};
const recon = reconcileStatement(statement, augBook);

// --- AR subledger: open receivables + one settled (history) ----------------
const ar = new ARSubledger(USD);
ar.addInvoice({ id: "INV-201", customerId: "northwind", issueDate: "2026-07-05", dueDate: "2026-08-20", amount: usd("22000.00") });
ar.addInvoice({ id: "INV-202", customerId: "contoso", issueDate: "2026-07-20", dueDate: "2026-08-31", amount: usd("16500.00") });
ar.addInvoice({ id: "INV-150", customerId: "northwind", issueDate: "2026-06-01", dueDate: "2026-06-30", amount: usd("18000.00") });
ar.applyPayment({ documentId: "INV-150", date: "2026-07-08", amount: usd("18000.00") }); // paid 8 days late -> history

// --- AP subledger: open bills ----------------------------------------------
const ap = new APSubledger(USD);
ap.addBill({ id: "BILL-88", vendorId: "adobe", billDate: "2026-08-05", dueDate: "2026-09-05", amount: usd("1800.00") });

// control reconciliations (subledger vs GL control accounts)
const arCtl = reconcileControl("AR", ar.controlBalance(), usd("38500.00"));
const apCtl = reconcileControl("AP", ap.controlBalance(), usd("1800.00"));

const parts = subledgerForecastParts(ar, ap);
const { dto, notes } = buildForecastInputs({
  reconciliations: [recon],
  transactions: active,
  asOf: statement.periodEnd,
  subledger: parts,
});

writeFileSync(out, toJson(dto, true));
console.error(`bank recon: ${recon.status}; AR control ${arCtl.balanced ? "tied" : "DRIFT"}; AP control ${apCtl.balanced ? "tied" : "DRIFT"}`);
console.error(notes.join("; "));
console.error(`wrote ${out}`);
