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
} from "@rgnr8/ledger-kernel";
import type { Account } from "@rgnr8/ledger-kernel";
import {
  BankPlaidLikeAdapter,
  IngestionPipeline,
  PayrollGustoLikeAdapter,
  RuleSet,
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
    acct("software", AccountType.EXPENSE), // rule target
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

test("a matching rule codes to a specific account instead of the catch-all expense", async () => {
  const rules = new RuleSet([
    { id: "figma", priority: 10, match: { descriptionContains: "Figma" }, accountId: asAccountId("gl.software"), category: "Software" },
  ]);
  const chart = coa();
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart, store, new PeriodRegistry());

  await postCanonicalToLedger(ingestAll().active(), defaultAccountMap(), engine, store, {
    postedAt: POST_AT,
    rules,
  });

  const bal = await accountBalances(store, TENANT, chart, USD);
  // Figma charge codes to software, not the generic expense (dupes are deduped)
  assert.equal(bal.get(asAccountId("gl.software"))?.toDecimalString(), "89.99");
});

test("without a rule, the same transactions fall back to the default account", async () => {
  const chart = coa();
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart, store, new PeriodRegistry());

  await postCanonicalToLedger(ingestAll().active(), defaultAccountMap(), engine, store, {
    postedAt: POST_AT,
  });

  const bal = await accountBalances(store, TENANT, chart, USD);
  assert.equal(bal.get(asAccountId("gl.software")), undefined); // no rule → nothing coded here
});

test("rule priority: lower priority wins when several match", () => {
  const t = {
    id: "x", tenantId: "acme", accountId: "a", date: "2026-08-01",
    amount: { minorUnits: -8999n, currency: USD, abs: () => ({ minorUnits: 8999n }) } as never,
    direction: "OUTFLOW", description: "Figma subscription", counterparty: "Figma",
    kind: "PURCHASE", isInternalTransfer: false,
    source: { provider: "plaid", sourceType: "txn", sourceVersion: "1", fetchedAt: POST_AT },
  } as never;
  const rules = new RuleSet([
    { id: "general", priority: 100, match: { descriptionContains: "Figma" }, accountId: asAccountId("gl.expense") },
    { id: "specific", priority: 1, match: { counterpartyEquals: "figma" }, accountId: asAccountId("gl.software") },
  ]);
  assert.equal(String(rules.accountFor(t)), "gl.software"); // priority 1 beats 100
});
