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
} from "@rgnr8/ledger-kernel";
import type { Account, JournalLineInput, Provenance } from "@rgnr8/ledger-kernel";
import { computeBalanceSheet, computeIncomeStatement, renderBalanceSheet } from "../src/index.js";

const usd = (s: string) => Money.fromDecimal(s, USD);
const TENANT = asTenantId("acme");

const PROV: Provenance = {
  sourceSystem: "test", sourceObject: "je", sourceVersion: "1",
  effectiveDate: "2026-08-01", postedDate: "2026-08-01", ingestedAt: "2026-08-01",
  normalizationVersion: "1", mappingVersion: "1",
};

function acct(code: string, type: AccountType): Account {
  return { id: asAccountId(`gl.${code}`), code, name: code, type, currency: USD };
}
function coa(): ChartOfAccounts {
  return new ChartOfAccounts([
    acct("cash", AccountType.ASSET),
    acct("ar", AccountType.ASSET),
    acct("ap", AccountType.LIABILITY),
    acct("equity", AccountType.EQUITY),
    acct("revenue", AccountType.REVENUE),
    acct("rent", AccountType.EXPENSE),
  ]);
}

async function seed() {
  const chart = coa();
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart, store, new PeriodRegistry());
  let n = 0;
  const post = async (date: string, lines: JournalLineInput[]) => {
    await engine.post(
      {
        tenantId: TENANT,
        idempotencyKey: asIdempotencyKey(`je-${++n}`),
        periodKey: asPeriodKey(date.slice(0, 7)),
        currency: USD,
        entryDate: date,
        provenance: PROV,
        lines,
      },
      { postedAt: "2026-08-31T00:00:00Z" },
    );
  };

  // Owner funds the business
  await post("2026-08-01", [
    { accountId: asAccountId("gl.cash"), side: "DEBIT", amount: usd("50000.00") },
    { accountId: asAccountId("gl.equity"), side: "CREDIT", amount: usd("50000.00") },
  ]);
  // Invoice a client (revenue on account)
  await post("2026-08-05", [
    { accountId: asAccountId("gl.ar"), side: "DEBIT", amount: usd("20000.00") },
    { accountId: asAccountId("gl.revenue"), side: "CREDIT", amount: usd("20000.00") },
  ]);
  // Collect part of it
  await post("2026-08-20", [
    { accountId: asAccountId("gl.cash"), side: "DEBIT", amount: usd("12000.00") },
    { accountId: asAccountId("gl.ar"), side: "CREDIT", amount: usd("12000.00") },
  ]);
  // Rent bill received (accrued, unpaid)
  await post("2026-08-25", [
    { accountId: asAccountId("gl.rent"), side: "DEBIT", amount: usd("6000.00") },
    { accountId: asAccountId("gl.ap"), side: "CREDIT", amount: usd("6000.00") },
  ]);
  return { chart, store };
}

test("income statement nets revenue against expenses", async () => {
  const { chart, store } = await seed();
  const is = await computeIncomeStatement(store, TENANT, chart, "2026-08-01", "2026-08-31", USD);
  assert.equal(is.revenue.total.toDecimalString(), "20000.00");
  assert.equal(is.expenses.total.toDecimalString(), "6000.00");
  assert.equal(is.netIncome.toDecimalString(), "14000.00");
});

test("balance sheet balances by construction (assets = liabilities + equity + earnings)", async () => {
  const { chart, store } = await seed();
  const bs = await computeBalanceSheet(store, TENANT, chart, "2026-08-31", USD);
  // Assets: cash 62000 (50000 + 12000) + AR 8000 (20000 − 12000) = 70000
  assert.equal(bs.totalAssets.toDecimalString(), "70000.00");
  // Liab 6000 (AP) + Equity 50000 + net income 14000 = 70000
  assert.equal(bs.totalLiabilitiesAndEquity.toDecimalString(), "70000.00");
  assert.equal(bs.netIncome.toDecimalString(), "14000.00");
  assert.ok(bs.balances);
});

test("period filter excludes out-of-range activity from the P&L", async () => {
  const { chart, store } = await seed();
  // A window before any revenue was booked yields zero.
  const is = await computeIncomeStatement(store, TENANT, chart, "2026-07-01", "2026-08-04", USD);
  assert.equal(is.revenue.total.toDecimalString(), "0.00");
  assert.equal(is.netIncome.toDecimalString(), "0.00");
});

test("renders a readable balance sheet", async () => {
  const { chart, store } = await seed();
  const bs = await computeBalanceSheet(store, TENANT, chart, "2026-08-31", USD);
  const text = renderBalanceSheet(bs);
  assert.ok(text.includes("Balance Sheet"));
  assert.ok(text.includes("Balances: YES"));
});
