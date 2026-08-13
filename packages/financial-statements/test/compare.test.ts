import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountType,
  ChartOfAccounts,
  Money,
  USD,
  asAccountId,
  type Account,
  type AccountId,
} from "@rgnr8/ledger-kernel";
import {
  balanceSheet,
  cashFlow,
  classify,
  classifyByCode,
  classifyByType,
  fromAccountBalances,
  incomeStatement,
  makeTrialBalance,
  periodCompare,
  renderStatements,
  type TrialBalanceEntry,
} from "../src/index.js";

const m = (minor: bigint) => Money.fromMinorUnits(minor, USD);

function entry(
  id: string,
  code: string,
  name: string,
  cls: TrialBalanceEntry["accountClass"],
  signedMinor: bigint,
): TrialBalanceEntry {
  return { accountId: asAccountId(id), code, name, accountClass: cls, signed: m(signedMinor) };
}

test("classify maps every account type to its class", () => {
  const a = (id: string, code: string, type: AccountType): Account => ({
    id: asAccountId(id),
    code,
    name: id,
    type,
    currency: USD,
  });
  assert.equal(classify(a("x", "1000", AccountType.ASSET)), "asset");
  assert.equal(classify(a("x", "2000", AccountType.LIABILITY)), "liability");
  assert.equal(classify(a("x", "3000", AccountType.EQUITY)), "equity");
  assert.equal(classify(a("x", "4000", AccountType.REVENUE)), "revenue");
  assert.equal(classify(a("x", "5000", AccountType.EXPENSE)), "expense");

  assert.equal(classifyByType(AccountType.ASSET), "asset");

  // Code-prefix fallback.
  assert.equal(classifyByCode("1200"), "asset");
  assert.equal(classifyByCode("2100"), "liability");
  assert.equal(classifyByCode("3050"), "equity");
  assert.equal(classifyByCode("4900"), "revenue");
  assert.equal(classifyByCode("5300"), "expense");
  assert.equal(classifyByCode("6100"), "expense");
});

test("fromAccountBalances adapts normal-oriented kernel balances into signed form", () => {
  const coa = new ChartOfAccounts([
    { id: asAccountId("cash"), code: "1000", name: "Cash", type: AccountType.ASSET, currency: USD },
    { id: asAccountId("rev"), code: "4000", name: "Revenue", type: AccountType.REVENUE, currency: USD },
  ]);
  // accountBalances returns normal-oriented amounts (positive = normal side).
  const oriented = new Map<AccountId, Money>([
    [asAccountId("cash"), m(50000n)], // debit-normal, oriented positive -> signed +50000
    [asAccountId("rev"), m(30000n)], // credit-normal, oriented positive -> signed -30000
  ]);
  const tb = fromAccountBalances(oriented, coa, USD);
  const cash = tb.entries.find((e) => e.code === "1000")!;
  const rev = tb.entries.find((e) => e.code === "4000")!;
  assert.equal(cash.signed.minorUnits, 50000n);
  assert.equal(rev.signed.minorUnits, -30000n);
  const is = incomeStatement(tb);
  assert.equal(is.revenue.toDecimalString(), "300.00");
});

test("period comparison computes line-level variances and percentages", () => {
  const prior = makeTrialBalance(USD, [
    entry("rev", "4000", "Sales", "revenue", -20000n), // $200 revenue
    entry("exp", "5000", "Expense", "expense", 10000n), // $100 expense
  ]);
  const current = makeTrialBalance(USD, [
    entry("rev", "4000", "Sales", "revenue", -30000n), // $300 revenue
    entry("exp", "5000", "Expense", "expense", 12000n), // $120 expense
    entry("mkt", "5100", "Marketing", "expense", 4000n), // $40, new this period
  ]);

  const pc = periodCompare(current, prior);

  const rev = pc.lines.find((l) => l.code === "4000")!;
  assert.equal(rev.current.toDecimalString(), "300.00");
  assert.equal(rev.prior.toDecimalString(), "200.00");
  assert.equal(rev.variance.toDecimalString(), "100.00");
  assert.equal(rev.variancePct, 50);

  const exp = pc.lines.find((l) => l.code === "5000")!;
  assert.equal(exp.variance.toDecimalString(), "20.00");
  assert.equal(exp.variancePct, 20);

  // New account: prior is zero, percentage is undefined (null).
  const mkt = pc.lines.find((l) => l.code === "5100")!;
  assert.equal(mkt.prior.toDecimalString(), "0.00");
  assert.equal(mkt.current.toDecimalString(), "40.00");
  assert.equal(mkt.variance.toDecimalString(), "40.00");
  assert.equal(mkt.variancePct, null);

  // Totals.
  assert.equal(pc.revenue.variance.toDecimalString(), "100.00");
  assert.equal(pc.expenses.current.toDecimalString(), "160.00"); // 120 + 40
  assert.equal(pc.expenses.prior.toDecimalString(), "100.00");
  assert.equal(pc.expenses.variance.toDecimalString(), "60.00");
  // NI: (300-160)=140 vs (200-100)=100 -> +40 (+40%).
  assert.equal(pc.netIncome.current.toDecimalString(), "140.00");
  assert.equal(pc.netIncome.variance.toDecimalString(), "40.00");
  assert.equal(pc.netIncome.variancePct, 40);
});

test("renderStatements produces a self-contained HTML document", () => {
  const tbStart = makeTrialBalance(USD, [
    entry("cash", "1000", "Cash", "asset", 50000n),
    entry("cap", "3000", "Owner Capital", "equity", -50000n),
  ]);
  const tbEnd = makeTrialBalance(USD, [
    entry("cash", "1000", "Cash", "asset", 60000n),
    entry("ar", "1100", "AR", "asset", 10000n),
    entry("cap", "3000", "Owner Capital", "equity", -50000n),
    entry("rev", "4000", "Sales", "revenue", -30000n),
    entry("exp", "5000", "Expense", "expense", 10000n),
  ]);
  const is = incomeStatement(tbEnd);
  const bs = balanceSheet(tbEnd, is.netIncome);
  const cf = cashFlow(tbStart, tbEnd, is.netIncome);
  const html = renderStatements({
    title: "August 2026",
    context: "Acme — August 2026",
    incomeStatement: is,
    balanceSheet: bs,
    cashFlow: cf,
    periodComparison: periodCompare(tbEnd, tbStart),
  });
  assert.match(html, /^<!doctype html>/);
  assert.ok(html.includes("<table"));
  assert.ok(html.includes("Income Statement"));
  assert.ok(html.includes("Balance Sheet"));
  assert.ok(html.includes("Cash Flow"));
  assert.ok(html.includes("Net income"));
  // Balanced fixture renders the balanced banner.
  assert.ok(html.includes("Balanced"));
});
