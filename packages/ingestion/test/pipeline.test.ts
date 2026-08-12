import { test } from "node:test";
import assert from "node:assert/strict";
import {
  BankPlaidLikeAdapter,
  IngestionPipeline,
  PayrollGustoLikeAdapter,
  TransactionKind,
  type CanonicalTransaction,
} from "../src/index.js";
import { gustoRawRecord, plaidRawRecords } from "./helpers.js";

function pipeline(): IngestionPipeline {
  return new IngestionPipeline()
    .register(new BankPlaidLikeAdapter("acme"))
    .register(new PayrollGustoLikeAdapter("acme"));
}

function byExternal(txns: readonly CanonicalTransaction[], externalId: string): CanonicalTransaction {
  const t = txns.find((x) => x.externalId === externalId);
  assert.ok(t, `expected a transaction with externalId ${externalId}`);
  return t;
}

test("bank sign convention is normalized (provider positive = money out)", () => {
  const p = pipeline();
  p.ingest(plaidRawRecords());
  const deposit = byExternal(p.all(), "p1");
  const purchase = byExternal(p.all(), "p2");
  assert.equal(deposit.amount.minorUnits, 500000n); // +5000 inflow
  assert.equal(deposit.direction, "INFLOW");
  assert.equal(purchase.amount.minorUnits, -12050n); // -120.50 outflow
  assert.equal(purchase.direction, "OUTFLOW");
});

test("transactions are classified by kind", () => {
  const p = pipeline();
  p.ingest([...plaidRawRecords(), gustoRawRecord()]);
  assert.equal(byExternal(p.all(), "p1").kind, TransactionKind.DEPOSIT);
  assert.equal(byExternal(p.all(), "p2").kind, TransactionKind.PURCHASE);
  assert.equal(byExternal(p.all(), "p3").kind, TransactionKind.FEE);
  assert.equal(byExternal(p.all(), "g1:net").kind, TransactionKind.PAYROLL_NET);
  assert.equal(byExternal(p.all(), "g1:tax").kind, TransactionKind.PAYROLL_TAX);
});

test("payroll run splits into net pay and taxes on their own dates", () => {
  const p = pipeline();
  p.ingest([gustoRawRecord()]);
  const net = byExternal(p.all(), "g1:net");
  const tax = byExternal(p.all(), "g1:tax");
  assert.equal(net.amount.minorUnits, -1600000n);
  assert.equal(net.date, "2026-08-07");
  assert.equal(tax.amount.minorUnits, -420000n); // 3200 + 1000
  assert.equal(tax.date, "2026-08-10");
});

test("pending is superseded by the posted record and excluded from active", () => {
  const p = pipeline();
  const res = p.ingest(plaidRawRecords());
  assert.equal(res.pendingSuperseded, 1);
  const pending = byExternal(p.all(), "p4");
  const posted = byExternal(p.all(), "p5");
  assert.equal(pending.supersededBy, posted.id);
  assert.equal(posted.supersedes, pending.id);
  assert.ok(!p.active().some((t) => t.externalId === "p4"));
  assert.ok(p.active().some((t) => t.externalId === "p5"));
});

test("internal transfers are detected and flagged (both sides)", () => {
  const p = pipeline();
  const res = p.ingest(plaidRawRecords());
  assert.equal(res.transfersDetected, 1);
  assert.equal(byExternal(p.all(), "p6").isInternalTransfer, true);
  assert.equal(byExternal(p.all(), "p7").isInternalTransfer, true);
  assert.equal(byExternal(p.all(), "p6").kind, TransactionKind.TRANSFER);
  // a normal purchase is not flagged
  assert.equal(byExternal(p.all(), "p2").isInternalTransfer, false);
});

test("raw payloads are archived append-only", () => {
  const p = pipeline();
  p.ingest([...plaidRawRecords(), gustoRawRecord()]);
  assert.equal(p.archive.count(), 8); // 7 bank + 1 payroll
});

test("re-ingesting the same feed is idempotent", () => {
  const p = pipeline();
  const raws = [...plaidRawRecords(), gustoRawRecord()];
  const first = p.ingest(raws);
  const activeAfterFirst = p.active().length;
  const second = p.ingest(raws);
  assert.equal(second.added.length, 0);
  assert.ok(second.duplicates >= first.added.length);
  assert.equal(second.archived, 0);
  assert.equal(p.active().length, activeAfterFirst);
});

test("unknown provider is reported as an issue, not a crash", () => {
  const p = pipeline();
  const res = p.ingest([
    { provider: "mystery", accountId: "x", externalId: "e1", payload: {}, fetchedAt: "2026-08-11T00:00:00Z", sourceVersion: "1" },
  ]);
  assert.equal(res.issues.length, 1);
  assert.equal(res.issues[0]?.kind, "no_adapter");
});
