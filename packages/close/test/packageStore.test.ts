import { test } from "node:test";
import assert from "node:assert/strict";
import { newDb } from "pg-mem";
import { runMigrations, type SqlExecutor } from "@rgnr8/migrations";
import {
  FINANCIAL_PACKAGE_MIGRATIONS,
  InMemoryFinancialPackageStore,
  PackageIntegrityError,
  PackagePublishedError,
  SqlFinancialPackageStore,
  buildFinancialPackage,
  type FinancialPackage,
  type FinancialPackageInput,
  type FinancialPackageStore,
} from "../src/index.js";

const META = { closedBy: "controller", closedAt: "2026-09-01T17:00:00Z", packagedAt: "2026-09-01T17:00:05Z" };

function inputFor(period: string, netIncomeMinor: string): FinancialPackageInput {
  return {
    periodKey: period,
    currency: "USD",
    trialBalance: {
      rows: [{ code: "1000", name: "Cash", debitMinor: "4600000", creditMinor: "0" }],
      totalDebitMinor: "4600000",
      totalCreditMinor: "4600000",
      inBalance: true,
    },
    incomeStatement: { revenueMinor: "1200000", expensesMinor: "400000", netIncomeMinor: netIncomeMinor },
    balanceSheet: {
      totalAssetsMinor: "5800000",
      totalLiabilitiesAndEquityMinor: "5800000",
      netIncomeMinor: netIncomeMinor,
      balances: true,
    },
  };
}

function pkgFor(period: string, netIncomeMinor = "800000"): FinancialPackage {
  return buildFinancialPackage(inputFor(period, netIncomeMinor), META);
}

function makeDb(): SqlExecutor {
  const db = newDb();
  const pg = db.adapters.createPg();
  return new pg.Pool() as unknown as SqlExecutor;
}

async function sqlStore(): Promise<SqlFinancialPackageStore> {
  const db = makeDb();
  await runMigrations(db, FINANCIAL_PACKAGE_MIGRATIONS, { appliedAt: "2026-08-06T00:00:00Z" });
  return new SqlFinancialPackageStore(db);
}

// The same behavioral contract holds for both implementations.
function contractTests(name: string, make: () => Promise<FinancialPackageStore>): void {
  test(`${name}: saves and reads back a sealed package`, async () => {
    const store = await make();
    await store.save("acme", pkgFor("2026-08"));
    const got = await store.get("acme", "2026-08");
    assert.ok(got);
    assert.equal(got?.periodKey, "2026-08");
    assert.equal(got?.incomeStatement.netIncomeMinor, "800000");
    assert.match(got?.fingerprint ?? "", /^[0-9a-f]{64}$/);
  });

  test(`${name}: missing period returns null; list is sorted and tenant-scoped`, async () => {
    const store = await make();
    await store.save("acme", pkgFor("2026-07"));
    await store.save("acme", pkgFor("2026-08"));
    await store.save("beta", pkgFor("2026-08"));
    assert.equal(await store.get("acme", "2026-06"), null);
    assert.deepEqual(await store.list("acme"), ["2026-07", "2026-08"]);
    assert.deepEqual(await store.list("beta"), ["2026-08"]);
  });

  test(`${name}: re-saving the identical package is idempotent`, async () => {
    const store = await make();
    await store.save("acme", pkgFor("2026-08"));
    await store.save("acme", pkgFor("2026-08")); // same content → same fingerprint
    assert.deepEqual(await store.list("acme"), ["2026-08"]);
  });

  test(`${name}: publishing a different package for a sealed period is rejected`, async () => {
    const store = await make();
    await store.save("acme", pkgFor("2026-08", "800000"));
    await assert.rejects(
      () => store.save("acme", pkgFor("2026-08", "999999")), // different numbers
      (e) => e instanceof PackagePublishedError,
    );
  });

  test(`${name}: an internally inconsistent package is refused on save`, async () => {
    const store = await make();
    const tampered = { ...pkgFor("2026-08"), fingerprint: "0".repeat(64) };
    await assert.rejects(() => store.save("acme", tampered), (e) => e instanceof PackageIntegrityError);
  });
}

contractTests("in-memory", async () => new InMemoryFinancialPackageStore());
contractTests("sql", sqlStore);

test("sql: a package row edited in the database is caught on read", async () => {
  const db = makeDb();
  await runMigrations(db, FINANCIAL_PACKAGE_MIGRATIONS, { appliedAt: "2026-08-06T00:00:00Z" });
  const store = new SqlFinancialPackageStore(db);
  await store.save("acme", pkgFor("2026-08"));

  // tamper with the stored JSON directly (simulating a DB edit)
  const original = pkgFor("2026-08");
  const tampered = { ...original, incomeStatement: { ...original.incomeStatement, netIncomeMinor: "1" } };
  await db.query("UPDATE financial_package SET package_json = $1 WHERE tenant_id = $2 AND period_key = $3", [
    JSON.stringify(tampered),
    "acme",
    "2026-08",
  ]);

  await assert.rejects(() => store.get("acme", "2026-08"), (e) => e instanceof PackageIntegrityError);
});
