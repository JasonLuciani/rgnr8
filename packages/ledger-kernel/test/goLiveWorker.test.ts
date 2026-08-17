import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountSubtype,
  BusinessCategory,
  GoLiveWorker,
  InMemoryGoLiveQueue,
  InMemoryLedgerStore,
  Money,
  PeriodRegistry,
  USD,
  asTenantId,
  goLiveToDto,
  sourceAccountsFromTrialBalance,
  type GoLiveDto,
  type GoLiveRequest,
  type LedgerFor,
  type Provenance,
} from "../src/index.js";

const prov: Provenance = {
  sourceSystem: "quickbooks", sourceObject: "trial_balance", sourceVersion: "1",
  effectiveDate: "2026-08-31", postedDate: "2026-08-31", ingestedAt: "2026-08-31",
  normalizationVersion: "1", mappingVersion: "1",
};
const m = (s: string) => Money.fromDecimal(s, USD);

function requestFor(tenantId: string): GoLiveRequest {
  return {
    tenantId, sourceSystem: "quickbooks", cutoverDate: "2026-08-31", currency: USD,
    coaCategory: BusinessCategory.SERVICE_GENERAL, openingBalanceEquityCode: "3010", provenance: prov,
    sourceAccounts: [
      { code: "1000", name: "Checking", balance: m("25000.00"), subtype: AccountSubtype.BANK },
      { code: "3900", name: "Retained Earnings", balance: m("-25000.00"), subtype: AccountSubtype.RETAINED_EARNINGS },
    ],
  };
}

// One ledger per tenant, created lazily and reused (so re-drains hit the same store).
function ledgers(): { ledgerFor: LedgerFor; storeOf: (t: string) => InMemoryLedgerStore } {
  const stores = new Map<string, InMemoryLedgerStore>();
  const periods = new Map<string, PeriodRegistry>();
  const ledgerFor: LedgerFor = (tenantId) => {
    if (!stores.has(tenantId)) {
      stores.set(tenantId, new InMemoryLedgerStore());
      periods.set(tenantId, new PeriodRegistry());
    }
    return { store: stores.get(tenantId)!, periods: periods.get(tenantId)! };
  };
  return { ledgerFor, storeOf: (t) => stores.get(t)! };
}

test("worker drains queued go-live requests into each tenant's ledger", async () => {
  const queue = new InMemoryGoLiveQueue();
  queue.enqueue({ id: "j1", dto: goLiveToDto(requestFor("acme")) });
  queue.enqueue({ id: "j2", dto: goLiveToDto(requestFor("beta")) });
  const { ledgerFor, storeOf } = ledgers();

  const results = await new GoLiveWorker(queue, ledgerFor).drain("2026-08-31T00:00:00Z");
  assert.equal(results.length, 2);
  assert.ok(results.every((r) => r.ok));
  assert.ok(results.every((r) => r.openingEntryId));
  // each tenant's ledger has exactly the opening entry
  assert.equal((await storeOf("acme").list(asTenantId("acme"))).length, 1);
  assert.equal((await storeOf("beta").list(asTenantId("beta"))).length, 1);
});

test("a malformed job is isolated and doesn't block the others", async () => {
  const queue = new InMemoryGoLiveQueue();
  const bad: GoLiveDto = { ...goLiveToDto(requestFor("acme")), contract: "nope/1" };
  queue.enqueue({ id: "bad", dto: bad });
  queue.enqueue({ id: "good", dto: goLiveToDto(requestFor("beta")) });
  const { ledgerFor, storeOf } = ledgers();

  const results = await new GoLiveWorker(queue, ledgerFor).drain("2026-08-31T00:00:00Z");
  const byId = new Map(results.map((r) => [r.id, r]));
  assert.equal(byId.get("bad")!.ok, false);
  assert.match(byId.get("bad")!.error!, /go-live\/1/);
  assert.equal(byId.get("good")!.ok, true);
  assert.equal((await storeOf("beta").list(asTenantId("beta"))).length, 1);
});

test("processed jobs leave the pending set; re-draining is a no-op", async () => {
  const queue = new InMemoryGoLiveQueue();
  queue.enqueue({ id: "j1", dto: goLiveToDto(requestFor("acme")) });
  const { ledgerFor, storeOf } = ledgers();
  const worker = new GoLiveWorker(queue, ledgerFor);

  const first = await worker.drain("2026-08-31T00:00:00Z");
  assert.equal(first.length, 1);
  const second = await worker.drain("2026-08-31T00:00:00Z");
  assert.equal(second.length, 0); // nothing pending
  assert.equal((await storeOf("acme").list(asTenantId("acme"))).length, 1); // not double-posted
  assert.ok(queue.result("j1")?.ok);
});

test("sourceAccountsFromTrialBalance maps debit/credit columns to signed balances", () => {
  const src = sourceAccountsFromTrialBalance([
    { code: "1000", name: "Checking", debitMinor: "2500000", creditMinor: "0", subtype: AccountSubtype.BANK },
    { code: "3900", name: "Retained Earnings", debitMinor: "0", creditMinor: "2500000", subtype: AccountSubtype.RETAINED_EARNINGS },
  ], USD);
  assert.equal(src[0]!.balance.toDecimalString(), "25000.00");
  assert.equal(src[1]!.balance.toDecimalString(), "-25000.00");
});
