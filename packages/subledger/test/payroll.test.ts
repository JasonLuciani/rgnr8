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
  PayrollError,
  payrollRunToPostCommand,
  payrollTotals,
  type DocPostContext,
  type PayrollAccounts,
  type PayrollRun,
} from "../src/index.js";

const usd = (s: string) => Money.fromDecimal(s, USD);
const TENANT = asTenantId("acme");
const POST_AT = "2026-08-11T00:00:00Z";
const PROV: Provenance = Object.freeze({
  sourceSystem: "test", sourceObject: "payroll", sourceVersion: "1",
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
    acct("wages", AccountType.EXPENSE, AccountSubtype.EXPENSE),
    acct("ertax_exp", AccountType.EXPENSE, AccountSubtype.EXPENSE),
    acct("cash", AccountType.ASSET, AccountSubtype.BANK),
    acct("ee_tax_pay", AccountType.LIABILITY, AccountSubtype.OTHER_CURRENT_LIABILITY),
    acct("er_tax_pay", AccountType.LIABILITY, AccountSubtype.OTHER_CURRENT_LIABILITY),
    acct("deduct_pay", AccountType.LIABILITY, AccountSubtype.OTHER_CURRENT_LIABILITY),
  ]);
}
const ACCOUNTS: PayrollAccounts = {
  wagesExpense: A("wages"), employerTaxExpense: A("ertax_exp"), cash: A("cash"),
  employeeTaxPayable: A("ee_tax_pay"), employerTaxPayable: A("er_tax_pay"), deductionsPayable: A("deduct_pay"),
};

// Two employees; net = gross − employeeTaxes − deductions
const RUN: PayrollRun = {
  id: "PR-2026-08-15", date: "2026-08-15",
  employees: [
    { employeeId: "e1", grossPay: usd("5000.00"), employeeTaxes: usd("1200.00"), deductions: usd("300.00"), netPay: usd("3500.00") },
    { employeeId: "e2", grossPay: usd("4000.00"), employeeTaxes: usd("900.00"), netPay: usd("3100.00") },
  ],
  employerTaxes: usd("700.00"),
};

test("payrollTotals sums the run and validates net-to-gross", () => {
  const t = payrollTotals(RUN, USD);
  assert.equal(t.gross.toDecimalString(), "9000.00");
  assert.equal(t.employeeTaxes.toDecimalString(), "2100.00");
  assert.equal(t.deductions.toDecimalString(), "300.00");
  assert.equal(t.net.toDecimalString(), "6600.00");
  assert.equal(t.employerTaxes.toDecimalString(), "700.00");
});

test("a mismatched net is rejected", () => {
  const bad: PayrollRun = {
    ...RUN,
    employees: [{ employeeId: "e1", grossPay: usd("5000.00"), employeeTaxes: usd("1200.00"), netPay: usd("9999.00") }],
  };
  assert.throws(() => payrollTotals(bad, USD), PayrollError);
});

test("payroll run posts a balanced gross-to-net journal with liability accrual", async () => {
  const chart = coa();
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart, store, new PeriodRegistry());

  await engine.post(payrollRunToPostCommand(RUN, ACCOUNTS, CTX), { postedAt: POST_AT });

  const tb = await computeTrialBalance(store, TENANT, chart, USD);
  assert.ok(tb.inBalance);
  const bal = await accountBalances(store, TENANT, chart, USD);
  assert.equal(bal.get(A("wages"))?.toDecimalString(), "9000.00"); // gross expense
  assert.equal(bal.get(A("ertax_exp"))?.toDecimalString(), "700.00"); // employer tax expense
  assert.equal(bal.get(A("cash"))?.toDecimalString(), "-6600.00"); // net pay out
  assert.equal(bal.get(A("ee_tax_pay"))?.toDecimalString(), "2100.00"); // employee withholdings accrued
  assert.equal(bal.get(A("er_tax_pay"))?.toDecimalString(), "700.00"); // employer taxes accrued
  assert.equal(bal.get(A("deduct_pay"))?.toDecimalString(), "300.00"); // deductions accrued
});
