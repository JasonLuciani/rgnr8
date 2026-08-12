/**
 * Runnable demo:  node --import tsx examples/demo.ts
 * Ingests a small bank feed, reconciles it against a statement, prints the
 * result and the publish gate, then shows what a missing bank line does.
 */
import { Money, USD } from "@rgnr8/ledger-kernel";
import { BankPlaidLikeAdapter, IngestionPipeline, type RawRecord } from "@rgnr8/ingestion";
import {
  bookItemsFromCanonical,
  isCleanForPublish,
  reconcileStatement,
  signReconciliation,
  type Reconciliation,
  type Statement,
  type StatementLine,
} from "../src/index.js";

const usd = (s: string) => Money.fromDecimal(s, USD);

const feed: RawRecord[] = [
  { transaction_id: "t1", account_id: "chk", date: "2026-08-05", pending: false, amount: "-5000.00", name: "Client ACH", category: ["Deposit"] },
  { transaction_id: "t2", account_id: "chk", date: "2026-08-08", pending: false, amount: "1200.00", name: "Vendor payment", category: ["Service"] },
  { transaction_id: "t3", account_id: "chk", date: "2026-08-30", pending: false, amount: "800.00", name: "Contractor", category: ["Service"] },
].map((t) => ({ provider: "plaid", accountId: t.account_id, externalId: t.transaction_id, payload: t, fetchedAt: "2026-08-31T00:00:00Z", sourceVersion: "1" }));

const pipeline = new IngestionPipeline().register(new BankPlaidLikeAdapter("acme"));
pipeline.ingest(feed);
const book = bookItemsFromCanonical(pipeline.active(), "chk");

function lineFrom(b: (typeof book)[number], id: string): StatementLine {
  return { id, date: b.date, amount: b.amount, description: b.description };
}

function print(title: string, r: Reconciliation): void {
  console.log(`\n${title}`);
  console.log(`  status: ${r.status}   clean for publish: ${isCleanForPublish(r) ? "YES" : "NO"}`);
  console.log(`  statement close ${r.statementClosing.toDecimalString()} · book close ${r.bookClosing.toDecimalString()} · diff ${r.difference.toDecimalString()} · residual ${r.unreconciledResidual.toDecimalString()}`);
  console.log(`  matched ${r.matched.length}, unmatched book ${r.unmatchedBook.length}, unmatched statement ${r.unmatchedStatement.length}`);
  for (const n of r.notes) console.log(`  note: ${n}`);
}

// Scenario A: the last contractor payment is still in transit (not yet on the statement).
const clearedLines: StatementLine[] = [lineFrom(book[0]!, "s1"), lineFrom(book[1]!, "s2")];
const stmtA: Statement = {
  accountId: "chk",
  periodStart: "2026-08-01",
  periodEnd: "2026-08-31",
  openingBalance: usd("10000.00"),
  closingBalance: usd("13800.00"), // 10000 + 5000 - 1200 (the 800 hasn't cleared)
  lines: clearedLines,
};
const reconA = reconcileStatement(stmtA, book);
print("A) One payment in transit (timing) — should still be clean:", reconA);

if (isCleanForPublish(reconA)) {
  const signed = signReconciliation(reconA, {
    owner: "owner@acme",
    reviewer: "controller@rgnr8",
    completedAt: "2026-09-01T09:00:00Z",
  });
  console.log(`  signed off by ${signed.signOff?.reviewer} at ${signed.signOff?.completedAt}`);
}

// Scenario B: the bank charged a fee the feed never captured (missing source).
const stmtB: Statement = {
  ...stmtA,
  closingBalance: usd("13755.00"),
  lines: [...clearedLines, { id: "s-fee", date: "2026-08-20", amount: usd("-45.00"), description: "Wire fee" }],
};
print("B) Bank fee missing from the feed — should block publish:", reconcileStatement(stmtB, book));
