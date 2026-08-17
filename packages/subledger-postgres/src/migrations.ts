import type { Migration } from "@rgnr8/migrations";
import { MASTER_DATA_DDL, MASTER_DATA_RLS_DDL } from "./schema.js";

/**
 * The subledger master-data schema as forward-only migrations. The core tables
 * are pg-mem-safe (what the tests run); RLS is a separate migration applied
 * under a non-owner role in production.
 */
export const MASTER_DATA_MIGRATIONS: readonly Migration[] = [
  { version: 1, name: "master_data_core", sql: MASTER_DATA_DDL },
];

export const MASTER_DATA_MIGRATIONS_WITH_RLS: readonly Migration[] = [
  ...MASTER_DATA_MIGRATIONS,
  { version: 2, name: "master_data_rls", sql: MASTER_DATA_RLS_DDL },
];
