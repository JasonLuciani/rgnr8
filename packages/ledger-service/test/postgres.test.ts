import { test, describe } from "node:test";
import assert from "node:assert/strict";
import pg from "pg";
import {
  LedgerService, PostgresBackend, TENANT_TABLES, appRoleDdl, superuserWarning,
} from "../src/index.js";

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

    // A TAXED invoice reads back with its tax intact — a column added to the
    // table but forgotten in one SELECT reads as "no tax was charged", which
    // is exactly the kind of quiet wrongness this whole codebase is against.
    await svcB.handle({
      method: "POST", path: `/t/${tenant}/invoices`, query: {},
      body: JSON.stringify({
        id: "INV-TAX", party_id: "c1", date: "2026-08-02",
        tax_rate_ppm: 82_500,
        lines: [{ unit_amount_minor: "100000", account_code: "4100" }],
      }),
      headers: {},
    });
    const taxed = (await get(`/t/${tenant}/invoices/INV-TAX`))["document"] as Record<string, unknown>;
    assert.equal(taxed["net_minor"], "100000");
    assert.equal(taxed["tax_minor"], "8250");
    assert.equal(taxed["tax_rate_ppm"], 82500);
    assert.equal(taxed["total_minor"], "108250");
    const listed = ((await get(`/t/${tenant}/invoices`))["documents"] as Array<Record<string, unknown>>)
      .find((d) => d["id"] === "INV-TAX")!;
    assert.equal(listed["tax_minor"], "8250", "the list must agree with the detail");

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

  test("ROW-LEVEL SECURITY: the database refuses a query that forgets its tenant", async () => {
    const suffix = `${Date.now()}`.slice(-8);
    const alpha = `rls_alpha_${suffix}`;
    const bravo = `rls_bravo_${suffix}`;
    const role = `rgnr8_rls_${suffix}`;
    const password = `pw-${suffix}`;

    const ownerPool = new pg.Pool({ connectionString: URL_ });
    const backend = new PostgresBackend(ownerPool as never);
    await backend.migrate();
    const svc = new LedgerService(backend, { now: () => NOW });
    const call = (
      method: string, path: string, body: unknown = "",
    ): Promise<{ status: number; body: unknown }> =>
      svc.handle({
        method, path, query: {},
        body: typeof body === "string" ? body : JSON.stringify(body),
        headers: {},
      });

    for (const t of [alpha, bravo]) {
      await call("POST", `/t/${t}/accounts/seed`, { category: "PROFESSIONAL_SERVICES" });
      await call("POST", `/t/${t}/entries`, {
        date: "2026-08-05", memo: `${t} sale`,
        lines: [
          { code: "1000", side: "DEBIT", amount_minor: "111100" },
          { code: "4100", side: "CREDIT", amount_minor: "111100" },
        ],
      });
    }

    // Policies are FORCEd, so even the table's owner is subject to them.
    const forced = await ownerPool.query(
      `SELECT relname FROM pg_class WHERE relname = ANY($1)
         AND relrowsecurity AND relforcerowsecurity`,
      [[...TENANT_TABLES]],
    );
    assert.equal(
      forced.rows.length, TENANT_TABLES.length,
      "every tenant table must have RLS enabled AND forced",
    );

    await ownerPool.query(appRoleDdl(role, password));
    const url = new URL(URL_);
    url.username = role;
    url.password = password;
    const appPool = new pg.Pool({ connectionString: url.toString() });

    try {
      const client = await appPool.connect();
      try {
        // A raw query with NO tenant filter at all — exactly what a future bug
        // looks like. It must come back empty, not with everyone's books.
        const naked = await client.query("SELECT * FROM journal_entry");
        assert.equal(
          naked.rows.length, 0,
          "with no app.tenant_id bound, a filterless query must return nothing",
        );

        // Bind one tenant and the same filterless query sees only that tenant.
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.tenant_id', $1, true)", [alpha]);
        const scoped = await client.query("SELECT tenant_id FROM journal_entry");
        assert.ok(scoped.rows.length > 0, "alpha can see its own entries");
        assert.ok(
          scoped.rows.every((r) => String(r["tenant_id"]) === alpha),
          "bravo's rows are invisible even with no WHERE clause",
        );

        // And writing INTO another tenant is rejected outright by WITH CHECK.
        await assert.rejects(
          client.query(
            `INSERT INTO feed_txn (tenant_id, id, account_code, txn_date, amount_minor)
             VALUES ($1, 'smuggled', '1000', '2026-08-05', '100')`,
            [bravo],
          ),
          /row-level security/i,
        );
        await client.query("ROLLBACK");
      } finally {
        client.release();
      }
    } finally {
      await appPool.end();
      await ownerPool.query(`DROP OWNED BY "${role}"`).catch(() => undefined);
      await ownerPool.query(`DROP ROLE IF EXISTS "${role}"`).catch(() => undefined);
      await ownerPool.end();
    }
  });

  test("a SUPERUSER connection bypasses RLS entirely — which is why the app role is not optional", async () => {
    // This is the uncomfortable half of the story and it belongs in a test
    // rather than a comment: Postgres exempts superusers from every policy, so
    // RLS protects nothing if the service connects as one. `superuserWarning`
    // is what stops that configuration going unnoticed in production.
    const suffix = `${Date.now()}`.slice(-8);
    const tenant = `rls_super_${suffix}`;
    const pool = new pg.Pool({ connectionString: URL_ });
    const backend = new PostgresBackend(pool as never);
    await backend.migrate();
    const svc = new LedgerService(backend, { now: () => NOW });
    await svc.handle({
      method: "POST", path: `/t/${tenant}/accounts/seed`, query: {},
      body: JSON.stringify({ category: "PROFESSIONAL_SERVICES" }), headers: {},
    });

    const warning = await superuserWarning(pool as never);
    if (warning) {
      // The test database is a superuser connection, so prove the bypass is real.
      const naked = await pool.query("SELECT tenant_id FROM account LIMIT 1");
      assert.ok(naked.rows.length > 0, "a superuser sees rows with no tenant bound");
      assert.match(warning, /superuser|BYPASSRLS/i);
    }
    await pool.end();
  });

  test("the app role can change rows but not the rules that constrain it", async () => {
    const suffix = `${Date.now()}`.slice(-8);
    const tenant = `rls_role_${suffix}`;
    const role = `rgnr8_app_${suffix}`;
    const password = `pw-${suffix}`;

    const ownerPool = new pg.Pool({ connectionString: URL_ });
    const backend = new PostgresBackend(ownerPool as never);
    await backend.migrate();
    const svc = new LedgerService(backend, { now: () => NOW });
    await svc.handle({
      method: "POST", path: `/t/${tenant}/accounts/seed`, query: {},
      body: JSON.stringify({ category: "PROFESSIONAL_SERVICES" }), headers: {},
    });
    await ownerPool.query(appRoleDdl(role, password));

    // Connect AS the app role — the way production should.
    const url = new URL(URL_);
    url.username = role;
    url.password = password;
    const appPool = new pg.Pool({ connectionString: url.toString() });
    try {
      const client = await appPool.connect();
      try {
        // it can read its own tenant's rows
        await client.query("BEGIN");
        await client.query("SELECT set_config('app.tenant_id', $1, true)", [tenant]);
        const rows = await client.query("SELECT code FROM account");
        assert.ok(rows.rows.length > 0);
        await client.query("COMMIT");

        // but it cannot drop the policy that constrains it, or reshape the table
        await assert.rejects(
          client.query("DROP POLICY journal_entry_tenant_isolation ON journal_entry"),
          /must be owner|permission denied/i,
        );
        await assert.rejects(
          client.query("ALTER TABLE journal_entry DISABLE ROW LEVEL SECURITY"),
          /must be owner|permission denied/i,
        );
        await assert.rejects(
          client.query("CREATE TABLE rls_escape_hatch (x int)"),
          /permission denied/i,
        );
      } finally {
        client.release();
      }
    } finally {
      await appPool.end();
      await ownerPool.query(`DROP OWNED BY "${role}"`).catch(() => undefined);
      await ownerPool.query(`DROP ROLE IF EXISTS "${role}"`).catch(() => undefined);
      await ownerPool.end();
    }
  });

  test("PARITY: the SQL trial-balance pushdown equals summing the journal in memory", async () => {
    // The Pg store aggregates net-by-account in SQL; that MUST equal what summing
    // every posted line in the window would give. Post a realistic mix, then
    // compare the pushdown to a hand sum of list().
    const tenant = `parity_${`${Date.now()}`.slice(-8)}`;
    const pool = new pg.Pool({ connectionString: URL_ });
    try {
      const backend = new PostgresBackend(pool as never);
      await backend.migrate();
      const svc = new LedgerService(backend, { now: () => NOW });
      const call = (method: string, path: string, body: unknown = "", query: Record<string, string> = {}) =>
        svc.handle({ method, path, query, body: typeof body === "string" ? body : JSON.stringify(body), headers: {} });

      await call("POST", `/t/${tenant}/accounts/seed`, { category: "PROFESSIONAL_SERVICES" });
      for (const [date, a, b, amt] of [
        ["2026-06-05", "1000", "4000", "500000"],
        ["2026-06-20", "6300", "1000", "120000"],
        ["2026-07-02", "1000", "4000", "300000"],
        ["2026-07-15", "6300", "1000", "90000"],
      ] as const) {
        await call("POST", `/t/${tenant}/entries`, {
          date, memo: "x",
          lines: [{ code: a, side: "DEBIT", amount_minor: amt }, { code: b, side: "CREDIT", amount_minor: amt }],
        });
      }

      // hand-sum net-by-account from list(), windowed to June, as the reference
      const store = backend.store(tenant as never);
      const window = { from: "2026-06-01", to: "2026-06-30" };
      const ref = new Map<string, bigint>();
      for (const e of await store.list(tenant as never)) {
        if (e.entryDate < window.from || e.entryDate > window.to) continue;
        for (const l of e.lines) {
          const d = l.side === "DEBIT" ? l.amount.minorUnits : -l.amount.minorUnits;
          ref.set(String(l.accountId), (ref.get(String(l.accountId)) ?? 0n) + d);
        }
      }
      // the SQL pushdown for the same window
      const pushed = await store.netByAccount!(tenant as never, window);
      assert.equal(pushed.size, ref.size, "same set of accounts");
      for (const [id, net] of ref) {
        assert.equal(pushed.get(id as never), net, `account ${id} nets the same both ways`);
      }
    } finally {
      await pool.end();
    }
  });

  test("CONCURRENCY: two deploys migrating at once are serialized, both succeed", async () => {
    // Two service instances booting against the same database call migrate()
    // simultaneously. The advisory lock must serialize them so neither errors on
    // concurrent DDL, and the schema is usable afterward.
    const poolA = new pg.Pool({ connectionString: URL_ });
    const poolB = new pg.Pool({ connectionString: URL_ });
    try {
      const a = new PostgresBackend(poolA as never);
      const b = new PostgresBackend(poolB as never);
      const results = await Promise.allSettled([a.migrate(), b.migrate()]);
      for (const r of results) {
        assert.equal(r.status, "fulfilled", r.status === "rejected" ? String(r.reason) : "");
      }
      // and the schema works: a service can seed and read a chart
      const tenant = `mig_${`${Date.now()}`.slice(-8)}`;
      const svc = new LedgerService(a, { now: () => NOW });
      const seeded = await svc.handle({
        method: "POST", path: `/t/${tenant}/accounts/seed`,
        query: {}, body: JSON.stringify({ category: "PROFESSIONAL_SERVICES" }), headers: {},
      });
      assert.equal(seeded.status, 201, JSON.stringify(seeded.body));
    } finally {
      await poolA.end();
      await poolB.end();
    }
  });

  test("CONCURRENCY: parallel CRM events for one tenant get distinct, contiguous sequences", async () => {
    // The feed's sequence is COALESCE(MAX,0)+1. Without serialization, concurrent
    // appends read the same MAX and collide on the primary key — one throws and its
    // event is lost. A transaction-scoped advisory lock must make this race clean.
    const tenant = `race_${`${Date.now()}`.slice(-8)}`;
    const pool = new pg.Pool({ connectionString: URL_ });
    const backend = new PostgresBackend(pool as never);
    await backend.migrate();
    const svc = new LedgerService(backend, { now: () => NOW });
    const call = (
      method: string, path: string, body: unknown = "",
    ): Promise<{ status: number; body: unknown }> =>
      svc.handle({
        method, path, query: {},
        body: typeof body === "string" ? body : JSON.stringify(body),
        headers: {},
      });
    try {
      await call("POST", `/t/${tenant}/accounts/seed`, { category: "PROFESSIONAL_SERVICES" });

      // 25 leads created at once: 25 append-event writes racing on the same tenant.
      const N = 25;
      const results = await Promise.all(
        Array.from({ length: N }, (_v, i) =>
          call("POST", `/t/${tenant}/leads`, {
            id: `L-${i}`, name: `Lead ${i}`, source: "Race",
          })),
      );
      for (const r of results) assert.equal(r.status, 201, JSON.stringify(r.body));

      const feed = (await svc.handle({
        method: "GET", path: `/t/${tenant}/events`, query: { limit: "1000" },
        body: "", headers: {},
      })).body as Record<string, unknown>;
      const seqs = (feed["events"] as Array<Record<string, unknown>>).map((e) => Number(e["sequence"]));
      assert.equal(seqs.length, N, "every event survived — none lost to a PK collision");
      assert.equal(new Set(seqs).size, N, "every sequence is distinct");
      assert.deepEqual(
        [...seqs].sort((a, b) => a - b),
        Array.from({ length: N }, (_v, i) => i + 1),
        "sequences are 1..N with no gaps",
      );
    } finally {
      await pool.end();
    }
  });
});
