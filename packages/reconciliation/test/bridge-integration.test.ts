import { test } from "node:test";
import assert from "node:assert/strict";
import { Money, USD } from "@rgnr8/ledger-kernel";
import { BankPlaidLikeAdapter, IngestionPipeline, type RawRecord } from "@rgnr8/ingestion";
import {
  bookItemsFromCanonical,
  isCleanForPublish,
  reconcileStatement,
  type StatementLine,
} from "../src/index.js";

const usd = (s: string) => Money.fromDecimal(s, USD);
const FETCHED = "2026-08-31T00:00:00Z";

// Two checking-account transactions from a Plaid-like feed (provider sign: + = out).
function raws(): RawRecord[] {
  const txns = [
    { transaction_id: "t1", account_id: "chk", date: "2026-08-05", pending: false, amount: "-5000.00", name: "Client ACH", category: ["Deposit"] },
    { transaction_id: "t2", account_id: "chk", date: "2026-08-08", pending: false, amount: "1200.00", name: "Vendor payment", category: ["Service"] },
  ];
  return txns.map((t) => ({
    provider: "plaid",
    accountId: t.account_id,
    externalId: t.transaction_id,
    payload: t,
    fetchedAt: FETCHED,
    sourceVersion: "1",
  }));
}

function ingestedBookItems() {
  const pipeline = new IngestionPipeline().register(new BankPlaidLikeAdapter("acme"));
  pipeline.ingest(raws());
  return bookItemsFromCanonical(pipeline.active(), "chk");
}

test("ingested feed reconciles clean against a matching statement", () => {
  const book = ingestedBookItems();
  // Bank statement shows the same two movements (+5000 in, -1200 out).
  const lines: StatementLine[] = book.map((b) => ({
    id: `stmt-${b.id}`,
    date: b.date,
    amount: b.amount,
    description: b.description,
  }));
  const recon = reconcileStatement(
    {
      accountId: "chk",
      periodStart: "2026-08-01",
      periodEnd: "2026-08-31",
      openingBalance: usd("10000.00"),
      closingBalance: usd("13800.00"), // 10000 + 5000 - 1200
      lines,
    },
    book,
  );
  assert.equal(recon.status, "BALANCED");
  assert.equal(recon.matched.length, 2);
  assert.equal(recon.difference.toDecimalString(), "0.00");
  assert.ok(isCleanForPublish(recon));
});

test("a bank line the feed missed blocks the publish gate", () => {
  const book = ingestedBookItems();
  const lines: StatementLine[] = book.map((b) => ({
    id: `stmt-${b.id}`,
    date: b.date,
    amount: b.amount,
    description: b.description,
  }));
  // Add a bank fee that the feed did not capture.
  lines.push({ id: "stmt-fee", date: "2026-08-20", amount: usd("-45.00"), description: "Wire fee" });

  const recon = reconcileStatement(
    {
      accountId: "chk",
      periodStart: "2026-08-01",
      periodEnd: "2026-08-31",
      openingBalance: usd("10000.00"),
      closingBalance: usd("13755.00"), // 13800 - 45
      lines,
    },
    book,
  );
  assert.equal(recon.status, "OUT_OF_BALANCE");
  assert.equal(recon.unmatchedStatement.length, 1);
  assert.ok(!isCleanForPublish(recon));
});
