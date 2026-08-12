import type { Migration, SqlExecutor } from "@rgnr8/migrations";
import { verifyFinancialPackage, type FinancialPackage } from "./financialPackage.js";

/**
 * Persistence for the immutable financial package. A closed period's sealed
 * package is a *published record*: once stored it must be retrievable, verified
 * on the way in and out, and never silently overwritten. This module defines a
 * small store seam (`save` / `get` / `list`) with an in-memory implementation
 * and a SQL one over the same executor seam the ledger and migration runner use.
 *
 * Two invariants are enforced here:
 *  - **integrity** — a package is fingerprint-verified before it is stored and
 *    again when it is read; a mismatch throws `PackageIntegrityError`.
 *  - **immutability** — writing a *different* package for a (tenant, period)
 *    that already has one throws `PackagePublishedError`; re-writing the
 *    identical package (same fingerprint) is an idempotent no-op.
 */

export class PackageIntegrityError extends Error {
  override readonly name: string = "PackageIntegrityError";
}
export class PackagePublishedError extends Error {
  override readonly name = "PackagePublishedError";
  constructor(tenantId: string, periodKey: string) {
    super(
      `a different financial package is already published for ${tenantId} ${periodKey}; a sealed period's record is immutable`,
    );
  }
}

export interface FinancialPackageStore {
  save(tenantId: string, pkg: FinancialPackage): Promise<void>;
  get(tenantId: string, periodKey: string): Promise<FinancialPackage | null>;
  list(tenantId: string): Promise<readonly string[]>; // period keys, sorted
}

function assertIntact(pkg: FinancialPackage): void {
  const v = verifyFinancialPackage(pkg);
  if (!v.valid) {
    throw new PackageIntegrityError(
      `financial package for ${pkg.periodKey} failed verification (expected ${v.expected}, got ${v.actual})`,
    );
  }
}

export class InMemoryFinancialPackageStore implements FinancialPackageStore {
  private readonly byTenant = new Map<string, Map<string, FinancialPackage>>();

  async save(tenantId: string, pkg: FinancialPackage): Promise<void> {
    assertIntact(pkg);
    const periods = this.byTenant.get(tenantId) ?? new Map<string, FinancialPackage>();
    const existing = periods.get(pkg.periodKey);
    if (existing && existing.fingerprint !== pkg.fingerprint) {
      throw new PackagePublishedError(tenantId, pkg.periodKey);
    }
    periods.set(pkg.periodKey, pkg);
    this.byTenant.set(tenantId, periods);
  }

  async get(tenantId: string, periodKey: string): Promise<FinancialPackage | null> {
    const pkg = this.byTenant.get(tenantId)?.get(periodKey) ?? null;
    if (pkg) assertIntact(pkg);
    return pkg;
  }

  async list(tenantId: string): Promise<readonly string[]> {
    return [...(this.byTenant.get(tenantId)?.keys() ?? [])].sort();
  }
}

/** Table for the SQL store — register through `@rgnr8/migrations`. */
export const FINANCIAL_PACKAGE_MIGRATIONS: readonly Migration[] = [
  {
    version: 1,
    name: "financial_package",
    sql: `CREATE TABLE financial_package (
      tenant_id    text NOT NULL,
      period_key   text NOT NULL,
      fingerprint  text NOT NULL,
      package_json text NOT NULL,
      CONSTRAINT financial_package_pk PRIMARY KEY (tenant_id, period_key)
    );`,
  },
];

export class SqlFinancialPackageStore implements FinancialPackageStore {
  constructor(private readonly db: SqlExecutor) {}

  async save(tenantId: string, pkg: FinancialPackage): Promise<void> {
    assertIntact(pkg);
    const existing = await this.db.query(
      "SELECT fingerprint FROM financial_package WHERE tenant_id = $1 AND period_key = $2",
      [tenantId, pkg.periodKey],
    );
    const row = existing.rows[0];
    if (row !== undefined) {
      if (String(row["fingerprint"]) !== pkg.fingerprint) {
        throw new PackagePublishedError(tenantId, pkg.periodKey);
      }
      return; // identical package already published — idempotent
    }
    await this.db.query(
      "INSERT INTO financial_package (tenant_id, period_key, fingerprint, package_json) VALUES ($1, $2, $3, $4)",
      [tenantId, pkg.periodKey, pkg.fingerprint, JSON.stringify(pkg)],
    );
  }

  async get(tenantId: string, periodKey: string): Promise<FinancialPackage | null> {
    const res = await this.db.query(
      "SELECT package_json FROM financial_package WHERE tenant_id = $1 AND period_key = $2",
      [tenantId, periodKey],
    );
    const row = res.rows[0];
    if (row === undefined) return null;
    const pkg = JSON.parse(String(row["package_json"])) as FinancialPackage;
    assertIntact(pkg); // detects a row edited in the database
    return pkg;
  }

  async list(tenantId: string): Promise<readonly string[]> {
    const res = await this.db.query(
      "SELECT period_key FROM financial_package WHERE tenant_id = $1",
      [tenantId],
    );
    return res.rows.map((r) => String(r["period_key"])).sort();
  }
}
