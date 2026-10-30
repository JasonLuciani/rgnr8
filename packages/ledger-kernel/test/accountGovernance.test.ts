import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountSubtype,
  AccountType,
  BusinessCategory,
  ChartOfAccounts,
  InactiveAccountError,
  InMemoryLedgerStore,
  PeriodRegistry,
  USD,
  asAccountId,
  asIdempotencyKey,
  asPeriodKey,
  executeGoLive,
  isSystemAccount,
  parentsFirst,
  type Account,
  type GoLiveRequest,
} from "../src/index.js";
import { ACCT, POST_AT, PROV, TENANT, fixture, saleCommand, usd } from "./helpers.js";

test("an inactive account refuses a posting and a reversal that would touch it", async () => {
  const { engine, store, coa } = fixture();
  const posted = await engine.post(saleCommand("100.00"), { postedAt: POST_AT });
  await engine.reverse(TENANT, posted.id, {
    idempotencyKey: asIdempotencyKey("rev-1"), periodKey: asPeriodKey("2026-08"),
    entryDate: "2026-08-15", postedAt: POST_AT, provenance: PROV,
  });
  coa.deactivate(ACCT.cash);

  await assert.rejects(engine.post(saleCommand("50.00", "again"), { postedAt: POST_AT }), InactiveAccountError);
  await assert.rejects(
    engine.reverse(TENANT, posted.id, {
      idempotencyKey: asIdempotencyKey("rev-2"), periodKey: asPeriodKey("2026-08"),
      entryDate: "2026-08-15", postedAt: POST_AT, provenance: PROV,
    }),
    InactiveAccountError,
  );
  assert.equal((await store.list(TENANT)).length, 2, "nothing new was appended");
});

test("a reactivated account takes postings again", async () => {
  const { engine, coa } = fixture();
  coa.deactivate(ACCT.cash);
  await assert.rejects(engine.post(saleCommand("10.00"), { postedAt: POST_AT }), InactiveAccountError);
  coa.activate(ACCT.cash);
  assert.equal((await engine.post(saleCommand("10.00"), { postedAt: POST_AT })).sequence, 1);
});

test("system accounts are recognised by detail type, and by the opening-balance equity", () => {
  assert.equal(isSystemAccount({ subtype: AccountSubtype.ACCOUNTS_RECEIVABLE, name: "Trade debtors" }), true);
  assert.equal(isSystemAccount({ subtype: AccountSubtype.ACCOUNTS_PAYABLE, name: "AP" }), true);
  assert.equal(isSystemAccount({ subtype: AccountSubtype.RETAINED_EARNINGS, name: "RE" }), true);
  assert.equal(isSystemAccount({ subtype: AccountSubtype.BANK, name: "Checking" }), false);
  assert.equal(isSystemAccount({ subtype: AccountSubtype.EQUITY, name: "Owner's Equity" }), false);
  assert.equal(isSystemAccount({ subtype: AccountSubtype.EQUITY, name: "Opening Balance Equity" }), true);
  assert.equal(isSystemAccount({ name: "Misc" }), false);
});

test("parentsFirst puts every parent before its children, whatever order it is given in", () => {
  const exp = (over: Partial<Account> & Pick<Account, "id" | "code" | "name">): Account => ({
    type: AccountType.EXPENSE, subtype: AccountSubtype.EXPENSE, currency: USD, ...over,
  });
  const parent = exp({ id: asAccountId("p"), code: "6000", name: "Ads" });
  const child = exp({ id: asAccountId("c"), code: "6001", name: "Search", parentId: parent.id });
  const grand = exp({ id: asAccountId("g"), code: "6002", name: "Brand", parentId: child.id });
  assert.deepEqual(parentsFirst([grand, child, parent]).map((a) => a.code), ["6000", "6001", "6002"]);
  assert.equal(new ChartOfAccounts(parentsFirst([grand, child, parent])).list().length, 3);
  // The constructor alone would refuse this order.
  assert.throws(() => new ChartOfAccounts([grand, child, parent]));
});

test("a go-live from QuickBooks marks every account it brings over, template accounts included", async () => {
  const result = await executeGoLive(new InMemoryLedgerStore(), new PeriodRegistry(), {
    tenantId: "acme",
    sourceSystem: "quickbooks",
    cutoverDate: "2026-09-30",
    currency: USD,
    coaCategory: BusinessCategory.PROFESSIONAL_SERVICES,
    openingBalanceEquityCode: "3010",
    sourceAccounts: [
      { code: "1000", name: "Checking", balance: usd("25000.00"), subtype: AccountSubtype.BANK },
      { code: "9100", name: "Legacy Interest", balance: usd("-25000.00"), subtype: AccountSubtype.OTHER_EXPENSE },
    ],
    provenance: { ...PROV, sourceSystem: "quickbooks" },
  } satisfies GoLiveRequest, POST_AT);
  const byCode = new Map(result.chart.list().map((a) => [a.code, a]));
  assert.equal(byCode.get("1000")?.origin, "quickbooks", "a template account the source also has is marked");
  assert.equal(byCode.get("9100")?.origin, "quickbooks");
  assert.equal(byCode.get("1200")?.origin, undefined, "a template-only account is RGNR8's");
});
