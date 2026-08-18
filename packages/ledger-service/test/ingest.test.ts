import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/** The feed → ledger path: bank/QBO activity becoming real journal entries. */

const NOW = "2026-08-31T00:00:00Z";

function svc(): LedgerService {
  return new LedgerService(new InMemoryBackend(), { now: () => NOW });
}
const call = (s: LedgerService, method: string, path: string, body: unknown = "", query: Record<string, string> = {}): Promise<ServiceResponse> =>
  s.handle({ method, path, query, body: typeof body === "string" ? body : JSON.stringify(body), headers: {} });
const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;

const FEED = [
  { id: "t1", date: "2026-08-03", amount_minor: "500000", description: "Client payment", kind: "DEPOSIT" },
  { id: "t2", date: "2026-08-05", amount_minor: "-8999", description: "Figma subscription", counterparty: "Figma" },
  { id: "t3", date: "2026-08-06", amount_minor: "-12000", description: "Bank fee", kind: "FEE" },
];

async function seeded(): Promise<LedgerService> {
  const s = svc();
  await call(s, "POST", "/t/acme/accounts/seed", { category: "SERVICE_GENERAL" });
  return s;
}

test("a bank feed posts into the ledger and keeps it in balance", async () => {
  const s = await seeded();
  const r = await call(s, "POST", "/t/acme/ingest", { source: "plaid", transactions: FEED });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.equal(obj(r)["posted"], 3);

  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  assert.equal(tb["in_balance"], true);
  const rows = tb["rows"] as Array<Record<string, unknown>>;
  const cash = rows.find((x) => x["code"] === "1000")!;
  // 5000.00 in − 89.99 − 120.00 = 4790.01
  assert.equal(cash["debit_minor"], "479001");
  assert.ok(rows.some((x) => x["code"] === "4000")); // income
  assert.ok(rows.some((x) => x["code"] === "6050")); // bank fee
});

test("re-syncing an overlapping window never double-posts", async () => {
  const s = await seeded();
  await call(s, "POST", "/t/acme/ingest", { source: "plaid", transactions: FEED });
  const again = obj(await call(s, "POST", "/t/acme/ingest", {
    source: "plaid",
    transactions: [...FEED, { id: "t4", date: "2026-08-09", amount_minor: "-4500", description: "Coffee" }],
  }));
  assert.equal(again["already_posted"], 3);
  assert.equal(again["posted"], 1); // only the genuinely new one
  const entries = obj(await call(s, "GET", "/t/acme/entries"))["entries"] as unknown[];
  assert.equal(entries.length, 4);
});

test("categorization rules code a feed line to a specific account", async () => {
  const s = await seeded();
  await call(s, "POST", "/t/acme/accounts", { code: "6500", name: "Software & Subscriptions", subtype: "EXPENSE" });
  await call(s, "POST", "/t/acme/ingest", {
    source: "plaid",
    transactions: FEED,
    rules: [{ id: "figma", priority: 10, description_contains: "Figma", account_code: "6500" }],
  });
  const reg = obj(await call(s, "GET", "/t/acme/accounts/6500/register"));
  assert.equal(reg["closing_minor"], "8999"); // the Figma charge landed here, not the catch-all
});

test("internal transfers are skipped, not booked as income or expense", async () => {
  const s = await seeded();
  const r = obj(await call(s, "POST", "/t/acme/ingest", {
    transactions: [
      { id: "x1", date: "2026-08-03", amount_minor: "-100000", description: "Move to savings", is_transfer: true },
      { id: "x2", date: "2026-08-03", amount_minor: "250000", description: "Client payment" },
    ],
  }));
  assert.equal(r["skipped_transfers"], 1);
  assert.equal(r["posted"], 1);
});

test("feed lines dated into a closed month are deferred, not force-posted", async () => {
  const s = await seeded();
  await call(s, "POST", "/t/acme/periods/2026-08/lock", {});
  const r = obj(await call(s, "POST", "/t/acme/ingest", {
    transactions: [
      { id: "late1", date: "2026-08-15", amount_minor: "100000", description: "Late arrival" },
      { id: "ok1", date: "2026-09-02", amount_minor: "100000", description: "Open month" },
    ],
  }));
  assert.equal(r["blocked_by_closed_period"], 1);
  assert.equal(r["posted"], 1);
});

test("ingest validates its input instead of writing junk", async () => {
  const s = await seeded();
  assert.equal((await call(s, "POST", "/t/acme/ingest", { transactions: [] })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/ingest", {
    transactions: [{ id: "b1", date: "Aug 5", amount_minor: "100" }],
  })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/ingest", {
    transactions: [{ id: "b2", date: "2026-08-05", amount_minor: "not-a-number" }],
  })).status, 400);
  // an account-map override pointing at a code that doesn't exist
  assert.equal((await call(s, "POST", "/t/acme/ingest", {
    accounts: { cash: "9999" },
    transactions: [{ id: "b3", date: "2026-08-05", amount_minor: "100" }],
  })).status, 400);
});

test("ingest is tenant-scoped", async () => {
  const s = await seeded();
  await call(s, "POST", "/t/beta/accounts/seed", { category: "RETAIL" });
  await call(s, "POST", "/t/acme/ingest", { transactions: [FEED[0]!] });
  const betaEntries = obj(await call(s, "GET", "/t/beta/entries"))["entries"] as unknown[];
  assert.equal(betaEntries.length, 0);
});
