import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountSubtype,
  BusinessCategory,
  GoLiveWorker,
  InMemoryChartStore,
  InMemoryGoLiveQueue,
  InMemoryLedgerStore,
  Money,
  PeriodRegistry,
  USD,
  asPeriodKey,
  asTenantId,
  goLiveToDto,
  sourceAccountsFromTrialBalance,
  type ChartStore,
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
function ledgers(): {
  ledgerFor: LedgerFor;
  storeOf: (t: string) => InMemoryLedgerStore;
  chartOf: (t: string) => InMemoryChartStore;
} {
  const stores = new Map<string, InMemoryLedgerStore>();
  const periods = new Map<string, PeriodRegistry>();
  const charts = new Map<string, InMemoryChartStore>();
  const ledgerFor: LedgerFor = (tenantId) => {
    if (!stores.has(tenantId)) {
      stores.set(tenantId, new InMemoryLedgerStore());
      periods.set(tenantId, new PeriodRegistry());
      charts.set(tenantId, new InMemoryChartStore());
    }
    return {
      store: stores.get(tenantId)!,
      periods: periods.get(tenantId)!,
      chartStore: charts.get(tenantId)!,
    };
  };
  return { ledgerFor, storeOf: (t) => stores.get(t)!, chartOf: (t) => charts.get(t)! };
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

test("worker-driven go-live persists the chart so chart(tenant) returns the accounts (A2)", async () => {
  const queue = new InMemoryGoLiveQueue();
  queue.enqueue({ id: "j1", dto: goLiveToDto(requestFor("acme")) });
  const { ledgerFor, chartOf } = ledgers();

  const [res] = await new GoLiveWorker(queue, ledgerFor).drain("2026-08-31T00:00:00Z");
  assert.ok(res!.ok);

  const chart = chartOf("acme").chart(asTenantId("acme"));
  assert.ok(chart, "the chart must be persisted, not discarded");
  // The brought-over source accounts and the OBE are all in the persisted chart.
  assert.ok(chart!.get("acct:1000" as never), "checking account persisted");
  assert.ok(chart!.get("acct:3010" as never), "opening balance equity persisted");
});

test("CRASH INJECTION: a chart-persist failure leaves NO orphan postings (A2)", async () => {
  // chartStore throws before any posting → because the chart is written first,
  // a failure can only ever leave chart-less + posting-less state, never a
  // posting that references an unpersisted account.
  const store = new InMemoryLedgerStore();
  const periods = new PeriodRegistry();
  const boom: ChartStore = { saveChart: () => Promise.reject(new Error("disk full")) };
  const ledgerFor: LedgerFor = () => ({ store, periods, chartStore: boom });

  const queue = new InMemoryGoLiveQueue();
  queue.enqueue({ id: "j1", dto: goLiveToDto(requestFor("acme")) });
  const [res] = await new GoLiveWorker(queue, ledgerFor).drain("2026-08-31T00:00:00Z");

  assert.equal(res!.ok, false);
  assert.match(res!.error!, /disk full/);
  assert.equal((await store.list(asTenantId("acme"))).length, 0, "no opening entry posted");
});

test("CRASH INJECTION: a lock failure after posting is idempotently resumable (A2)", async () => {
  // Fail the period lock on the first attempt (after the opening entry posts).
  // In-memory can't roll back, but: the chart is persisted (no orphan), and a
  // retry is idempotent — no duplicate opening entry, and the lock then holds.
  const store = new InMemoryLedgerStore();
  const chart = new InMemoryChartStore();
  let periods = new PeriodRegistry();
  let failLock = true;
  const flaky = () => {
    const p = new PeriodRegistry();
    const realLockThrough = p.lockThrough.bind(p);
    p.lockThrough = (t, period) => (failLock ? Promise.reject(new Error("lock lost")) : realLockThrough(t, period));
    return p;
  };
  periods = flaky();
  const ledgerFor: LedgerFor = () => ({ store, periods, chartStore: chart });

  const q1 = new InMemoryGoLiveQueue();
  q1.enqueue({ id: "j1", dto: goLiveToDto(requestFor("acme")) });
  const [first] = await new GoLiveWorker(q1, ledgerFor).drain("2026-08-31T00:00:00Z");
  assert.equal(first!.ok, false);
  assert.match(first!.error!, /lock lost/);
  // The opening entry did post; the chart is persisted (so it is not an orphan).
  assert.equal((await store.list(asTenantId("acme"))).length, 1);
  assert.ok(chart.chart(asTenantId("acme")));

  // Resume: lock now succeeds. Re-running is idempotent — still one entry, locked.
  failLock = false;
  const q2 = new InMemoryGoLiveQueue();
  q2.enqueue({ id: "j2", dto: goLiveToDto(requestFor("acme")) });
  const [second] = await new GoLiveWorker(q2, ledgerFor).drain("2026-08-31T00:00:00Z");
  assert.ok(second!.ok);
  assert.equal((await store.list(asTenantId("acme"))).length, 1, "no double opening entry");
  assert.equal(await periods.status(asTenantId("acme"), asPeriodKey("2026-08")), "LOCKED");
});

test("sourceAccountsFromTrialBalance maps debit/credit columns to signed balances", () => {
  const src = sourceAccountsFromTrialBalance([
    { code: "1000", name: "Checking", debitMinor: "2500000", creditMinor: "0", subtype: AccountSubtype.BANK },
    { code: "3900", name: "Retained Earnings", debitMinor: "0", creditMinor: "2500000", subtype: AccountSubtype.RETAINED_EARNINGS },
  ], USD);
  assert.equal(src[0]!.balance.toDecimalString(), "25000.00");
  assert.equal(src[1]!.balance.toDecimalString(), "-25000.00");
});
