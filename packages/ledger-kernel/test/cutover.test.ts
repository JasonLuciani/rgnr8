import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountType,
  ChartOfAccounts,
  CutoverRegistry,
  InMemoryLedgerStore,
  Money,
  PeriodClosedError,
  PeriodRegistry,
  PostingEngine,
  USD,
  asAccountId,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  buildOpeningBalanceEntry,
  computeTrialBalance,
  executeCutover,
  type Account,
  type CutoverPlan,
  type PostCommand,
  type Provenance,
} from "../src/index.js";

const TENANT = "acme";
const tenant = asTenantId(TENANT);
const prov: Provenance = {
  sourceSystem: "quickbooks", sourceObject: "trial_balance", sourceVersion: "1",
  effectiveDate: "2026-08-31", postedDate: "2026-08-31", ingestedAt: "2026-08-31",
  normalizationVersion: "1", mappingVersion: "1",
};
function acct(id: string, code: string, type: AccountType): Account {
  return { id: asAccountId(id), code, name: code, type, currency: USD };
}
const coa = new ChartOfAccounts([
  acct("cash", "1000", AccountType.ASSET),
  acct("ar", "1200", AccountType.ASSET),
  acct("ap", "2000", AccountType.LIABILITY),
  acct("obe", "3000", AccountType.EQUITY),
  acct("re", "3900", AccountType.EQUITY),
  acct("sales", "4000", AccountType.REVENUE),
]);
const m = (s: string) => Money.fromDecimal(s, USD);

// Imported QBO trial balance as of the cutover date (signed debit-positive):
// cash 25,000 + AR 8,000 (assets, +) ; AP -3,000 ; retained earnings -30,000 (credits)
function plan(over: Partial<CutoverPlan> = {}): CutoverPlan {
  return {
    tenantId: TENANT, sourceSystem: "quickbooks", cutoverDate: "2026-08-31", currency: USD,
    openingBalanceEquityId: asAccountId("obe"), provenance: prov,
    balances: [
      { accountId: asAccountId("cash"), balance: m("25000.00") },
      { accountId: asAccountId("ar"), balance: m("8000.00") },
      { accountId: asAccountId("ap"), balance: m("-3000.00") },
      { accountId: asAccountId("re"), balance: m("-30000.00") },
    ],
    ...over,
  };
}

test("opening-balance entry balances via Opening Balance Equity", () => {
  const cmd = buildOpeningBalanceEntry(plan());
  // debit lines: cash 25000, ar 8000 = 33000; credit lines: ap 3000, re 30000 = 33000
  // net signed = 25000+8000-3000-30000 = 0 -> no OBE line needed here (already balanced)
  const debit = cmd.lines.filter((l) => l.side === "DEBIT").reduce((a, l) => a + l.amount.minorUnits, 0n);
  const credit = cmd.lines.filter((l) => l.side === "CREDIT").reduce((a, l) => a + l.amount.minorUnits, 0n);
  assert.equal(debit, credit);
});

test("OBE absorbs the residual when balances don't net to zero", () => {
  // Drop retained earnings so the books don't self-balance; OBE must plug 30000.
  const cmd = buildOpeningBalanceEntry(plan({
    balances: [
      { accountId: asAccountId("cash"), balance: m("25000.00") },
      { accountId: asAccountId("ar"), balance: m("8000.00") },
      { accountId: asAccountId("ap"), balance: m("-3000.00") },
    ],
  }));
  const obe = cmd.lines.find((l) => String(l.accountId) === "obe")!;
  assert.ok(obe);
  // net signed = 25000+8000-3000 = +30000 -> OBE credit 30000
  assert.equal(obe.side, "CREDIT");
  assert.equal(obe.amount.toDecimalString(), "30000.00");
});

test("executeCutover posts opening balances, locks the period, and records SoR takeover", async () => {
  const store = new InMemoryLedgerStore();
  const periods = new PeriodRegistry();
  const engine = new PostingEngine(coa, store, periods);
  const registry = new CutoverRegistry();

  const result = await executeCutover(engine, periods, store, plan(), "2026-08-31T00:00:00Z");
  registry.record(result.record);

  assert.ok(result.newlyPosted);
  assert.equal(result.record.sourceSystem, "quickbooks");
  assert.equal(result.record.lockedThroughPeriod, "2026-08");
  assert.equal(result.record.openingTotal.toDecimalString(), "33000.00");

  // The ledger now carries the opening balances and is in balance.
  const tb = await computeTrialBalance(store, tenant, coa, USD);
  assert.ok(tb.inBalance);
  assert.equal(tb.rows.find((r) => r.code === "1000")!.debit.toDecimalString(), "25000.00");

  // RGNR8 is now the system of record from the cutover date forward.
  assert.ok(registry.isLive(TENANT));
  assert.ok(registry.isAuthoritativeOn(TENANT, "2026-09-01"));
  assert.equal(registry.isAuthoritativeOn(TENANT, "2026-08-30"), false);

  // The cutover period is locked — you can't post back into the frozen history.
  const backdated: PostCommand = {
    tenantId: tenant, idempotencyKey: asIdempotencyKey("late"), periodKey: asPeriodKey("2026-08"),
    currency: USD, entryDate: "2026-08-15", provenance: prov,
    lines: [
      { accountId: asAccountId("cash"), side: "DEBIT", amount: m("100.00") },
      { accountId: asAccountId("sales"), side: "CREDIT", amount: m("100.00") },
    ],
  };
  await assert.rejects(() => engine.post(backdated, { postedAt: "2026-09-01T00:00:00Z" }), PeriodClosedError);
});

test("re-running the same cutover is idempotent (no double opening entry)", async () => {
  const store = new InMemoryLedgerStore();
  const periods = new PeriodRegistry();
  const engine = new PostingEngine(coa, store, periods);
  const first = await executeCutover(engine, periods, store, plan(), "2026-08-31T00:00:00Z");
  const second = await executeCutover(engine, periods, store, plan(), "2026-08-31T00:00:00Z");
  assert.ok(first.newlyPosted);
  assert.equal(second.newlyPosted, false);
  assert.equal(first.entry.id, second.entry.id);
  assert.equal((await store.list(tenant)).length, 1); // exactly one opening entry
});
