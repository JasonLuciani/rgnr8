import { test } from "node:test";
import assert from "node:assert/strict";
import { USD, asTenantId, computeTrialBalance } from "@rgnr8/ledger-kernel";
import { currentVersion, runMigrations } from "@rgnr8/migrations";
import { LEDGER_MIGRATIONS, PgLedgerStore } from "../src/index.js";
import { engineWith, makePool, saleCommand, standardCoa, POST_AT } from "./support.js";

const AT = "2026-08-06T00:00:00Z";

test("the ledger schema builds via the versioned migration runner and posts", async () => {
  const pool = makePool();
  const report = await runMigrations(pool, LEDGER_MIGRATIONS, { appliedAt: AT });
  assert.deepEqual(report.applied.map((a) => a.name), ["ledger_core"]);
  assert.equal(await currentVersion(pool), 1);

  // the schema the migration created is a working ledger
  const store = new PgLedgerStore(pool);
  const engine = engineWith(store);
  await engine.post(saleCommand("acme", "1000.00", "s1"), { postedAt: POST_AT });
  const tb = await computeTrialBalance(store, asTenantId("acme"), standardCoa(), USD);
  assert.ok(tb.inBalance);
});

test("re-running the ledger migrations is idempotent", async () => {
  const pool = makePool();
  await runMigrations(pool, LEDGER_MIGRATIONS, { appliedAt: AT });
  const again = await runMigrations(pool, LEDGER_MIGRATIONS, { appliedAt: "2026-09-01T00:00:00Z" });
  assert.equal(again.applied.length, 0);
  assert.equal(again.skipped, 1);
});
