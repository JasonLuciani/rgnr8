# @rgnr8/migrations

Forward-only SQL schema migrations: ordered, idempotent, and **drift-detecting**. A production system's schema has to evolve safely — migrations apply in version order, exactly once, and a migration whose text is edited after it was applied is caught rather than silently diverging.

TypeScript, zero runtime deps. 7 tests (via pg-mem), `tsc --strict` clean.

## The seam

`runMigrations(db, migrations, { appliedAt })` works over a minimal `SqlExecutor` — `query(text, values?) => { rows }` — the same shape the ledger's Postgres `Pool` satisfies (and pg-mem, so the whole runner is tested without a live database). No ORM, no driver dependency.

## What it does

- Records applied migrations in a `schema_migrations` table (`version, name, checksum, applied_at`), created on first use via an existence probe (not a repeated `CREATE TABLE IF NOT EXISTS`).
- Applies every **pending** migration in ascending `version` order (input order doesn't matter). Each migration + its bookkeeping insert run inside a `BEGIN/COMMIT`, rolling back on failure so the version table always reflects what actually applied.
- **Idempotent**: a version already recorded is skipped — but only after its checksum (SHA-256 of the SQL) matches. A changed checksum throws `MigrationDriftError`: history was edited; add a new migration instead.
- Rejects duplicate versions before running anything.
- `currentVersion(db)` reports the highest applied version.

```ts
const migrations = [
  { version: 1, name: "create_ledger", sql: CORE_DDL },
  { version: 2, name: "add_index", sql: "CREATE INDEX ..." },
];
const report = await runMigrations(pool, migrations, { appliedAt: new Date().toISOString() });
report.applied;        // [{version,name}, ...] applied this run
report.currentVersion; // highest version
```

## What's next

- A CLI wrapper and a `down`/repair path for development.
- Advisory-lock the runner so concurrent app boots don't race the same migration.
- Wire the ledger (`@rgnr8/ledger-postgres`) and web state tables (`SqlTenantStore`) through this instead of ad-hoc `CREATE TABLE` calls.
