import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountType,
  ChartOfAccounts,
  InMemoryLedgerStore,
  PeriodRegistry,
  PostingEngine,
  USD,
  asAccountId,
  asPeriodKey,
  asTenantId,
  computeTrialBalance,
} from "@rgnr8/ledger-kernel";
import type { Account } from "@rgnr8/ledger-kernel";
import {
  BankPlaidLikeAdapter,
  IngestionPipeline,
  PayrollGustoLikeAdapter,
  defaultAccountMap,
  postCanonicalToLedger,
} from "../src/index.js";
import { gustoRawRecord, plaidRawRecords } from "./helpers.js";

const TENANT = asTenantId("acme");
const POST_AT = "2026-08-11T00:00:00Z";

function acct(code: string, type: AccountType): Account {
  return { id: asAccountId(`gl.${code}`), code, name: code, type, currency: USD };
}

function coa(): ChartOfAccounts {
  return new ChartOfAccounts([
    acct("cash", AccountType.ASSET),
    acct("income", AccountType.REVENUE),
    acct("expense", AccountType.EXPENSE),
    acct("fees", AccountType.EXPENSE),
    acct("payroll_expense", AccountType.EXPENSE),
    acct("payroll_tax", AccountType.EXPENSE),
    acct("interest_income", AccountType.REVENUE),
  ]);
}

function ingestAll(): IngestionPipeline {
  const p = new IngestionPipeline()
    .register(new BankPlaidLikeAdapter("acme"))
    .register(new PayrollGustoLikeAdapter("acme"));
  p.ingest([...plaidRawRecords(), gustoRawRecord()]);
  return p;
}

function newEngine() {
  const chart = coa();
  const store = new InMemoryLedgerStore();
  const periods = new PeriodRegistry();
  return { chart, store, periods, engine: new PostingEngine(chart, store, periods) };
}

test("postCanonicalToLedger drives the feed into the ledger (the missing wire)", async () => {
  const pipeline = ingestAll();
  const { chart, store, engine } = newEngine();

  const report = await postCanonicalToLedger(
    pipeline.active(),
    defaultAccountMap(),
    engine,
    store,
    { postedAt: POST_AT },
  );

  assert.ok(report.posted > 0, "should have posted real entries");
  assert.equal(report.skippedTransfers, 2); // both legs of the internal transfer
  assert.equal(report.blockedByClosedPeriod, 0);
  assert.equal(report.failed.length, 0);

  // the ledger now actually holds the entries and ties out
  const tb = await computeTrialBalance(store, TENANT, chart, USD);
  assert.ok(tb.inBalance);
  assert.equal((await store.list(TENANT)).length, report.posted);
});

test("re-running the poster is idempotent — already-posted, not double-posted", async () => {
  const pipeline = ingestAll();
  const { store, engine } = newEngine();
  const map = defaultAccountMap();

  const first = await postCanonicalToLedger(pipeline.active(), map, engine, store, { postedAt: POST_AT });
  const countAfterFirst = (await store.list(TENANT)).length;

  const second = await postCanonicalToLedger(pipeline.active(), map, engine, store, { postedAt: POST_AT });
  const countAfterSecond = (await store.list(TENANT)).length;

  assert.equal(countAfterFirst, first.posted);
  assert.equal(countAfterSecond, countAfterFirst); // no duplicates
  assert.equal(second.posted, 0);
  assert.equal(second.alreadyPosted, first.posted);
});

test("transactions dated into a locked period are deferred, not thrown", async () => {
  const pipeline = ingestAll();
  const { store, periods, engine } = newEngine();
  // lock the period the fixture transactions fall in (2026-08)
  periods.lock(TENANT, asPeriodKey("2026-08"));

  const report = await postCanonicalToLedger(
    pipeline.active(),
    defaultAccountMap(),
    engine,
    store,
    { postedAt: POST_AT },
  );

  assert.equal(report.posted, 0);
  assert.ok(report.blockedByClosedPeriod > 0, "closed-period txns should be deferred");
  assert.equal(report.failed.length, 0); // deferral is not a failure
  assert.equal((await store.list(TENANT)).length, 0);
});
