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

  test("AR/AP documents are durable and tie to the control accounts", async () => {
    const tenant = `arap_${`${Date.now()}`.slice(-8)}`;

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

    await callA("POST", `/t/${tenant}/accounts/seed`, { category: "PROFESSIONAL_SERVICES" });
    await callA("POST", `/t/${tenant}/customers`, { id: "c1", name: "Northwind", terms_days: 30 });
    await callA("POST", `/t/${tenant}/vendors`, { id: "v1", name: "Copyshop", terms_days: 15 });

    assert.equal((await callA("POST", `/t/${tenant}/invoices`, {
      id: "INV-1", party_id: "c1", date: "2026-08-01", memo: "Consulting",
      lines: [{ description: "Work", quantity: 4, unit_amount_minor: "50000", account_code: "4100" }],
    })).status, 201);
    assert.equal((await callA("POST", `/t/${tenant}/bills`, {
      id: "B-1", party_id: "v1", date: "2026-08-02",
      lines: [{ unit_amount_minor: "36000", account_code: "6400" }],
    })).status, 201);
    // collect half the invoice
    assert.equal((await callA("POST", `/t/${tenant}/invoices/INV-1/payments`, {
      date: "2026-08-15", amount_minor: "100000",
    })).status, 201);
    await poolA.end();

    // --- a fresh service instance over the same database ---
    const poolB = new pg.Pool({ connectionString: URL_ });
    const svcB = new LedgerService(new PostgresBackend(poolB as never), { now: () => NOW });
    const get = async (path: string, query: Record<string, string> = {}): Promise<Record<string, unknown>> =>
      (await svcB.handle({ method: "GET", path, query, body: "", headers: {} }))
        .body as Record<string, unknown>;

    const invoices = (await get(`/t/${tenant}/invoices`))["documents"] as Array<Record<string, unknown>>;
    assert.equal(invoices.length, 1, "the invoice survived the restart");
    assert.equal(invoices[0]!["total_minor"], "200000");
    assert.equal(invoices[0]!["open_minor"], "100000");
    assert.equal(invoices[0]!["status"], "PARTIAL");
    assert.equal(invoices[0]!["due_date"], "2026-08-31");
    assert.equal((invoices[0]!["lines"] as unknown[]).length, 1);

    const bills = (await get(`/t/${tenant}/bills`))["documents"] as Array<Record<string, unknown>>;
    assert.equal(bills[0]!["open_minor"], "36000");

    // payments persisted against the document
    const detail = await get(`/t/${tenant}/invoices/INV-1`);
    assert.equal((detail["payments"] as unknown[]).length, 1);

    // and the CONTROL ACCOUNTS still tie to the open documents
    const tb = await get(`/t/${tenant}/trial-balance`);
    const signed = (code: string): bigint => {
      const r = (tb["rows"] as Array<Record<string, unknown>>).find((x) => x["code"] === code);
      return r ? BigInt(String(r["debit_minor"])) - BigInt(String(r["credit_minor"])) : 0n;
    };
    assert.equal(tb["in_balance"], true);
    assert.equal(signed("1200"), 100000n, "AR control == open receivables");
    assert.equal(-signed("2000"), 36000n, "AP control == open payables");

    // aging reads from the durable open items
    const aging = await get(`/t/${tenant}/aging/ar`, { as_of: "2026-08-31" });
    assert.equal(aging["grand_total_minor"], "100000");

    await poolB.end();
  });

  test("a finished reconciliation survives a restart and stays locked", async () => {
    const tenant = `recon_${`${Date.now()}`.slice(-8)}`;

    // --- instance #1: post three transactions, tick the two on the statement ---
    const poolA = new pg.Pool({ connectionString: URL_ });
    const backendA = new PostgresBackend(poolA as never);
    await backendA.migrate();
    const svcA = new LedgerService(backendA, { now: () => NOW });
    const callA = (
      method: string, path: string, body: unknown = "", query: Record<string, string> = {},
    ): Promise<{ status: number; body: unknown }> =>
      svcA.handle({
        method, path, query,
        body: typeof body === "string" ? body : JSON.stringify(body),
        headers: {},
      });

    await callA("POST", `/t/${tenant}/accounts/seed`, { category: "PROFESSIONAL_SERVICES" });
    const post = async (date: string, memo: string, minor: string): Promise<string> => {
      const into = !minor.startsWith("-");
      const abs = into ? minor : minor.slice(1);
      const r = await callA("POST", `/t/${tenant}/entries`, {
        date, memo,
        lines: into
          ? [
              { code: "1000", side: "DEBIT", amount_minor: abs },
              { code: "4100", side: "CREDIT", amount_minor: abs },
            ]
          : [
              { code: "6400", side: "DEBIT", amount_minor: abs },
              { code: "1000", side: "CREDIT", amount_minor: abs },
            ],
      });
      assert.equal(r.status, 201, JSON.stringify(r.body));
      const body = r.body as Record<string, unknown>;
      return String((body["entry"] as Record<string, unknown>)["id"]);
    };
    const deposit = await post("2026-08-03", "Client deposit", "500000");
    const software = await post("2026-08-05", "Software", "-20000");
    await post("2026-08-30", "Check not cashed", "-5000");

    const STMT = { statement_date: "2026-08-31", statement_balance_minor: "480000" };
    for (const entryId of [deposit, software]) {
      const r = await callA("POST", `/t/${tenant}/accounts/1000/reconcile/toggle`, {
        entry_id: entryId, cleared: true, ...STMT,
      });
      assert.equal(r.status, 200, JSON.stringify(r.body));
    }
    const fin = await callA("POST", `/t/${tenant}/accounts/1000/reconcile/finish`, STMT);
    assert.equal(fin.status, 200, JSON.stringify(fin.body));
    assert.equal((fin.body as Record<string, unknown>)["reconciled_entries"], 2);
    await poolA.end(); // the process that reconciled goes away

    // --- instance #2: the tick marks are still there ---
    const poolB = new pg.Pool({ connectionString: URL_ });
    const svcB = new LedgerService(new PostgresBackend(poolB as never), { now: () => NOW });
    const callB = (
      method: string, path: string, body: unknown = "", query: Record<string, string> = {},
    ): Promise<{ status: number; body: unknown }> =>
      svcB.handle({
        method, path, query,
        body: typeof body === "string" ? body : JSON.stringify(body),
        headers: {},
      });

    const view = (await callB("GET", `/t/${tenant}/accounts/1000/reconcile`, "", {
      statement_date: "2026-09-30", statement_balance_minor: "475000",
    })).body as Record<string, unknown>;
    assert.equal(view["reconciled_through"], "2026-08-31");
    assert.equal(view["reconciled_balance_minor"], "480000");
    const lines = view["lines"] as Array<Record<string, unknown>>;
    assert.equal(lines.filter((l) => l["status"] === "RECONCILED").length, 2);
    // the outstanding check is the only thing left to tick, and it is exactly
    // what September is short by
    assert.equal(view["cleared_this_session_minor"], "0");
    assert.equal(view["difference_minor"], "-5000");
    assert.equal(lines.filter((l) => l["status"] === "UNCLEARED").length, 1);

    // and a reconciled line cannot be un-ticked by the new instance either
    const undo = await callB("POST", `/t/${tenant}/accounts/1000/reconcile/toggle`, {
      entry_id: deposit, cleared: false,
      statement_date: "2026-09-30", statement_balance_minor: "475000",
    });
    assert.equal(undo.status, 400);
    assert.match(String((undo.body as Record<string, unknown>)["error"]), /locked in/);

    await poolB.end();
  });

  test("the feed inbox and payroll are durable across a restart", async () => {
    const tenant = `feedpay_${`${Date.now()}`.slice(-8)}`;

    // --- instance #1 ---
    const poolA = new pg.Pool({ connectionString: URL_ });
    const backendA = new PostgresBackend(poolA as never);
    await backendA.migrate();
    const svcA = new LedgerService(backendA, { now: () => NOW });
    const callA = (
      method: string, path: string, body: unknown = "", query: Record<string, string> = {},
    ): Promise<{ status: number; body: unknown }> =>
      svcA.handle({
        method, path, query,
        body: typeof body === "string" ? body : JSON.stringify(body),
        headers: {},
      });

    await callA("POST", `/t/${tenant}/accounts/seed`, { category: "PROFESSIONAL_SERVICES" });
    await callA("POST", `/t/${tenant}/feed-rules`, {
      id: "rent", account_code: "6300", description_contains: "RIVERSIDE",
    });
    await callA("POST", `/t/${tenant}/feed/1000`, {
      source: "plaid-like",
      transactions: [
        { id: "d-1", date: "2026-08-03", amount_minor: "500000", description: "DEPOSIT" },
        { id: "d-2", date: "2026-08-09", amount_minor: "-350000", description: "RIVERSIDE RENT" },
      ],
    });
    await callA("POST", `/t/${tenant}/feed/txn/d-1/accept`, { category_code: "4100" });

    await callA("POST", `/t/${tenant}/payroll/employees`, { id: "ada", name: "Ada Reyes" });
    await callA("POST", `/t/${tenant}/payroll/runs`, {
      id: "PR-1", date: "2026-08-15", employer_taxes_minor: "7650",
      lines: [{ employee_id: "ada", gross_minor: "100000", employee_taxes_minor: "22000" }],
    });
    await callA("POST", `/t/${tenant}/payroll/runs/PR-1/post`, {});
    await poolA.end();

    // --- instance #2: everything is still there ---
    const poolB = new pg.Pool({ connectionString: URL_ });
    const svcB = new LedgerService(new PostgresBackend(poolB as never), { now: () => NOW });
    const get = async (
      path: string, query: Record<string, string> = {},
    ): Promise<Record<string, unknown>> =>
      (await svcB.handle({ method: "GET", path, query, body: "", headers: {} }))
        .body as Record<string, unknown>;

    // the un-actioned line is still waiting, still carrying its rule suggestion
    const queue = await get(`/t/${tenant}/feed`);
    assert.equal(queue["pending"], 1);
    assert.equal(queue["posted"], 1);
    const item = (queue["items"] as Array<Record<string, unknown>>)[0]!;
    assert.equal(item["id"], "d-2");
    assert.equal((item["suggestion"] as Record<string, unknown>)["account_code"], "6300");
    assert.equal((item["suggestion"] as Record<string, unknown>)["source"], "rule");

    // the accepted line kept its link to the entry it produced
    const posted = await get(`/t/${tenant}/feed`, { status: "POSTED" });
    const done = (posted["items"] as Array<Record<string, unknown>>)[0]!;
    assert.equal(done["category_code"], "4100");
    assert.ok(String(done["entry_id"]).length > 0);

    // payroll survived, liabilities and all
    const runs = (await get(`/t/${tenant}/payroll/runs`))["runs"] as Array<Record<string, unknown>>;
    assert.equal(runs.length, 1);
    assert.equal(runs[0]!["status"], "POSTED");
    assert.equal((runs[0]!["totals"] as Record<string, unknown>)["total_cost_minor"], "107650");
    assert.equal((await get(`/t/${tenant}/payroll/liabilities`))["owed_minor"], "29650");

    // and a re-synced feed window still doesn't duplicate, from a fresh process
    const resync = await svcB.handle({
      method: "POST", path: `/t/${tenant}/feed/1000`, query: {},
      body: JSON.stringify({ transactions: [
        { id: "d-1", date: "2026-08-03", amount_minor: "500000", description: "DEPOSIT" },
      ] }),
      headers: {},
    });
    assert.equal((resync.body as Record<string, unknown>)["added"], 0);
    assert.equal((resync.body as Record<string, unknown>)["duplicates"], 1);

    await poolB.end();
  });
});
