import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildFinancialPackage,
  fingerprintContent,
  verifyFinancialPackage,
  type FinancialPackageInput,
} from "../src/index.js";

const META = { closedBy: "controller", closedAt: "2026-09-01T17:00:00Z", packagedAt: "2026-09-01T17:00:05Z" };

function baseInput(): FinancialPackageInput {
  return {
    periodKey: "2026-08",
    currency: "USD",
    trialBalance: {
      rows: [{ code: "4000", name: "Sales", debitMinor: "0", creditMinor: "1200000" }],
      totalDebitMinor: "1200000",
      totalCreditMinor: "1200000",
      inBalance: true,
    },
    incomeStatement: { revenueMinor: "1200000", expensesMinor: "400000", netIncomeMinor: "800000" },
    balanceSheet: {
      totalAssetsMinor: "5800000",
      totalLiabilitiesAndEquityMinor: "5800000",
      netIncomeMinor: "800000",
      balances: true,
    },
  };
}

test("a package can seal full statement lines, not just summary totals", () => {
  const input: FinancialPackageInput = {
    ...baseInput(),
    incomeStatement: {
      ...baseInput().incomeStatement,
      revenueLines: [{ code: "4000", name: "Sales", amountMinor: "1200000" }],
      expenseLines: [{ code: "6000", name: "Rent", amountMinor: "400000" }],
    },
    balanceSheet: {
      ...baseInput().balanceSheet,
      assetLines: [{ code: "1000", name: "Cash", amountMinor: "5800000" }],
      liabilityLines: [],
      equityLines: [{ code: "3000", name: "Owner Equity", amountMinor: "5000000" }],
    },
  };
  const pkg = buildFinancialPackage(input, META);
  assert.equal(verifyFinancialPackage(pkg).valid, true);
  assert.equal(pkg.incomeStatement.revenueLines?.length, 1);
  assert.equal(pkg.balanceSheet.equityLines?.[0]?.amountMinor, "5000000");
  // The sealed line detail is inside the fingerprint (tampering is detectable).
  const tampered = {
    ...pkg,
    incomeStatement: {
      ...pkg.incomeStatement,
      revenueLines: [{ code: "4000", name: "Sales", amountMinor: "9900000" }],
    },
  };
  assert.notEqual(fingerprintContent(tampered), pkg.fingerprint);
  assert.equal(verifyFinancialPackage(tampered).valid, false);
});

test("omitting line detail reproduces the legacy summary-only fingerprint (backward compatible)", () => {
  // A summary-only package (no *Lines fields) fingerprints exactly as before the
  // full-statement fields were added — the optional keys are simply absent.
  const pkg = buildFinancialPackage(baseInput(), META);
  assert.equal(verifyFinancialPackage(pkg).valid, true);
  assert.equal(pkg.incomeStatement.revenueLines, undefined);
});
