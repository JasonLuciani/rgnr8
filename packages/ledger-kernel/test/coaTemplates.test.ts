import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountSubtype,
  AccountType,
  BusinessCategory,
  USD,
  asAccountId,
  buildChartForCategory,
  listCoaTemplates,
  templateAccounts,
  accountTypeOfSubtype,
} from "../src/index.js";

test("every template builds a valid chart with unique codes and derived types", () => {
  for (const category of Object.values(BusinessCategory)) {
    const chart = buildChartForCategory(category);
    const accounts = chart.list();
    assert.ok(accounts.length >= 25, `${category} should have a full base chart`);
    // codes unique
    const codes = new Set(accounts.map((a) => a.code));
    assert.equal(codes.size, accounts.length, `${category} has duplicate codes`);
    // every account's type matches its subtype (ChartOfAccounts would have thrown otherwise)
    for (const a of accounts) {
      if (a.subtype) assert.equal(a.type, accountTypeOfSubtype(a.subtype));
    }
  }
});

test("the base chart has the essential system accounts", () => {
  const chart = buildChartForCategory(BusinessCategory.SERVICE_GENERAL);
  const bank = chart.get(asAccountId("acct:1000"))!;
  assert.equal(bank.subtype, AccountSubtype.BANK);
  assert.equal(bank.type, AccountType.ASSET);
  // AR, AP, sales tax payable, retained earnings all present
  assert.ok(chart.list().some((a) => a.subtype === AccountSubtype.ACCOUNTS_RECEIVABLE));
  assert.ok(chart.list().some((a) => a.subtype === AccountSubtype.ACCOUNTS_PAYABLE));
  assert.ok(chart.list().some((a) => a.subtype === AccountSubtype.SALES_TAX_PAYABLE));
  assert.ok(chart.list().some((a) => a.subtype === AccountSubtype.RETAINED_EARNINGS));
});

test("category extras layer on top of the base (contractor has job costs)", () => {
  const contractor = buildChartForCategory(BusinessCategory.CONTRACTOR_TRADES);
  assert.ok(contractor.list().some((a) => a.name === "Job Materials"));
  assert.ok(contractor.list().some((a) => a.name === "Subcontractors"));
  // restaurant has food COGS + tips payable; general does not
  const restaurant = buildChartForCategory(BusinessCategory.RESTAURANT);
  assert.ok(restaurant.list().some((a) => a.name === "Tips Payable"));
  const general = buildChartForCategory(BusinessCategory.SERVICE_GENERAL);
  assert.equal(general.list().some((a) => a.name === "Tips Payable"), false);
});

test("templateAccounts are persistable domain objects in a chosen currency", () => {
  const accounts = templateAccounts(BusinessCategory.RETAIL, USD);
  assert.ok(accounts.every((a) => a.currency.code === "USD"));
  assert.ok(accounts.some((a) => a.subtype === AccountSubtype.INVENTORY)); // retail has inventory
});

test("listCoaTemplates exposes picker metadata for every category", () => {
  const templates = listCoaTemplates();
  assert.equal(templates.length, Object.values(BusinessCategory).length);
  for (const t of templates) {
    assert.ok(t.label.length > 0);
    assert.ok(t.description.length > 0);
    assert.ok(t.accountCount >= 25);
  }
});
