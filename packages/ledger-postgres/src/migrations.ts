import type { Migration } from "@rgnr8/migrations";
import { CORE_DDL, RLS_DDL } from "./schema.js";
import { ACCOUNT_DDL, ACCOUNT_RLS_DDL } from "./accountSchema.js";

/**
 * The ledger schema as versioned, forward-only migrations for
 * `@rgnr8/migrations`. This replaces the ad-hoc `migrate()` (a raw
 * `CREATE TABLE IF NOT EXISTS`) with an ordered, checksum-tracked history, so
 * production schema changes are applied once, in order, and can never silently
 * diverge.
 *
 * `LEDGER_MIGRATIONS` is the core schema (tables, constraints, indexes) — safe
 * against pg-mem, so it is what the tests run. Row-level security is a separate
 * migration (`LEDGER_MIGRATIONS_WITH_RLS`) because pg-mem does not implement RLS
 * and it must be applied under a non-owner role in production.
 */
export const LEDGER_MIGRATIONS: readonly Migration[] = [
  { version: 1, name: "ledger_core", sql: CORE_DDL },
];

export const LEDGER_MIGRATIONS_WITH_RLS: readonly Migration[] = [
  ...LEDGER_MIGRATIONS,
  { version: 2, name: "ledger_rls", sql: RLS_DDL },
];

/**
 * The persistent chart-of-accounts schema as a migration (version 3, after the
 * ledger core + RLS). RLS for the account table is a separate migration for the
 * same reason as the ledger's — pg-mem doesn't implement it and it applies under
 * a non-owner role in production.
 */
export const ACCOUNT_MIGRATIONS: readonly Migration[] = [
  { version: 3, name: "account_core", sql: ACCOUNT_DDL },
];

export const ACCOUNT_MIGRATIONS_WITH_RLS: readonly Migration[] = [
  ...ACCOUNT_MIGRATIONS,
  { version: 4, name: "account_rls", sql: ACCOUNT_RLS_DDL },
];

