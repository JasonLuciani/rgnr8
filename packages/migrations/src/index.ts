import { createHash } from "node:crypto";

/**
 * Forward-only SQL migrations. A production system needs its schema to evolve
 * deterministically and safely: migrations apply in version order, exactly once,
 * and a migration whose text was edited after it was applied is caught rather
 * than silently diverging. This runner does exactly that over a minimal
 * Postgres-compatible seam (the same `query(text, values?)` shape the ledger's
 * `Pool` satisfies — and pg-mem, so it's tested without a live database).
 */

export interface SqlExecutor {
  query(text: string, values?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
}

export interface Migration {
  /** Strictly increasing, unique. Gaps are allowed; order is by this number. */
  readonly version: number;
  readonly name: string;
  /** The DDL/DML to apply. Should be idempotent-friendly but is only ever run once. */
  readonly sql: string;
}

export interface AppliedMigration {
  readonly version: number;
  readonly name: string;
}

export interface MigrationReport {
  readonly applied: readonly AppliedMigration[];
  readonly skipped: number;
  readonly currentVersion: number;
}

export class MigrationError extends Error {
  override readonly name: string = "MigrationError";
}
export class MigrationDriftError extends MigrationError {
  override readonly name = "MigrationDriftError";
  constructor(version: number) {
    super(
      `migration ${version} was already applied with a different checksum — its SQL was edited after the fact; add a new migration instead of editing history`,
    );
  }
}

const MIGRATIONS_DDL = `
CREATE TABLE schema_migrations (
  version    integer PRIMARY KEY,
  name       text    NOT NULL,
  checksum   text    NOT NULL,
  applied_at text    NOT NULL
);`;

/**
 * Ensure the bookkeeping table exists, creating it only when absent. We probe
 * with a SELECT rather than `CREATE TABLE IF NOT EXISTS` so the CREATE runs at
 * most once — issuing IF NOT EXISTS repeatedly is both wasteful and trips some
 * engines' planners on the no-op path.
 */
async function ensureTable(db: SqlExecutor): Promise<void> {
  try {
    await db.query("SELECT 1 FROM schema_migrations LIMIT 0");
  } catch {
    await db.query(MIGRATIONS_DDL);
  }
}

function checksum(sql: string): string {
  return createHash("sha256").update(sql, "utf8").digest("hex");
}

function assertOrdered(migrations: readonly Migration[]): Migration[] {
  const seen = new Set<number>();
  for (const m of migrations) {
    if (!Number.isInteger(m.version)) throw new MigrationError(`version must be an integer: ${m.version}`);
    if (seen.has(m.version)) throw new MigrationError(`duplicate migration version ${m.version}`);
    seen.add(m.version);
  }
  return [...migrations].sort((a, b) => a.version - b.version);
}

export interface RunOptions {
  /** ISO timestamp recorded for applied migrations; injected for determinism. */
  readonly appliedAt: string;
}

/**
 * Apply every pending migration in version order. Idempotent: a migration whose
 * version is already recorded is skipped after a checksum check (drift → throw).
 * Runs each migration and its bookkeeping insert inside a transaction so a
 * failure leaves the schema-version table consistent with what actually applied.
 */
export async function runMigrations(
  db: SqlExecutor,
  migrations: readonly Migration[],
  opts: RunOptions,
): Promise<MigrationReport> {
  const ordered = assertOrdered(migrations);
  await ensureTable(db);

  const existing = await db.query("SELECT version, checksum FROM schema_migrations");
  const applied = new Map<number, string>();
  for (const row of existing.rows) {
    applied.set(Number(row["version"]), String(row["checksum"]));
  }

  const appliedNow: AppliedMigration[] = [];
  let skipped = 0;

  for (const m of ordered) {
    const sum = checksum(m.sql);
    const prior = applied.get(m.version);
    if (prior !== undefined) {
      if (prior !== sum) throw new MigrationDriftError(m.version);
      skipped += 1;
      continue;
    }

    await db.query("BEGIN");
    try {
      await db.query(m.sql);
      await db.query(
        "INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES ($1, $2, $3, $4)",
        [m.version, m.name, sum, opts.appliedAt],
      );
      await db.query("COMMIT");
    } catch (err) {
      await safeRollback(db);
      throw err instanceof Error ? err : new MigrationError(String(err));
    }
    appliedNow.push({ version: m.version, name: m.name });
  }

  const currentVersion = ordered.length > 0 ? (ordered[ordered.length - 1] as Migration).version : 0;
  return { applied: appliedNow, skipped, currentVersion };
}

/** The highest applied migration version (0 if none / table absent). */
export async function currentVersion(db: SqlExecutor): Promise<number> {
  await ensureTable(db);
  const res = await db.query("SELECT version FROM schema_migrations");
  let max = 0;
  for (const row of res.rows) max = Math.max(max, Number(row["version"]));
  return max;
}

async function safeRollback(db: SqlExecutor): Promise<void> {
  try {
    await db.query("ROLLBACK");
  } catch {
    /* ignore rollback failure */
  }
}
