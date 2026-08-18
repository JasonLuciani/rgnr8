import { test, describe } from "node:test";
import assert from "node:assert/strict";
import pg from "pg";
import { LedgerService, PostgresBackend } from "../src/index.js";

/**
 * Real-PostgreSQL integration. `pg-mem` covers the SQL shape in the fast suite;
 * this proves the books are genuinely **durable** — written to a real database
 * and readable by a *second* service instance, which is what "the client's books
 * live in RGNR8" actually requires.
 *
 * Opt-in: set RGNR8_TEST_DATABASE_URL to a Postgres you don't mind being written
 * to. Skipped (never silently passed) when it isn't set.
 */
const URL_ = process.env["RGNR8_TEST_DATABASE_URL"] ?? "";
const NOW = "2026-08-31T00:00:00Z";

describe("real PostgreSQL", { skip: URL_ ? false : "set RGNR8_TEST_DATABASE_URL to run" }, () => {
  test("books are durable across service restarts and isolated per tenant", async () => {
    const suffix = `${Date.now()}`.slice(-8);
    const acme = `acme_${suffix}`;
    const bistro = `bistro_${suffix}`;

    // --- instance #1: set up two clients and post to each ---
    const poolA = new pg.Pool({ connectionString: URL_ });
    const backendA = new PostgresBackend(poolA as never);
    await backendA.migrate();
    const svcA = new LedgerService(backendA, { now: () => NOW });

    const callA = (method: string, path: string, body: unknown = ""): Promise<{ status: number; body: unknown }> =>
      svcA.handle({
        method, path, query: {},
        body: typeof body === "string" ? body : JSON.stringify(body),
        headers: {},
      });

    assert.equal((await callA("POST", `/t/${acme}/accounts/seed`, { category: "PROFESSIONAL_SERVICES" })).status, 201);
    assert.equal((await callA("POST", `/t/${bistro}/accounts/seed`, { category: "RESTAURANT" })).status, 201);
    assert.equal((await callA("POST", `/t/${acme}/entries`, {
      date: "2026-08-05", memo: "Consulting",
      lines: [
        { code: "1000", side: "DEBIT", amount_minor: "1500000" },
        { code: "4100", side: "CREDIT", amount_minor: "1500000" },
      ],
    })).status, 201);
    assert.equal((await callA("POST", `/t/${bistro}/entries`, {
      date: "2026-08-06", memo: "Dinner service",
      lines: [
        { code: "1000", side: "DEBIT", amount_minor: "67300" },
        { code: "4100", side: "CREDIT", amount_minor: "67300" },
      ],
    })).status, 201);
    await poolA.end(); // the "process" that wrote the books goes away

    // --- instance #2: a brand-new pool, backend, and service over the same DB ---
    const poolB = new pg.Pool({ connectionString: URL_ });
    const svcB = new LedgerService(new PostgresBackend(poolB as never), { now: () => NOW });
    const read = async (tenant: string): Promise<Record<string, unknown>> =>
      (await svcB.handle({
        method: "GET", path: `/t/${tenant}/trial-balance`, query: {}, body: "", headers: {},
      })).body as Record<string, unknown>;

    const cashOf = (tb: Record<string, unknown>): unknown =>
      (tb["rows"] as Array<Record<string, unknown>>).find((r) => r["code"] === "1000")?.["debit_minor"];

    const acmeTb = await read(acme);
    const bistroTb = await read(bistro);
    assert.equal(acmeTb["in_balance"], true, "acme's books survived the restart in balance");
    assert.equal(cashOf(acmeTb), "1500000");
    assert.equal(bistroTb["in_balance"], true);
    assert.equal(cashOf(bistroTb), "67300");

    // charts persisted, and each client kept its own industry accounts
    const accountsOf = async (tenant: string): Promise<string[]> => {
      const res = await svcB.handle({
        method: "GET", path: `/t/${tenant}/accounts`, query: {}, body: "", headers: {},
      });
      return ((res.body as Record<string, unknown>)["accounts"] as Array<Record<string, unknown>>)
        .map((a) => String(a["name"]));
    };
    const acmeNames = await accountsOf(acme);
    const bistroNames = await accountsOf(bistro);
    assert.ok(bistroNames.includes("Tips Payable"));
    assert.equal(acmeNames.includes("Tips Payable"), false);
    assert.ok(acmeNames.includes("Retainer Income"));

    await poolB.end();
  });
});
