import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountType,
  ChartOfAccounts,
  InMemoryLedgerStore,
  PeriodRegistry,
  PostingEngine,
  USD,
  accountBalances,
  asAccountId,
  asTenantId,
  computeTrialBalance,
} from "@rgnr8/ledger-kernel";
import type { Account } from "@rgnr8/ledger-kernel";
import {
  BankPlaidLikeAdapter,
  IngestionPipeline,
  PayrollGustoLikeAdapter,
  defaultAccountMap,
  toPostingCommands,
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

test("ingested transactions map to balanced journals and the trial balance ties", async () => {
  const pipeline = ingestAll();
  const { commands, skippedTransfers } = toPostingCommands(pipeline.active(), defaultAccountMap());

  assert.equal(skippedTransfers, 2); // both legs of the internal transfer skipped

  const chart = coa();
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart, store, new PeriodRegistry());
  for (const cmd of commands) {
    await engine.post(cmd, { postedAt: POST_AT });
  }

  const tb = await computeTrialBalance(store, TENANT, chart, USD);
  assert.ok(tb.inBalance);
  assert.equal(tb.totalDebit.toDecimalString(), tb.totalCredit.toDecimalString());

  // Cash nets: +5000 - 120.50 - 35 - 89.99 - 16000 - 4200 = -15445.49
  const balances = await accountBalances(store, TENANT, chart, USD);
  assert.equal(balances.get(asAccountId("gl.cash"))?.toDecimalString(), "-15445.49");
  assert.equal(balances.get(asAccountId("gl.payroll_expense"))?.toDecimalString(), "16000.00");
  assert.equal(balances.get(asAccountId("gl.income"))?.toDecimalString(), "5000.00");
});

test("re-posting mapped commands is idempotent (no duplicate entries)", async () => {
  const pipeline = ingestAll();
  const { commands } = toPostingCommands(pipeline.active(), defaultAccountMap());

  const chart = coa();
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart, store, new PeriodRegistry());
  for (const cmd of commands) await engine.post(cmd, { postedAt: POST_AT });
  const afterFirst = (await store.list(TENANT)).length;
  for (const cmd of commands) await engine.post(cmd, { postedAt: POST_AT });
  const afterSecond = (await store.list(TENANT)).length;

  assert.equal(afterFirst, commands.length);
  assert.equal(afterSecond, afterFirst); // idempotency keys prevent duplicates
});
