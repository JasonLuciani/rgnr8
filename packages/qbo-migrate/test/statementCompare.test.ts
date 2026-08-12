import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountType,
  ChartOfAccounts,
  InMemoryLedgerStore,
  Money,
  PeriodRegistry,
  PostingEngine,
  USD,
  asAccountId,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  type Account,
  type PostCommand,
  type Provenance,
} from "@rgnr8/ledger-kernel";
import { computeBalanceSheet, computeIncomeStatement } from "@rgnr8/statements";
import {
  compareBalanceSheet,
  compareIncomeStatement,
  compareStatements,
  renderStatementsDiffHtml,
} from "../src/index.js";

const TENANT = asTenantId("acme");
const AUG = asPeriodKey("2026-08");

function acct(id: string, code: string, name: string, type: AccountType): Account {
  return { id: asAccountId(id), code, name, type, currency: USD };
}
const COA = new ChartOfAccounts([
  acct("cash", "1000", "Cash", AccountType.ASSET),
  acct("ar", "1200", "AR", AccountType.ASSET),
  acct("equity", "3000", "Equity", AccountType.EQUITY),
  acct("rev", "4000", "Sales", AccountType.REVENUE),
  acct("rent", "6000", "Rent", AccountType.EXPENSE),
]);
const PROV: Provenance = {
  sourceSystem: "t", sourceObject: "je", sourceVersion: "1",
  effectiveDate: "2026-08-01", postedDate: "2026-08-01", ingestedAt: "2026-08-01T00:00:00Z",
  normalizationVersion: "n/1", mappingVersion: "m/1",
};
function cmd(key: string, date: string, lines: PostCommand["lines"]): PostCommand {
  return { tenantId: TENANT, idempotencyKey: asIdempotencyKey(key), periodKey: AUG, currency: USD, entryDate: date, lines, provenance: PROV };
}

async function books() {
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(COA, store, new PeriodRegistry());
  const d = asAccountId;
  await engine.post(cmd("open", "2026-08-01", [
    { accountId: d("cash"), side: "DEBIT", amount: Money.fromDecimal("50000.00", USD) },
    { accountId: d("equity"), side: "CREDIT", amount: Money.fromDecimal("50000.00", USD) },
  ]), { postedAt: "2026-08-01T00:00:00Z" });
  await engine.post(cmd("sale", "2026-08-05", [
    { accountId: d("ar"), side: "DEBIT", amount: Money.fromDecimal("12000.00", USD) },
    { accountId: d("rev"), side: "CREDIT", amount: Money.fromDecimal("12000.00", USD) },
  ]), { postedAt: "2026-08-05T00:00:00Z" });
  await engine.post(cmd("rent", "2026-08-10", [
    { accountId: d("rent"), side: "DEBIT", amount: Money.fromDecimal("4000.00", USD) },
    { accountId: d("cash"), side: "CREDIT", amount: Money.fromDecimal("4000.00", USD) },
  ]), { postedAt: "2026-08-10T00:00:00Z" });
  const is = await computeIncomeStatement(store, TENANT, COA, "2026-08-01", "2026-08-31", USD);
  const bs = await computeBalanceSheet(store, TENANT, COA, "2026-08-31", USD);
  return { is, bs };
}

test("income statement ties to QBO's reported totals", async () => {
  const { is } = await books();
  const diff = compareIncomeStatement(is, { revenue: "12000.00", expenses: "4000.00", netIncome: "8000.00" }, { currency: USD });
  assert.equal(diff.inAgreement, true);
  assert.equal(diff.totalAbsDelta.isZero(), true);
  assert.equal(diff.lines.length, 3);
});

test("balance sheet ties to QBO's reported totals", async () => {
  const { bs } = await books();
  // assets = cash 46,000 + AR 12,000 = 58,000; L+E = equity 50,000 + net income 8,000 = 58,000
  const diff = compareBalanceSheet(bs, { totalAssets: "58000.00", totalLiabilitiesAndEquity: "58000.00" }, { currency: USD });
  assert.equal(diff.inAgreement, true);
});

test("a divergent net income is flagged as a mismatch", async () => {
  const { is } = await books();
  const diff = compareIncomeStatement(is, { revenue: "12000.00", expenses: "4000.00", netIncome: "7500.00" }, { currency: USD });
  assert.equal(diff.inAgreement, false);
  const ni = diff.lines.find((l) => l.label === "Net income");
  assert.equal(ni?.status, "mismatch");
  assert.equal(ni?.delta.toDecimalString(), "500.00"); // rgnr8 8000 − qbo 7500
});

test("compareStatements + HTML render summarize both statements", async () => {
  const { is, bs } = await books();
  const diff = compareStatements(is, bs, {
    income: { revenue: "12000.00", expenses: "4000.00", netIncome: "8000.00" },
    balance: { totalAssets: "58000.00", totalLiabilitiesAndEquity: "58000.00" },
  }, { currency: USD });
  assert.equal(diff.inAgreement, true);
  const html = renderStatementsDiffHtml(diff);
  assert.match(html, /tie to QuickBooks/);
  assert.match(html, /Income statement/);
  assert.match(html, /Balance sheet/);
});
