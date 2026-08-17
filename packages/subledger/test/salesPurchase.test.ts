import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountSubtype,
  AccountType,
  ChartOfAccounts,
  InMemoryLedgerStore,
  Money,
  PeriodRegistry,
  PostingEngine,
  USD,
  accountBalances,
  asAccountId,
  asTenantId,
  computeTrialBalance,
} from "@rgnr8/ledger-kernel";
import type { Account, Provenance } from "@rgnr8/ledger-kernel";
import {
  creditMemoToPostCommand,
  depositToPostCommand,
  expenseToPostCommand,
  refundReceiptToPostCommand,
  salesReceiptToPostCommand,
  vendorCreditToPostCommand,
  type DocPostContext,
} from "../src/index.js";

const usd = (s: string) => Money.fromDecimal(s, USD);
const TENANT = asTenantId("acme");
const POST_AT = "2026-08-11T00:00:00Z";
const PROV: Provenance = Object.freeze({
  sourceSystem: "test", sourceObject: "doc", sourceVersion: "1",
  effectiveDate: "2026-08-01", postedDate: "2026-08-01", ingestedAt: "2026-08-01T00:00:00Z",
  normalizationVersion: "n1", mappingVersion: "m1",
});
const CTX: DocPostContext = { tenantId: "acme", currency: USD, provenance: PROV };

function acct(code: string, type: AccountType, subtype?: AccountSubtype): Account {
  return { id: asAccountId(`gl.${code}`), code, name: code, type, currency: USD, ...(subtype ? { subtype } : {}) };
}
function coa(): ChartOfAccounts {
  return new ChartOfAccounts([
    acct("cash", AccountType.ASSET, AccountSubtype.BANK),
    acct("undeposited", AccountType.ASSET, AccountSubtype.UNDEPOSITED_FUNDS),
    acct("ar", AccountType.ASSET, AccountSubtype.ACCOUNTS_RECEIVABLE),
    acct("ap", AccountType.LIABILITY, AccountSubtype.ACCOUNTS_PAYABLE),
    acct("income", AccountType.REVENUE, AccountSubtype.INCOME),
    acct("expense", AccountType.EXPENSE, AccountSubtype.EXPENSE),
  ]);
}
const A = (c: string) => asAccountId(`gl.${c}`);

async function postAndBalances(cmds: Parameters<PostingEngine["post"]>[0][]) {
  const chart = coa();
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart, store, new PeriodRegistry());
  for (const c of cmds) await engine.post(c, { postedAt: POST_AT });
  const tb = await computeTrialBalance(store, TENANT, chart, USD);
  assert.ok(tb.inBalance, "must be balanced");
  return accountBalances(store, TENANT, chart, USD);
}

test("sales receipt: cash sale Dr cash / Cr income", async () => {
  const bal = await postAndBalances([
    salesReceiptToPostCommand(
      { id: "SR-1", customerId: "c1", date: "2026-08-05", lines: [{ unitAmount: usd("300.00"), accountId: A("income") }] },
      A("cash"), CTX,
    ),
  ]);
  assert.equal(bal.get(A("cash"))?.toDecimalString(), "300.00");
  assert.equal(bal.get(A("income"))?.toDecimalString(), "300.00");
});

test("credit memo reduces AR (reverse of an invoice)", async () => {
  const bal = await postAndBalances([
    creditMemoToPostCommand(
      { id: "CM-1", customerId: "c1", date: "2026-08-06", lines: [{ unitAmount: usd("100.00"), accountId: A("income") }] },
      A("ar"), CTX,
    ),
  ]);
  // AR (asset, debit-normal) goes negative by 100; income reversed negative
  assert.equal(bal.get(A("ar"))?.toDecimalString(), "-100.00");
  assert.equal(bal.get(A("income"))?.toDecimalString(), "-100.00");
});

test("refund receipt: cash back Cr cash / Dr income", async () => {
  const bal = await postAndBalances([
    refundReceiptToPostCommand(
      { id: "RR-1", customerId: "c1", date: "2026-08-07", lines: [{ unitAmount: usd("50.00"), accountId: A("income") }] },
      A("cash"), CTX,
    ),
  ]);
  assert.equal(bal.get(A("cash"))?.toDecimalString(), "-50.00");
});

test("vendor credit reduces AP (reverse of a bill)", async () => {
  const bal = await postAndBalances([
    vendorCreditToPostCommand(
      { id: "VC-1", vendorId: "v1", date: "2026-08-08", lines: [{ unitAmount: usd("80.00"), accountId: A("expense") }] },
      A("ap"), CTX,
    ),
  ]);
  // AP (liability, credit-normal) reduced by 80 → oriented -80
  assert.equal(bal.get(A("ap"))?.toDecimalString(), "-80.00");
  assert.equal(bal.get(A("expense"))?.toDecimalString(), "-80.00");
});

test("check/expense: cash purchase Dr expense / Cr cash", async () => {
  const bal = await postAndBalances([
    expenseToPostCommand(
      { id: "EXP-1", vendorId: "v1", date: "2026-08-09", lines: [{ unitAmount: usd("120.00"), accountId: A("expense") }] },
      A("cash"), CTX,
    ),
  ]);
  assert.equal(bal.get(A("expense"))?.toDecimalString(), "120.00");
  assert.equal(bal.get(A("cash"))?.toDecimalString(), "-120.00");
});

test("deposit: money into bank Dr bank / Cr undeposited", async () => {
  const bal = await postAndBalances([
    depositToPostCommand(
      { id: "DEP-1", date: "2026-08-10", lines: [{ unitAmount: usd("300.00"), accountId: A("undeposited") }] },
      A("cash"), CTX,
    ),
  ]);
  assert.equal(bal.get(A("cash"))?.toDecimalString(), "300.00");
  assert.equal(bal.get(A("undeposited"))?.toDecimalString(), "-300.00");
});
