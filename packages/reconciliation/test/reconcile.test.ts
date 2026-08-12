import { test } from "node:test";
import assert from "node:assert/strict";
import { Money, USD } from "@rgnr8/ledger-kernel";
import {
  DifferenceCategory,
  isCleanForPublish,
  reconcileStatement,
  signReconciliation,
  type BookItem,
  type Statement,
  type StatementLine,
} from "../src/index.js";

const usd = (s: string) => Money.fromDecimal(s, USD);

function line(id: string, date: string, amount: string, externalId?: string): StatementLine {
  return { id, date, amount: usd(amount), description: id, ...(externalId ? { externalId } : {}) };
}
function book(id: string, date: string, amount: string, externalId?: string): BookItem {
  return { id, date, amount: usd(amount), description: id, ...(externalId ? { externalId } : {}) };
}
function statement(lines: StatementLine[], opening: string, closing: string): Statement {
  return {
    accountId: "chk",
    periodStart: "2026-08-01",
    periodEnd: "2026-08-31",
    openingBalance: usd(opening),
    closingBalance: usd(closing),
    lines,
  };
}

test("fully matched statement is BALANCED with zero difference", () => {
  const stmt = statement(
    [line("s1", "2026-08-05", "500.00", "x1"), line("s2", "2026-08-06", "-200.00", "x2")],
    "1000.00",
    "1300.00",
  );
  const recon = reconcileStatement(stmt, [
    book("b1", "2026-08-05", "500.00", "x1"),
    book("b2", "2026-08-06", "-200.00", "x2"),
  ]);
  assert.equal(recon.status, "BALANCED");
  assert.equal(recon.difference.toDecimalString(), "0.00");
  assert.equal(recon.matched.length, 2);
  assert.equal(recon.matched[0]?.method, "external_id");
  assert.ok(isCleanForPublish(recon));
});

test("an in-transit book item (outstanding payment) still reconciles as BALANCED (timing)", () => {
  const stmt = statement([line("s1", "2026-08-05", "500.00", "x1")], "1000.00", "1500.00");
  const recon = reconcileStatement(stmt, [
    book("b1", "2026-08-05", "500.00", "x1"),
    book("b2", "2026-08-30", "-100.00"), // outstanding, not yet on statement
  ]);
  assert.equal(recon.status, "BALANCED");
  assert.equal(recon.unmatchedBook.length, 1);
  assert.equal(recon.unmatchedBook[0]?.category, DifferenceCategory.TIMING);
  assert.equal(recon.unreconciledResidual.toDecimalString(), "0.00");
  assert.ok(isCleanForPublish(recon));
});

test("a statement line missing from the books is OUT_OF_BALANCE (missing source)", () => {
  const stmt = statement(
    [line("s1", "2026-08-05", "500.00", "x1"), line("s2", "2026-08-07", "300.00")],
    "1000.00",
    "1800.00",
  );
  const recon = reconcileStatement(stmt, [book("b1", "2026-08-05", "500.00", "x1")]);
  assert.equal(recon.status, "OUT_OF_BALANCE");
  assert.equal(recon.unmatchedStatement.length, 1);
  assert.equal(recon.unmatchedStatement[0]?.category, DifferenceCategory.MISSING_SOURCE);
  assert.equal(recon.unreconciledResidual.toDecimalString(), "-300.00");
  assert.ok(!isCleanForPublish(recon));
});

test("matches by amount and date when there is no external id", () => {
  const stmt = statement([line("s1", "2026-08-05", "250.00")], "0.00", "250.00");
  const recon = reconcileStatement(stmt, [book("b1", "2026-08-07", "250.00")]); // 2 days apart
  assert.equal(recon.status, "BALANCED");
  assert.equal(recon.matched[0]?.method, "amount_date");
});

test("does not match across a too-wide date gap", () => {
  const stmt = statement([line("s1", "2026-08-05", "250.00")], "0.00", "250.00");
  const recon = reconcileStatement(stmt, [book("b1", "2026-08-20", "250.00")], { windowDays: 4 });
  assert.equal(recon.status, "OUT_OF_BALANCE");
  assert.equal(recon.unmatchedStatement.length, 1);
  assert.equal(recon.unmatchedBook.length, 1);
});

test("an internally inconsistent statement is flagged and out of balance", () => {
  const stmt = statement([line("s1", "2026-08-05", "500.00", "x1")], "1000.00", "9999.00");
  const recon = reconcileStatement(stmt, [book("b1", "2026-08-05", "500.00", "x1")]);
  assert.equal(recon.statementConsistent, false);
  assert.equal(recon.status, "OUT_OF_BALANCE");
  assert.ok(recon.notes.some((n) => n.includes("inconsistent")));
});

test("sign-off requires a balanced reconciliation", () => {
  const clean = reconcileStatement(
    statement([line("s1", "2026-08-05", "500.00", "x1")], "0.00", "500.00"),
    [book("b1", "2026-08-05", "500.00", "x1")],
  );
  const signed = signReconciliation(clean, {
    owner: "owner@acme",
    reviewer: "controller@rgnr8",
    completedAt: "2026-09-01T00:00:00Z",
  });
  assert.equal(signed.signOff?.reviewer, "controller@rgnr8");
  assert.equal(signed.status, "BALANCED");

  const dirty = reconcileStatement(
    statement([line("s1", "2026-08-05", "500.00")], "0.00", "500.00"),
    [], // nothing booked
  );
  assert.throws(
    () => signReconciliation(dirty, { owner: "o", reviewer: "r", completedAt: "2026-09-01T00:00:00Z" }),
    /out-of-balance/,
  );
});
