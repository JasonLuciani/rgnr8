/**
 * The ledger service's schema as a governed, versioned migration list.
 *
 * Previously every store ran its own `CREATE TABLE IF NOT EXISTS` at boot, with
 * no version or checksum tracking and no safe path to ALTER a column. That is
 * exactly what `@rgnr8/migrations` exists to prevent: this module gathers every
 * store's DDL into one ordered {@link Migration} list so `runMigrations` applies
 * each exactly once, records it in `schema_migrations`, and refuses a silently
 * edited migration (checksum drift).
 *
 * RULES:
 *  - Migrations are FORWARD-ONLY and IMMUTABLE. Never edit an applied migration's
 *    `sql` — the checksum guard will (correctly) reject it. To change the schema,
 *    append a NEW migration with the next version (e.g. an `ALTER TABLE`).
 *  - Versions are strictly increasing and never reused. Keep the numbering dense
 *    and append-only.
 *
 * The initial table DDL uses `CREATE TABLE IF NOT EXISTS`, so adopting the
 * governed runner over a database that already has the tables (from the old
 * boot path) is safe: the first governed run records the checksums and no-ops
 * the creates.
 */

import { runMigrations, type Migration, type SqlExecutor } from "@rgnr8/migrations";
import { CLOSE_STATE_MIGRATIONS, FINANCIAL_PACKAGE_MIGRATIONS } from "@rgnr8/close";
import { ACCOUNT_DDL, CORE_DDL } from "@rgnr8/ledger-postgres";
import { DOCUMENT_DDL } from "./documents.js";
import { RECON_DDL } from "./reconcile.js";
import { FEED_DDL } from "./feed.js";
import { PAYROLL_DDL } from "./payroll.js";
import { BUDGET_DDL } from "./reporting.js";
import { DIMENSION_DDL } from "./dimensions.js";
import { ATTACHMENT_DDL } from "./attachments.js";
import { RECURRING_DDL } from "./recurring.js";
import { JOB_DDL } from "./jobs.js";
import { ESTIMATE_DDL } from "./estimates.js";
import { SALES_ORDER_DDL } from "./salesorders.js";
import { WORK_ORDER_DDL } from "./workorders.js";
import { PURCHASING_DDL } from "./purchasing.js";
import { BILLING_DDL } from "./billing.js";
import { INVENTORY_DDL } from "./inventory.js";
import { CRM_DDL } from "./crm.js";
import { CONSOLIDATION_DDL } from "./consolidation.js";
import { SETTINGS_DDL } from "./settings.js";
import { DEBT_DDL } from "./debt.js";
import { FIXED_ASSET_DDL } from "./fixedassets.js";
import { rlsDdl } from "./security.js";

/**
 * The ordered schema migrations. Dependency order: core ledger + chart first,
 * then the AR/AP and subledger tables, then row-level-security policies last
 * (they reference every tenant table, so those must all exist first).
 *
 * @param enforceRls when false (the pg-mem test double, which cannot run the
 *   policy plpgsql) the RLS migration is omitted — a per-database decision, so a
 *   given database's `schema_migrations` stays internally consistent.
 */
export function ledgerMigrations(opts: { enforceRls: boolean }): Migration[] {
  const tables: Migration[] = [
    { version: 1, name: "ledger_core", sql: CORE_DDL },
    { version: 2, name: "account", sql: ACCOUNT_DDL },
    { version: 3, name: "documents", sql: DOCUMENT_DDL },
    { version: 4, name: "reconciliation", sql: RECON_DDL },
    { version: 5, name: "bank_feed", sql: FEED_DDL },
    { version: 6, name: "payroll", sql: PAYROLL_DDL },
    { version: 7, name: "budgets", sql: BUDGET_DDL },
    { version: 8, name: "dimensions", sql: DIMENSION_DDL },
    { version: 9, name: "attachments", sql: ATTACHMENT_DDL },
    { version: 10, name: "recurring", sql: RECURRING_DDL },
    { version: 11, name: "jobs", sql: JOB_DDL },
    { version: 12, name: "estimates", sql: ESTIMATE_DDL },
    { version: 13, name: "sales_orders", sql: SALES_ORDER_DDL },
    { version: 14, name: "work_orders", sql: WORK_ORDER_DDL },
    { version: 15, name: "purchasing", sql: PURCHASING_DDL },
    { version: 16, name: "billing", sql: BILLING_DDL },
    { version: 17, name: "inventory", sql: INVENTORY_DDL },
    { version: 18, name: "crm", sql: CRM_DDL },
    { version: 19, name: "consolidation", sql: CONSOLIDATION_DDL },
    { version: 20, name: "settings", sql: SETTINGS_DDL },
    { version: 21, name: "debt", sql: DEBT_DDL },
    { version: 22, name: "fixed_assets", sql: FIXED_ASSET_DDL },
    // Close / publication records (from @rgnr8/close). Their own version numbers
    // are re-based here into this service's single sequence.
    { version: 23, name: "financial_package", sql: FINANCIAL_PACKAGE_MIGRATIONS[0]!.sql },
    { version: 24, name: "close_state", sql: CLOSE_STATE_MIGRATIONS[0]!.sql },
  ];
  // RLS policies key on every tenant table, so they migrate last.
  if (opts.enforceRls) {
    tables.push({ version: 100, name: "rls_policies", sql: rlsDdl() });
  }
  return tables;
}

/** Apply the governed schema to a database. `appliedAt` is injected. */
export function migrateLedgerSchema(
  db: SqlExecutor,
  opts: { enforceRls: boolean; appliedAt: string },
): Promise<unknown> {
  return runMigrations(db, ledgerMigrations({ enforceRls: opts.enforceRls }), {
    appliedAt: opts.appliedAt,
  });
}
