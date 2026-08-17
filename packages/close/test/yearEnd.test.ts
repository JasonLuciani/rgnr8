import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountType,
  ChartOfAccounts,
  InMemoryLedgerStore,
  Money,
  PostingEngine,
  USD,
  accountBalances,
  asAccountId,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  computeTrialBalance,
  type Account,
  type JournalLineInput,
  type PostCommand,
  type Provenance,
} from "@rgnr8/ledger-kernel";
import { buildYearEndClose, fiscalYearBounds } from "../src/index.js";

const tenant = asTenantId("acme");
const prov: Provenance = {
  sourceSystem: "test", sourceObject: "fixture", sourceVersion: "1",
  effectiveDate: "2026-12-31", postedDate: "2026-12-31", ingestedAt: "2026-12-31",
  normalizationVersion: "1", mappingVersion: "1",
};
function acct(id: string, code: string, type: AccountType): Account {
  return { id: asAccountId(id), code, name: code, type, currency: USD };
}
const coa = new ChartOfAccounts([
  acct("cash", "1000", AccountType.ASSET),
  acct("capital", "3000", AccountType.EQUITY),
  acct("re", "3900", AccountType.EQUITY),
  acct("rev", "4000", AccountType.REVENUE),
  acct("cogs", "5000", AccountType.EXPENSE),
  acct("opex", "6000", AccountType.EXPENSE),
]);
const m = (minor: bigint) => Money.fromMinorUnits(minor, USD);
const dr = (id: string, minor: bigint): JournalLineInput => ({ accountId: asAccountId(id), side: "DEBIT", amount: m(minor) });
const cr = (id: string, minor: bigint): JournalLineInput => ({ accountId: asAccountId(id), side: "CREDIT", amount: m(minor) });

async function seed(): Promise<{ store: InMemoryLedgerStore; engine: PostingEngine }> {
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(coa, store);
  const post = (key: string, date: string, lines: JournalLineInput[]) => {
    const cmd: PostCommand = {
      tenantId: tenant, idempotencyKey: asIdempotencyKey(key), periodKey: asPeriodKey(date.slice(0, 7)),
      currency: USD, entryDate: date, lines, provenance: prov,
    };
    return engine.post(cmd, { postedAt: `${date}T00:00:00Z` });
  };
  await post("o1", "2026-01-01", [dr("cash", 100000n), cr("capital", 100000n)]);
  await post("s1", "2026-03-15", [dr("cash", 90000n), cr("rev", 90000n)]); // revenue 900
  await post("c1", "2026-04-10", [dr("cogs", 30000n), cr("cash", 30000n)]); // cogs 300
  await post("e1", "2026-06-20", [dr("opex", 20000n), cr("cash", 20000n)]); // opex 200
  return { store, engine };
}

test("fiscalYearBounds handles calendar and non-calendar fiscal years", () => {
  assert.deepEqual(fiscalYearBounds(2026), { from: "2026-01-01", to: "2026-12-31" });
  assert.deepEqual(fiscalYearBounds(2026, { startMonth: 7 }), { from: "2025-07-01", to: "2026-06-30" });
});

test("year-end close zeroes P&L accounts and rolls net income to retained earnings", async () => {
  const { store, engine } = await seed();
  const fy = fiscalYearBounds(2026);
  const tb = await computeTrialBalance(store, tenant, coa, USD, fy);

  const close = buildYearEndClose(tb, {
    tenantId: "acme", currency: USD, retainedEarningsId: asAccountId("re"),
    entryDate: "2026-12-31", fiscalYear: 2026, provenance: prov,
  });
  // net income = 900 - (300 + 200) = 400
  assert.equal(close.netIncome.toDecimalString(), "400.00");
  assert.equal(close.closedAccounts.length, 3);

  await engine.post(close.command, { postedAt: "2026-12-31T00:00:00Z" });

  const bal = await accountBalances(store, tenant, coa, USD);
  // P&L accounts are zeroed after the close.
  assert.equal(bal.get(asAccountId("rev"))?.toDecimalString(), "0.00");
  assert.equal(bal.get(asAccountId("cogs"))?.toDecimalString(), "0.00");
  assert.equal(bal.get(asAccountId("opex"))?.toDecimalString(), "0.00");
  // Retained earnings now carries the year's net income.
  assert.equal(bal.get(asAccountId("re"))?.toDecimalString(), "400.00");

  // Ledger still balances.
  const after = await computeTrialBalance(store, tenant, coa, USD);
  assert.ok(after.inBalance);
});

test("re-running the year-end close is idempotent", async () => {
  const { store, engine } = await seed();
  const tb = await computeTrialBalance(store, tenant, coa, USD, fiscalYearBounds(2026));
  const close = buildYearEndClose(tb, {
    tenantId: "acme", currency: USD, retainedEarningsId: asAccountId("re"),
    entryDate: "2026-12-31", fiscalYear: 2026, provenance: prov,
  });
  await engine.post(close.command, { postedAt: "2026-12-31T00:00:00Z" });
  await engine.post(close.command, { postedAt: "2026-12-31T00:00:00Z" }); // same idempotency key
  const re = (await accountBalances(store, tenant, coa, USD)).get(asAccountId("re"));
  assert.equal(re?.toDecimalString(), "400.00"); // not doubled
});
