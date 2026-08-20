import { test } from "node:test";
import assert from "node:assert/strict";
import { newDb } from "pg-mem";
import { currentVersion, runMigrations, MigrationDriftError } from "@rgnr8/migrations";
import { ledgerMigrations, migrateLedgerSchema } from "../src/migrations.js";

/** A pg-mem executor with the `query(text, values?)` shape the runner needs. */
function memDb(): { query(text: string, values?: unknown[]): Promise<{ rows: Record<string, unknown>[] }> } {
  const db = newDb();
  const pg = db.adapters.createPg();
  const pool = new pg.Pool();
  return pool as never;
}

const APPLIED_AT = "2026-08-20T00:00:00Z";

test("governed migrations apply, record schema_migrations, and are idempotent on re-run", async () => {
  const db = memDb();
  // pg-mem cannot run the RLS plpgsql, so exercise the table migrations.
  const migs = ledgerMigrations({ enforceRls: false });

  const first = (await migrateLedgerSchema(db, { enforceRls: false, appliedAt: APPLIED_AT })) as {
    applied: unknown[];
    skipped: number;
  };
  assert.equal(first.applied.length, migs.length, "every migration applied on a fresh DB");
  assert.equal(await currentVersion(db), 24);

  // A second run applies nothing (all versions recorded) and does not error.
  const second = (await migrateLedgerSchema(db, { enforceRls: false, appliedAt: APPLIED_AT })) as {
    applied: unknown[];
    skipped: number;
  };
  assert.equal(second.applied.length, 0, "re-run is a no-op");
  assert.equal(second.skipped, migs.length);

  // The bookkeeping table actually recorded the versions.
  const rows = await db.query("SELECT version FROM schema_migrations");
  assert.equal(rows.rows.length, migs.length);
});

test("checksum drift is caught — an edited applied migration is rejected", async () => {
  const db = memDb();
  await runMigrations(
    db,
    [{ version: 1, name: "t", sql: "CREATE TABLE drift_probe (id integer)" }],
    { appliedAt: APPLIED_AT },
  );
  // Same version, different SQL text = history was edited after the fact.
  await assert.rejects(
    () =>
      runMigrations(
        db,
        [{ version: 1, name: "t", sql: "CREATE TABLE drift_probe (id bigint)" }],
        { appliedAt: APPLIED_AT },
      ),
    MigrationDriftError,
  );
});

test("a column ALTER ships as a NEW governed migration and applies once", async () => {
  const db = memDb();
  // Start from the real schema…
  await migrateLedgerSchema(db, { enforceRls: false, appliedAt: APPLIED_AT });
  const base = await currentVersion(db);

  // …then evolve it with an appended migration (the governed ALTER path).
  const withAlter = [
    ...ledgerMigrations({ enforceRls: false }),
    { version: 25, name: "account_add_note", sql: "ALTER TABLE account ADD COLUMN note text" },
  ];
  const report = (await runMigrations(db, withAlter, { appliedAt: APPLIED_AT })) as {
    applied: { version: number }[];
  };
  assert.deepEqual(report.applied.map((a) => a.version), [25], "only the new migration runs");
  assert.equal(await currentVersion(db), 25);
  assert.ok(base < 25);

  // The new column exists and accepts writes.
  await db.query("INSERT INTO account (tenant_id, id, code, name, type, currency_code, note) VALUES ('t','a','1000','Cash','ASSET','USD','hi')");
  const r = await db.query("SELECT note FROM account WHERE id = 'a'");
  assert.equal(r.rows[0]?.["note"], "hi");
});
