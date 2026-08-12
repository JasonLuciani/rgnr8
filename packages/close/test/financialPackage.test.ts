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
  asAccountId,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  computeTrialBalance,
  type Account,
  type PostCommand,
  type Provenance,
} from "@rgnr8/ledger-kernel";
import { computeIncomeStatement, computeBalanceSheet } from "@rgnr8/statements";
import {
  buildFinancialPackage,
  fingerprintContent,
  verifyFinancialPackage,
  type FinancialPackageInput,
} from "../src/index.js";

const TENANT = asTenantId("acme");
const AUG = asPeriodKey("2026-08");
const AS_OF = "2026-08-31";

function acct(id: string, code: string, name: string, type: AccountType): Account {
  return { id: asAccountId(id), code, name, type, currency: USD };
}

const COA = new ChartOfAccounts([
  acct("cash", "1000", "Cash", AccountType.ASSET),
  acct("ar", "1200", "Accounts Receivable", AccountType.ASSET),
  acct("equity", "3000", "Owner Equity", AccountType.EQUITY),
  acct("rev", "4000", "Sales", AccountType.REVENUE),
  acct("rent", "6000", "Rent", AccountType.EXPENSE),
]);

const PROV: Provenance = {
  sourceSystem: "test",
  sourceObject: "je",
  sourceVersion: "1",
  effectiveDate: "2026-08-01",
  postedDate: "2026-08-01",
  ingestedAt: "2026-08-01T00:00:00Z",
  normalizationVersion: "n/1",
  mappingVersion: "m/1",
};

function cmd(key: string, date: string, lines: PostCommand["lines"]): PostCommand {
  return {
    tenantId: TENANT,
    idempotencyKey: asIdempotencyKey(key),
    periodKey: AUG,
    currency: USD,
    entryDate: date,
    lines,
    provenance: PROV,
  };
}

// Build a small but complete set of books, then extract the package input.
async function realInput(
  store: InMemoryLedgerStore,
  engine: PostingEngine,
): Promise<FinancialPackageInput> {
  const tb = await computeTrialBalance(store, TENANT, COA, USD);
  const is = await computeIncomeStatement(store, TENANT, COA, "2026-08-01", AS_OF, USD);
  const bs = await computeBalanceSheet(store, TENANT, COA, AS_OF, USD);
  return {
    periodKey: "2026-08",
    currency: "USD",
    trialBalance: {
      rows: tb.rows.map((r) => ({
        code: r.code,
        name: r.name,
        debitMinor: r.debit.minorUnits.toString(),
        creditMinor: r.credit.minorUnits.toString(),
      })),
      totalDebitMinor: tb.totalDebit.minorUnits.toString(),
      totalCreditMinor: tb.totalCredit.minorUnits.toString(),
      inBalance: tb.inBalance,
    },
    incomeStatement: {
      revenueMinor: is.revenue.total.minorUnits.toString(),
      expensesMinor: is.expenses.total.minorUnits.toString(),
      netIncomeMinor: is.netIncome.minorUnits.toString(),
    },
    balanceSheet: {
      totalAssetsMinor: bs.totalAssets.minorUnits.toString(),
      totalLiabilitiesAndEquityMinor: bs.totalLiabilitiesAndEquity.minorUnits.toString(),
      netIncomeMinor: bs.netIncome.minorUnits.toString(),
      balances: bs.balances,
    },
  };
}

async function books(): Promise<FinancialPackageInput> {
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(COA, store, new PeriodRegistry());
  const d = asAccountId;
  await engine.post(
    cmd("open", "2026-08-01", [
      { accountId: d("cash"), side: "DEBIT", amount: Money.fromDecimal("50000.00", USD) },
      { accountId: d("equity"), side: "CREDIT", amount: Money.fromDecimal("50000.00", USD) },
    ]),
    { postedAt: "2026-08-01T00:00:00Z" },
  );
  await engine.post(
    cmd("sale", "2026-08-05", [
      { accountId: d("ar"), side: "DEBIT", amount: Money.fromDecimal("12000.00", USD) },
      { accountId: d("rev"), side: "CREDIT", amount: Money.fromDecimal("12000.00", USD) },
    ]),
    { postedAt: "2026-08-05T00:00:00Z" },
  );
  await engine.post(
    cmd("rent", "2026-08-10", [
      { accountId: d("rent"), side: "DEBIT", amount: Money.fromDecimal("4000.00", USD) },
      { accountId: d("cash"), side: "CREDIT", amount: Money.fromDecimal("4000.00", USD) },
    ]),
    { postedAt: "2026-08-10T00:00:00Z" },
  );
  return realInput(store, engine);
}

const META = { closedBy: "controller", closedAt: "2026-09-01T17:00:00Z", packagedAt: "2026-09-01T17:00:05Z" };

test("seals a closed period's real books and verifies", async () => {
  const input = await books();
  assert.equal(input.trialBalance.inBalance, true);
  const pkg = buildFinancialPackage(input, META);
  assert.equal(pkg.version, "financial-package/1");
  assert.equal(pkg.algorithm, "sha256");
  assert.match(pkg.fingerprint, /^[0-9a-f]{64}$/);
  // net income = 12000 revenue - 4000 rent = 8000.00 => 800000 minor
  assert.equal(pkg.incomeStatement.netIncomeMinor, "800000");
  assert.equal(verifyFinancialPackage(pkg).valid, true);
});

test("the fingerprint is reproducible from the same books (envelope-independent)", async () => {
  const input = await books();
  const a = buildFinancialPackage(input, META);
  const b = buildFinancialPackage(input, {
    closedBy: "someone-else",
    closedAt: "2027-01-01T00:00:00Z",
    packagedAt: "2027-01-01T00:00:09Z",
  });
  // different envelope metadata, same financial content → same fingerprint
  assert.equal(a.fingerprint, b.fingerprint);
});

test("tampering with a sealed number is detected", async () => {
  const input = await books();
  const pkg = buildFinancialPackage(input, META);
  const tampered = {
    ...pkg,
    incomeStatement: { ...pkg.incomeStatement, netIncomeMinor: "999999" },
  };
  const result = verifyFinancialPackage(tampered);
  assert.equal(result.valid, false);
  assert.notEqual(result.actual, result.expected);
});

test("an included QBO reconciliation is part of the fingerprint", async () => {
  const input = await books();
  const withRecon: FinancialPackageInput = {
    ...input,
    qboReconciliation: {
      inAgreement: true,
      totalAbsDeltaMinor: "0",
      mismatchCount: 0,
      onlyInRgnr8Count: 0,
      onlyInQboCount: 0,
    },
  };
  const base = fingerprintContent({ version: "financial-package/1", ...input });
  const withReconFp = fingerprintContent({ version: "financial-package/1", ...withRecon });
  assert.notEqual(base, withReconFp); // adding the recon changes the fingerprint

  const pkg = buildFinancialPackage(withRecon, META);
  assert.equal(verifyFinancialPackage(pkg).valid, true);
});
