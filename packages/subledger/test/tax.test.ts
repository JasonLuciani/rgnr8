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
import { taxOnAmount, taxOnMinor, taxedInvoiceToPostCommand, type DocPostContext } from "../src/index.js";

const usd = (s: string) => Money.fromDecimal(s, USD);
const TENANT = asTenantId("acme");
const POST_AT = "2026-08-11T00:00:00Z";
const PROV: Provenance = Object.freeze({
  sourceSystem: "test", sourceObject: "doc", sourceVersion: "1",
  effectiveDate: "2026-08-01", postedDate: "2026-08-01", ingestedAt: "2026-08-01T00:00:00Z",
  normalizationVersion: "n1", mappingVersion: "m1",
});
const CTX: DocPostContext = { tenantId: "acme", currency: USD, provenance: PROV };
const A = (c: string) => asAccountId(`gl.${c}`);

function acct(code: string, type: AccountType, subtype?: AccountSubtype): Account {
  return { id: A(code), code, name: code, type, currency: USD, ...(subtype ? { subtype } : {}) };
}
function coa(): ChartOfAccounts {
  return new ChartOfAccounts([
    acct("ar", AccountType.ASSET, AccountSubtype.ACCOUNTS_RECEIVABLE),
    acct("income", AccountType.REVENUE, AccountSubtype.INCOME),
    acct("salestax", AccountType.LIABILITY, AccountSubtype.SALES_TAX_PAYABLE),
  ]);
}

test("tax math is exact and rounds half-up (ppm rates)", () => {
  assert.equal(taxOnAmount(usd("100.00"), 82_500).toDecimalString(), "8.25"); // 8.25%
  assert.equal(taxOnAmount(usd("10.00"), 83_750).toDecimalString(), "0.84"); // 0.8375 -> 0.84 half-up
  assert.equal(taxOnMinor(1000n, 82_500).toString(), "83"); // 10.00 * 8.25% = 0.825 -> 0.83
  assert.equal(taxOnAmount(usd("100.00"), 0).toDecimalString(), "0.00"); // zero rate
});

test("taxed invoice: Dr AR (net+tax) / Cr income (net) / Cr sales-tax-payable (tax)", async () => {
  const chart = coa();
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart, store, new PeriodRegistry());

  const { command, net, tax, total } = taxedInvoiceToPostCommand(
    { id: "INV-1", customerId: "c1", issueDate: "2026-08-05", dueDate: "2026-09-04",
      lines: [{ description: "Widget", unitAmount: usd("100.00"), accountId: A("income") }] },
    { arControl: A("ar"), salesTaxPayable: A("salestax") },
    82_500, // 8.25%
    CTX,
  );
  assert.equal(net.toDecimalString(), "100.00");
  assert.equal(tax.toDecimalString(), "8.25");
  assert.equal(total.toDecimalString(), "108.25");

  await engine.post(command, { postedAt: POST_AT });
  const tb = await computeTrialBalance(store, TENANT, chart, USD);
  assert.ok(tb.inBalance);
  const bal = await accountBalances(store, TENANT, chart, USD);
  assert.equal(bal.get(A("ar"))?.toDecimalString(), "108.25");
  assert.equal(bal.get(A("income"))?.toDecimalString(), "100.00");
  assert.equal(bal.get(A("salestax"))?.toDecimalString(), "8.25"); // liability accrued
});

test("only taxable lines are taxed; non-taxable excluded", () => {
  const { net, tax, total } = taxedInvoiceToPostCommand(
    { id: "INV-2", customerId: "c1", issueDate: "2026-08-05", dueDate: "2026-09-04",
      lines: [
        { description: "Taxable", unitAmount: usd("100.00"), accountId: A("income") },
        { description: "Exempt labor", unitAmount: usd("50.00"), accountId: A("income"), taxable: false },
      ] },
    { arControl: A("ar"), salesTaxPayable: A("salestax") },
    100_000, // 10%
    CTX,
  );
  assert.equal(net.toDecimalString(), "150.00");
  assert.equal(tax.toDecimalString(), "10.00"); // 10% of the 100 taxable only
  assert.equal(total.toDecimalString(), "160.00");
});

test("a zero-tax invoice posts no tax line", async () => {
  const chart = coa();
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart, store, new PeriodRegistry());
  const { command, tax } = taxedInvoiceToPostCommand(
    { id: "INV-3", customerId: "c1", issueDate: "2026-08-05", dueDate: "2026-09-04",
      lines: [{ unitAmount: usd("100.00"), accountId: A("income"), taxable: false }] },
    { arControl: A("ar"), salesTaxPayable: A("salestax") },
    82_500,
    CTX,
  );
  assert.equal(tax.toDecimalString(), "0.00");
  await engine.post(command, { postedAt: POST_AT });
  const bal = await accountBalances(store, TENANT, chart, USD);
  assert.equal(bal.get(A("salestax")), undefined); // no tax posting at all
});
