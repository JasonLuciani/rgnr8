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
  InMemoryMasterDataStore,
  billToPostCommand,
  dueDateFromTerms,
  invoiceToPostCommand,
  resolveInvoiceLines,
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
  return {
    id: asAccountId(`gl.${code}`), code, name: code, type, currency: USD,
    ...(subtype !== undefined ? { subtype } : {}),
  };
}
function coa(): ChartOfAccounts {
  return new ChartOfAccounts([
    acct("ar", AccountType.ASSET, AccountSubtype.ACCOUNTS_RECEIVABLE),
    acct("ap", AccountType.LIABILITY, AccountSubtype.ACCOUNTS_PAYABLE),
    acct("services", AccountType.REVENUE, AccountSubtype.INCOME),
    acct("product", AccountType.REVENUE, AccountSubtype.INCOME),
    acct("materials", AccountType.EXPENSE, AccountSubtype.EXPENSE),
  ]);
}

test("master data store: per-tenant customer/vendor/item roundtrip", () => {
  const md = new InMemoryMasterDataStore();
  md.upsertCustomer("acme", { id: "c1", name: "Amy" });
  md.upsertCustomer("bright", { id: "c1", name: "Other" });
  assert.equal(md.getCustomer("acme", "c1")?.name, "Amy");
  assert.equal(md.getCustomer("bright", "c1")?.name, "Other"); // tenant-isolated
  assert.equal(md.getCustomer("acme", "c1")?.active, true); // active by default
  assert.equal(md.listCustomers("acme").length, 1);

  md.upsertVendor("acme", { id: "v1", name: "Norton", is1099: true, taxId: "12-3456789" });
  assert.equal(md.getVendor("acme", "v1")?.is1099, true);

  md.upsertItem("acme", {
    id: "i1", name: "Consulting", type: "SERVICE",
    unitPrice: usd("150.00"), incomeAccountId: asAccountId("gl.services"),
  });
  assert.equal(md.getItem("acme", "i1")?.unitPrice?.toDecimalString(), "150.00");
});

test("dueDateFromTerms adds net-N days", () => {
  assert.equal(dueDateFromTerms("2026-08-01", 30), "2026-08-31");
  assert.equal(dueDateFromTerms("2026-08-01", undefined), "2026-08-01");
});

test("line-item invoice: item defaults fill price + account, lines compute", () => {
  const md = new InMemoryMasterDataStore();
  md.upsertItem("acme", {
    id: "svc", name: "Consulting", type: "SERVICE",
    unitPrice: usd("150.00"), incomeAccountId: asAccountId("gl.services"),
  });
  const lines = resolveInvoiceLines(
    { id: "INV-1", customerId: "c1", issueDate: "2026-08-01", dueDate: "2026-08-31",
      lines: [{ itemId: "svc", quantity: 3 }] },
    USD,
    { items: { tenant: "acme", store: md } },
  );
  assert.equal(lines[0]?.amount.toDecimalString(), "450.00"); // 150 * 3
  assert.equal(String(lines[0]?.accountId), "gl.services");
});

test("line-item invoice posts a balanced JE: Dr AR total / Cr each income line", async () => {
  const chart = coa();
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart, store, new PeriodRegistry());

  const cmd = invoiceToPostCommand(
    { id: "INV-1", customerId: "c1", issueDate: "2026-08-05", dueDate: "2026-09-04",
      lines: [
        { description: "Design", unitAmount: usd("1000.00"), accountId: asAccountId("gl.services") },
        { description: "License", quantity: 2, unitAmount: usd("250.00"), accountId: asAccountId("gl.product") },
      ] },
    asAccountId("gl.ar"),
    CTX,
  );
  await engine.post(cmd, { postedAt: POST_AT });

  const tb = await computeTrialBalance(store, TENANT, chart, USD);
  assert.ok(tb.inBalance);
  const bal = await accountBalances(store, TENANT, chart, USD);
  assert.equal(bal.get(asAccountId("gl.ar"))?.toDecimalString(), "1500.00"); // 1000 + 2*250
  assert.equal(bal.get(asAccountId("gl.services"))?.toDecimalString(), "1000.00");
  assert.equal(bal.get(asAccountId("gl.product"))?.toDecimalString(), "500.00");
});

test("line-item bill posts Cr AP total / Dr each expense line", async () => {
  const chart = coa();
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart, store, new PeriodRegistry());

  const cmd = billToPostCommand(
    { id: "BILL-1", vendorId: "v1", billDate: "2026-08-06", dueDate: "2026-09-05",
      lines: [{ description: "Lumber", unitAmount: usd("400.00"), accountId: asAccountId("gl.materials") }] },
    asAccountId("gl.ap"),
    CTX,
  );
  await engine.post(cmd, { postedAt: POST_AT });

  const bal = await accountBalances(store, TENANT, chart, USD);
  assert.equal(bal.get(asAccountId("gl.ap"))?.toDecimalString(), "400.00");
  assert.equal(bal.get(asAccountId("gl.materials"))?.toDecimalString(), "400.00");
});

test("a line with no price/account and no item is rejected", () => {
  assert.throws(
    () =>
      resolveInvoiceLines(
        { id: "X", customerId: "c", issueDate: "2026-08-01", dueDate: "2026-08-01",
          lines: [{ description: "mystery" }] },
        USD,
      ),
    /no unitAmount/,
  );
});
