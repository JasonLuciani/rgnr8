import { test } from "node:test";
import assert from "node:assert/strict";
import {
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
import type { Account } from "@rgnr8/ledger-kernel";
import {
  APSubledger,
  ARSubledger,
  defaultSubledgerAccounts,
  toAPPostingCommands,
  toARPostingCommands,
} from "../src/index.js";

const usd = (s: string) => Money.fromDecimal(s, USD);
const TENANT = asTenantId("acme");

function acct(code: string, type: AccountType): Account {
  return { id: asAccountId(`gl.${code}`), code, name: code, type, currency: USD };
}
function coa(): ChartOfAccounts {
  return new ChartOfAccounts([
    acct("cash", AccountType.ASSET),
    acct("ar_control", AccountType.ASSET),
    acct("ap_control", AccountType.LIABILITY),
    acct("revenue", AccountType.REVENUE),
    acct("expense", AccountType.EXPENSE),
    acct("bad_debt", AccountType.EXPENSE),
  ]);
}

test("posting subledger events to the GL ties the control accounts to the subledger", async () => {
  const ar = new ARSubledger(USD);
  ar.addInvoice({ id: "INV-1", customerId: "acme", issueDate: "2026-08-01", dueDate: "2026-08-31", amount: usd("10000.00") });
  ar.applyPayment({ documentId: "INV-1", date: "2026-08-10", amount: usd("4000.00") }); // open 6000

  const ap = new APSubledger(USD);
  ap.addBill({ id: "BILL-1", vendorId: "aws", billDate: "2026-08-05", dueDate: "2026-08-25", amount: usd("3000.00") });
  ap.applyPayment({ documentId: "BILL-1", date: "2026-08-20", amount: usd("3000.00") }); // open 0

  const map = defaultSubledgerAccounts("gl.");
  const ctx = { tenantId: "acme", currency: USD };
  const commands = [
    ...toARPostingCommands(ar.events(), map, ctx),
    ...toAPPostingCommands(ap.events(), map, ctx),
  ];

  const chart = coa();
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart, store, new PeriodRegistry());
  for (const cmd of commands) await engine.post(cmd, { postedAt: "2026-08-31T00:00:00Z" });

  const tb = await computeTrialBalance(store, TENANT, chart, USD);
  assert.ok(tb.inBalance);

  const bal = await accountBalances(store, TENANT, chart, USD);
  // GL AR control (debit-normal) equals the AR subledger's open receivables.
  assert.equal(bal.get(asAccountId("gl.ar_control"))?.toDecimalString(), ar.controlBalance().toDecimalString());
  assert.equal(bal.get(asAccountId("gl.ar_control"))?.toDecimalString(), "6000.00");
  // GL AP control (credit-normal, normal orientation) equals AP open bills.
  assert.equal(bal.get(asAccountId("gl.ap_control"))?.toDecimalString(), ap.controlBalance().toDecimalString());
  assert.equal(bal.get(asAccountId("gl.ap_control"))?.toDecimalString(), "0.00");
  // Revenue recognized; cash reflects the receipt minus the bill payment.
  assert.equal(bal.get(asAccountId("gl.revenue"))?.toDecimalString(), "10000.00");
  assert.equal(bal.get(asAccountId("gl.cash"))?.toDecimalString(), "1000.00"); // +4000 receipt − 3000 bill
});

test("re-posting the same subledger events is idempotent", async () => {
  const ar = new ARSubledger(USD);
  ar.addInvoice({ id: "INV-1", customerId: "acme", issueDate: "2026-08-01", dueDate: "2026-08-31", amount: usd("5000.00") });
  const map = defaultSubledgerAccounts("gl.");
  const ctx = { tenantId: "acme", currency: USD };
  const commands = toARPostingCommands(ar.events(), map, ctx);

  const chart = coa();
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart, store, new PeriodRegistry());
  for (const cmd of commands) await engine.post(cmd, { postedAt: "2026-08-31T00:00:00Z" });
  for (const cmd of commands) await engine.post(cmd, { postedAt: "2026-08-31T00:00:00Z" });
  assert.equal((await store.list(TENANT)).length, commands.length);
});
