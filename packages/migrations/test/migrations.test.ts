import { test } from "node:test";
import assert from "node:assert/strict";
import { newDb } from "pg-mem";
import {
  MigrationDriftError,
  MigrationError,
  currentVersion,
  runMigrations,
  type Migration,
  type SqlExecutor,
} from "../src/index.js";

function makeDb(): SqlExecutor {
  const db = newDb();
  const pg = db.adapters.createPg();
  return new pg.Pool() as unknown as SqlExecutor;
}

const AT = "2026-08-06T00:00:00Z";

const M1: Migration = {
  version: 1,
  name: "create_widgets",
  sql: "CREATE TABLE widgets (id text PRIMARY KEY, name text NOT NULL);",
};
const M2: Migration = {
  version: 2,
  name: "add_widget_price",
  sql: "ALTER TABLE widgets ADD COLUMN price numeric(38,0) NOT NULL DEFAULT 0;",
};

test("applies pending migrations in order and records them", async () => {
  const db = makeDb();
  const report = await runMigrations(db, [M2, M1], { appliedAt: AT }); // deliberately out of order
  assert.deepEqual(report.applied.map((a) => a.version), [1, 2]); // sorted
  assert.equal(report.currentVersion, 2);
  assert.equal(report.skipped, 0);

  // the widgets table exists with both columns
  await db.query("INSERT INTO widgets (id, name, price) VALUES ('w1', 'A', 10);");
  const rows = (await db.query("SELECT id, price FROM widgets")).rows;
  assert.equal(rows.length, 1);
});

test("re-running is idempotent — everything is skipped, nothing re-applied", async () => {
  const db = makeDb();
  await runMigrations(db, [M1, M2], { appliedAt: AT });
  const again = await runMigrations(db, [M1, M2], { appliedAt: "2026-09-01T00:00:00Z" });
  assert.equal(again.applied.length, 0);
  assert.equal(again.skipped, 2);
  // applied_at from the first run is preserved (not overwritten)
  const row = (await db.query("SELECT applied_at FROM schema_migrations WHERE version = 1")).rows[0];
  assert.equal(row?.["applied_at"], AT);
});

test("only newly-added migrations apply on a later run", async () => {
  const db = makeDb();
  await runMigrations(db, [M1], { appliedAt: AT });
  const report = await runMigrations(db, [M1, M2], { appliedAt: AT });
  assert.deepEqual(report.applied.map((a) => a.version), [2]);
  assert.equal(report.skipped, 1);
});

test("editing an already-applied migration is caught as drift", async () => {
  const db = makeDb();
  await runMigrations(db, [M1], { appliedAt: AT });
  const tampered: Migration = { ...M1, sql: M1.sql + " -- sneaky edit" };
  await assert.rejects(
    () => runMigrations(db, [tampered], { appliedAt: AT }),
    (e) => e instanceof MigrationDriftError,
  );
});

test("duplicate versions are rejected before anything runs", async () => {
  const db = makeDb();
  await assert.rejects(
    () => runMigrations(db, [M1, { ...M2, version: 1 }], { appliedAt: AT }),
    (e) => e instanceof MigrationError,
  );
});

test("a failing migration rolls back and does not record a version", async () => {
  const db = makeDb();
  const bad: Migration = { version: 1, name: "bad", sql: "CREATE TABLE ( invalid sql" };
  await assert.rejects(() => runMigrations(db, [bad], { appliedAt: AT }));
  assert.equal(await currentVersion(db), 0); // nothing recorded
});

test("currentVersion reports the highest applied version", async () => {
  const db = makeDb();
  assert.equal(await currentVersion(db), 0);
  await runMigrations(db, [M1, M2], { appliedAt: AT });
  assert.equal(await currentVersion(db), 2);
});
