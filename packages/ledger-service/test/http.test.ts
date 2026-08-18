import { test } from "node:test";
import assert from "node:assert/strict";
import type { AddressInfo } from "node:net";
import { newDb } from "pg-mem";
import {
  InMemoryBackend,
  LedgerService,
  PostgresBackend,
  createLedgerServer,
} from "../src/index.js";

const NOW = "2026-08-31T00:00:00Z";
const TOKEN = "http-secret";

/** Boot the real HTTP server on an ephemeral port and return a client + closer. */
async function boot(backend: InMemoryBackend | PostgresBackend): Promise<{
  call: (method: string, path: string, body?: unknown) => Promise<{ status: number; json: Record<string, unknown> }>;
  close: () => Promise<void>;
}> {
  await backend.migrate();
  const service = new LedgerService(backend, { authToken: TOKEN, now: () => NOW });
  const server = createLedgerServer(service);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as AddressInfo).port;
  const base = `http://127.0.0.1:${port}`;

  return {
    call: async (method, path, body) => {
      const res = await fetch(`${base}${path}`, {
        method,
        headers: { authorization: `Bearer ${TOKEN}`, "content-type": "application/json" },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      return { status: res.status, json: (await res.json()) as Record<string, unknown> };
    },
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

test("the service really serves over HTTP end to end", async () => {
  const { call, close } = await boot(new InMemoryBackend());
  try {
    await call("POST", "/t/acme/accounts/seed", { category: "SERVICE_GENERAL" });
    const posted = await call("POST", "/t/acme/entries", {
      date: "2026-08-05",
      memo: "Cash sale",
      lines: [
        { code: "1000", side: "DEBIT", amount_minor: "500000" },
        { code: "4000", side: "CREDIT", amount_minor: "500000" },
      ],
    });
    assert.equal(posted.status, 201);

    const tb = await call("GET", "/t/acme/trial-balance");
    assert.equal(tb.status, 200);
    assert.equal(tb.json["in_balance"], true);
    assert.equal(tb.json["total_debit_minor"], "500000");

    const reg = await call("GET", "/t/acme/accounts/1000/register");
    assert.equal(reg.json["closing_minor"], "500000");
  } finally {
    await close();
  }
});

test("HTTP rejects a request with no/!bad token", async () => {
  const backend = new InMemoryBackend();
  await backend.migrate();
  const service = new LedgerService(backend, { authToken: TOKEN, now: () => NOW });
  const server = createLedgerServer(service);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as AddressInfo).port;
  try {
    const anon = await fetch(`http://127.0.0.1:${port}/t/acme/accounts`);
    assert.equal(anon.status, 401);
    const health = await fetch(`http://127.0.0.1:${port}/health`);
    assert.equal(health.status, 200);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test("the books are durable on the PostgreSQL backend, and tenant-isolated", async () => {
  const db = newDb();
  const pg = db.adapters.createPg();
  const pool = new pg.Pool();
  const { call, close } = await boot(new PostgresBackend(pool as never));
  try {
    await call("POST", "/t/acme/accounts/seed", { category: "SERVICE_GENERAL" });
    await call("POST", "/t/beta/accounts/seed", { category: "RETAIL" });

    await call("POST", "/t/acme/entries", {
      date: "2026-08-05",
      memo: "Acme sale",
      lines: [
        { code: "1000", side: "DEBIT", amount_minor: "500000" },
        { code: "4000", side: "CREDIT", amount_minor: "500000" },
      ],
    });
    await call("POST", "/t/beta/entries", {
      date: "2026-08-06",
      memo: "Beta sale",
      lines: [
        { code: "1000", side: "DEBIT", amount_minor: "111100" },
        { code: "4100", side: "CREDIT", amount_minor: "111100" },
      ],
    });

    // each tenant sees only its own books, read back out of SQL
    const acme = await call("GET", "/t/acme/trial-balance");
    const beta = await call("GET", "/t/beta/trial-balance");
    const cash = (tb: Record<string, unknown>): unknown =>
      (tb["rows"] as Array<Record<string, unknown>>).find((r) => r["code"] === "1000")!["debit_minor"];
    assert.equal(cash(acme.json), "500000");
    assert.equal(cash(beta.json), "111100");
    assert.equal(acme.json["in_balance"], true);
    assert.equal(beta.json["in_balance"], true);

    // the chart persisted to SQL too (retail has inventory; general services doesn't)
    const betaAccounts = (await call("GET", "/t/beta/accounts")).json["accounts"] as Array<Record<string, unknown>>;
    assert.ok(betaAccounts.some((a) => a["subtype"] === "INVENTORY"));
    const acmeAccounts = (await call("GET", "/t/acme/accounts")).json["accounts"] as Array<Record<string, unknown>>;
    assert.equal(acmeAccounts.some((a) => a["subtype"] === "INVENTORY"), false);

    const acmeEntries = (await call("GET", "/t/acme/entries")).json["entries"] as unknown[];
    assert.equal(acmeEntries.length, 1);
  } finally {
    await close();
  }
});
