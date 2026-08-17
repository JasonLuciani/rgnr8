import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountSubtype,
  BusinessCategory,
  GO_LIVE_CONTRACT,
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
  computeTrialBalance,
  executeGoLive,
  goLiveFromDto,
  goLiveToDto,
  type GoLiveRequest,
  type Provenance,
  type SourceAccount,
} from "../src/index.js";

const tenant = asTenantId("northwind");
const prov: Provenance = {
  sourceSystem: "quickbooks", sourceObject: "trial_balance", sourceVersion: "1",
  effectiveDate: "2026-08-31", postedDate: "2026-08-31", ingestedAt: "2026-08-31",
  normalizationVersion: "1", mappingVersion: "1",
};
const m = (s: string) => Money.fromDecimal(s, USD);

// A QBO trial balance as of cutover (signed debit-positive). Some accounts match
// the contractor template's codes (cash 1000, AR 1200); others are source-only.
const SOURCE: SourceAccount[] = [
  { code: "1000", name: "Business Checking", balance: m("25000.00"), subtype: AccountSubtype.BANK },
  { code: "1200", name: "Accounts Receivable", balance: m("8000.00"), subtype: AccountSubtype.ACCOUNTS_RECEIVABLE },
  { code: "2000", name: "Accounts Payable", balance: m("-3000.00"), subtype: AccountSubtype.ACCOUNTS_PAYABLE },
  { code: "3900", name: "Retained Earnings", balance: m("-30000.00"), subtype: AccountSubtype.RETAINED_EARNINGS },
];

function request(over: Partial<GoLiveRequest> = {}): GoLiveRequest {
  return {
    tenantId: "northwind", sourceSystem: "quickbooks", cutoverDate: "2026-08-31", currency: USD,
    coaCategory: BusinessCategory.CONTRACTOR_TRADES, sourceAccounts: SOURCE,
    openingBalanceEquityCode: "3010", provenance: prov, ...over,
  };
}

test("go-live seeds the template chart, brings over source accounts, and posts opening balances", async () => {
  const store = new InMemoryLedgerStore();
  const periods = new PeriodRegistry();
  const result = await executeGoLive(store, periods, request(), "2026-08-31T00:00:00Z");

  // template (contractor) accounts are present, plus the OBE we created
  assert.ok(result.chart.getByCode("5100")); // "Job Materials" from the contractor template
  assert.ok(result.chart.getByCode("3010")); // Opening Balance Equity, created for the residual
  assert.ok(result.createdAccounts.some((a) => a.code === "3010"));

  // opening balances posted and the ledger is in balance
  const tb = await computeTrialBalance(store, tenant, result.chart, USD);
  assert.ok(tb.inBalance);
  assert.equal(tb.rows.find((r) => r.code === "1000")!.debit.toDecimalString(), "25000.00");
  assert.equal(tb.rows.find((r) => r.code === "2000")!.credit.toDecimalString(), "3000.00");

  // RGNR8 is now system of record; the cutover period is locked
  assert.equal(result.cutover.record.sourceSystem, "quickbooks");
  assert.equal(result.cutover.record.lockedThroughPeriod, "2026-08");
});

test("go-live creates source-only accounts that aren't in the template", async () => {
  const store = new InMemoryLedgerStore();
  const periods = new PeriodRegistry();
  // a source account with a code the template doesn't have
  const src = [...SOURCE, { code: "1700", name: "Deposits Paid", balance: m("500.00"), subtype: AccountSubtype.OTHER_CURRENT_ASSET }];
  const result = await executeGoLive(store, periods, request({ sourceAccounts: src }), "2026-08-31T00:00:00Z");
  assert.ok(result.createdAccounts.some((a) => a.code === "1700"));
  assert.ok(result.chart.getByCode("1700"));
});

test("go-live can start from source accounts only (no template)", async () => {
  const store = new InMemoryLedgerStore();
  const periods = new PeriodRegistry();
  const noTemplate: GoLiveRequest = {
    tenantId: "northwind", sourceSystem: "quickbooks", cutoverDate: "2026-08-31", currency: USD,
    sourceAccounts: SOURCE, openingBalanceEquityCode: "3010", provenance: prov,
  };
  const result = await executeGoLive(store, periods, noTemplate, "2026-08-31T00:00:00Z");
  const tb = await computeTrialBalance(store, tenant, result.chart, USD);
  assert.ok(tb.inBalance);
  // no template accounts, only the 4 source + OBE
  assert.equal(result.chart.list().length, 5);
});

test("after go-live the frozen cutover period rejects backdated posts", async () => {
  const store = new InMemoryLedgerStore();
  const periods = new PeriodRegistry();
  const result = await executeGoLive(store, periods, request(), "2026-08-31T00:00:00Z");
  const engine = new PostingEngine(result.chart, store, periods);
  const backdated = {
    tenantId: tenant,
    idempotencyKey: asIdempotencyKey("late"),
    periodKey: asPeriodKey("2026-08"),
    currency: USD,
    entryDate: "2026-08-10",
    provenance: prov,
    lines: [
      { accountId: asAccountId("acct:1000"), side: "DEBIT" as const, amount: m("100.00") },
      { accountId: asAccountId("acct:3900"), side: "CREDIT" as const, amount: m("100.00") },
    ],
  };
  await assert.rejects(() => engine.post(backdated, { postedAt: "2026-09-01T00:00:00Z" }), PeriodClosedError);
});

test("go-live/1 contract round-trips through toDto/fromDto", () => {
  const req = request();
  const dto = goLiveToDto(req);
  assert.equal(dto.contract, GO_LIVE_CONTRACT);
  assert.equal(dto.coa_category, "CONTRACTOR_TRADES");
  assert.equal(dto.source_accounts[0]!.balance_minor, "2500000"); // 25000.00
  const back = goLiveFromDto(dto);
  assert.equal(back.tenantId, "northwind");
  assert.equal(back.sourceAccounts[0]!.balance.toDecimalString(), "25000.00");
  assert.equal(back.coaCategory, BusinessCategory.CONTRACTOR_TRADES);
  assert.equal(back.openingBalanceEquityCode, "3010");
});

test("go-live executed from a parsed contract matches the direct path", async () => {
  const dto = goLiveToDto(request());
  const parsed = goLiveFromDto(dto);
  const store = new InMemoryLedgerStore();
  const result = await executeGoLive(store, new PeriodRegistry(), parsed, "2026-08-31T00:00:00Z");
  const tb = await computeTrialBalance(store, tenant, result.chart, USD);
  assert.ok(tb.inBalance);
});
