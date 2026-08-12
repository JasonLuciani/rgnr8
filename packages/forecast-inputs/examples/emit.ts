/**
 * Emit side of the cross-language loop.
 *   node --import tsx examples/emit.ts <out.json>
 * Ingests a bank feed, reconciles the current month, builds the ForecastInputs
 * DTO (gated on the clean reconciliation), and writes it as JSON.
 */
import { writeFileSync } from "node:fs";
import { Money, USD } from "@rgnr8/ledger-kernel";
import { BankPlaidLikeAdapter, IngestionPipeline, type RawRecord } from "@rgnr8/ingestion";
import { bookItemsFromCanonical, reconcileStatement, type Statement } from "@rgnr8/reconciliation";
import { buildForecastInputs, toJson } from "../src/index.js";

const outPath = process.argv[2] ?? "/tmp/forecast_inputs.json";

// Plaid-like provider sign: positive = money OUT of the account.
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

const feed: RawRecord[] = [
  // monthly rent (out) — 3 months of history
  raw("r1", "2026-06-01", "4000.00", "Rent Co"),
  raw("r2", "2026-07-01", "4000.00", "Rent Co"),
  raw("r3", "2026-08-01", "4000.00", "Rent Co"),
  // biweekly payroll (out)
  raw("p1", "2026-07-10", "16000.00", "Payroll"),
  raw("p2", "2026-07-24", "16000.00", "Payroll"),
  raw("p3", "2026-08-07", "16000.00", "Payroll"),
  // monthly retainer (in) — provider negative = money in
  raw("d1", "2026-06-15", "-9000.00", "Globex retainer"),
  raw("d2", "2026-07-15", "-9000.00", "Globex retainer"),
  raw("d3", "2026-08-15", "-9000.00", "Globex retainer"),
];

const pipeline = new IngestionPipeline().register(new BankPlaidLikeAdapter("acme"));
pipeline.ingest(feed);
const active = pipeline.active();

// Reconcile just the current month (August) against a matching statement.
const augBook = bookItemsFromCanonical(active, "chk").filter((b) => b.date >= "2026-08-01");
const sumAug = augBook.reduce((acc, b) => acc.plus(b.amount), Money.zero(USD));
const statement: Statement = {
  accountId: "chk",
  periodStart: "2026-08-01",
  periodEnd: "2026-08-31",
  openingBalance: Money.fromDecimal("20000.00", USD),
  closingBalance: Money.fromDecimal("20000.00", USD).plus(sumAug),
  lines: augBook.map((b) => ({ id: `s-${b.id}`, date: b.date, amount: b.amount, description: b.description, externalId: b.externalId })),
};
const recon = reconcileStatement(statement, augBook);

const { dto, notes } = buildForecastInputs({
  reconciliations: [recon],
  transactions: active, // detection uses full history
  asOf: statement.periodEnd,
});

writeFileSync(outPath, toJson(dto, true));
console.error(`reconciliation: ${recon.status}; ${notes.join("; ")}`);
console.error(`wrote ${outPath} (opening ${dto.opening.available.minor} minor, ${dto.recurring.length} recurring flows)`);
