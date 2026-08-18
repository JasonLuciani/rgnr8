import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Recurring transactions.
 *
 * The design decision under test is that nothing posts unattended. A rule that
 * fires on its own keeps paying rent after the lease ends and keeps invoicing a
 * client who cancelled, silently, for months. So a recurring transaction is a
 * memorized template plus a schedule, and running it is something a person does
 * having seen the list.
 *
 * The other property that matters is that running twice cannot duplicate rent.
 */

const NOW = "2026-09-01T00:00:00Z";

const call = (
  s: LedgerService, method: string, path: string,
  body: unknown = "", query: Record<string, string> = {},
): Promise<ServiceResponse> =>
  s.handle({
    method, path, query,
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: {},
  });

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;
type Row = Record<string, unknown>;

async function ready(): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  return s;
}

const RENT = {
  id: "rent",
  name: "Studio rent",
  frequency: "MONTHLY",
  interval: 1,
  start_date: "2026-06-01",
  memo: "Riverside Properties",
  lines: [
    { account_code: "6300", side: "DEBIT", amount_minor: "350000" },
    { account_code: "1000", side: "CREDIT", amount_minor: "350000" },
  ],
};

const define = (s: LedgerService, extra: Record<string, unknown> = {}) =>
  call(s, "POST", "/t/acme/recurring", { ...RENT, ...extra });

const due = (s: LedgerService, asOf: string) =>
  call(s, "GET", "/t/acme/recurring/due", "", { as_of: asOf });

const run = (s: LedgerService, asOf: string, id?: string) =>
  call(s, "POST", "/t/acme/recurring/run", { as_of: asOf, ...(id ? { id } : {}) });

async function balance(s: LedgerService, code: string): Promise<bigint> {
  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  const row = (tb["rows"] as Row[]).find((r) => r["code"] === code);
  if (!row) return 0n;
  return BigInt(String(row["debit_minor"])) - BigInt(String(row["credit_minor"]));
}

// --- defining ----------------------------------------------------------------

test("a template is stored with its schedule and lines", async () => {
  const s = await ready();
  const r = await define(s);
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const t = obj(r)["recurring"] as Row;
  assert.equal(t["name"], "Studio rent");
  assert.equal(t["frequency"], "MONTHLY");
  assert.equal((t["lines"] as Row[]).length, 2);
  assert.equal(t["active"], true);
});

test("a template that can never balance is refused when it's written", async () => {
  const s = await ready();
  const r = await define(s, {
    lines: [
      { account_code: "6300", side: "DEBIT", amount_minor: "350000" },
      { account_code: "1000", side: "CREDIT", amount_minor: "340000" },
    ],
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /doesn't balance/);
});

test("a nonsense schedule or line is refused", async () => {
  const s = await ready();
  assert.equal((await define(s, { frequency: "FORTNIGHTLY" })).status, 400);
  assert.equal((await define(s, { interval: 0 })).status, 400);
  assert.equal((await define(s, { start_date: "June" })).status, 400);
  assert.equal((await define(s, { end_date: "2026-01-01" })).status, 400);
  assert.equal((await define(s, { name: "" })).status, 400);
  assert.equal((await define(s, { lines: [RENT.lines[0]] })).status, 400);
  assert.equal((await define(s, {
    lines: [
      { account_code: "9999", side: "DEBIT", amount_minor: "1" },
      { account_code: "1000", side: "CREDIT", amount_minor: "1" },
    ],
  })).status, 400);
  assert.equal((await define(s, {
    lines: [
      { account_code: "6300", side: "DEBIT", amount_minor: "-1" },
      { account_code: "1000", side: "CREDIT", amount_minor: "-1" },
    ],
  })).status, 400);
});

// --- what is due -------------------------------------------------------------

test("what's due is listed rather than posted behind your back", async () => {
  const s = await ready();
  await define(s);
  const r = await due(s, "2026-08-31");
  assert.equal(r.status, 200);
  const dates = (obj(r)["due"] as Row[]).map((d) => d["date"]);
  assert.deepEqual(dates, ["2026-06-01", "2026-07-01", "2026-08-01"]);
  // and the ledger is still empty
  assert.equal((obj(await call(s, "GET", "/t/acme/entries"))["entries"] as Row[]).length, 0);
});

test("an inactive template is not due", async () => {
  const s = await ready();
  await define(s, { active: false });
  assert.equal((obj(await due(s, "2026-08-31"))["due"] as Row[]).length, 0);
});

test("an ended schedule stops being due", async () => {
  const s = await ready();
  await define(s, { end_date: "2026-07-15" });
  const dates = (obj(await due(s, "2026-12-31"))["due"] as Row[]).map((d) => d["date"]);
  assert.deepEqual(dates, ["2026-06-01", "2026-07-01"]);
});

test("an interval means every N periods", async () => {
  const s = await ready();
  await define(s, { id: "quarterly", frequency: "MONTHLY", interval: 3 });
  const dates = (obj(await due(s, "2026-12-31"))["due"] as Row[]).map((d) => d["date"]);
  assert.deepEqual(dates, ["2026-06-01", "2026-09-01", "2026-12-01"]);
});

// --- running -----------------------------------------------------------------

test("running posts what's due and nothing else", async () => {
  const s = await ready();
  await define(s);
  const r = await run(s, "2026-08-31");
  assert.equal(r.status, 200);
  assert.equal(obj(r)["posted"], 3);
  assert.equal(await balance(s, "6300"), 1050000n, "three months of rent");
  assert.equal(await balance(s, "1000"), -1050000n);
  assert.equal(obj(await call(s, "GET", "/t/acme/trial-balance"))["in_balance"], true);
  // nothing is due any more
  assert.equal((obj(await due(s, "2026-08-31"))["due"] as Row[]).length, 0);
});

test("running twice cannot duplicate rent", async () => {
  const s = await ready();
  await define(s);
  await run(s, "2026-08-31");
  const again = await run(s, "2026-08-31");
  assert.equal(obj(again)["posted"], 0);
  assert.equal(await balance(s, "6300"), 1050000n, "still three months, not six");
  assert.equal((obj(await call(s, "GET", "/t/acme/entries"))["entries"] as Row[]).length, 3);
});

test("running can be limited to one template", async () => {
  const s = await ready();
  await define(s);
  await define(s, {
    id: "software", name: "Design software", start_date: "2026-08-01",
    lines: [
      { account_code: "6500", side: "DEBIT", amount_minor: "24900" },
      { account_code: "1000", side: "CREDIT", amount_minor: "24900" },
    ],
  });
  const r = await run(s, "2026-08-31", "software");
  assert.equal(obj(r)["posted"], 1);
  assert.equal(await balance(s, "6500"), 24900n);
  assert.equal(await balance(s, "6300"), 0n, "the rent was left alone");
});

test("one failure does not sink the rest of the run", async () => {
  const s = await ready();
  await define(s);
  await call(s, "POST", "/t/acme/periods/2026-07/lock", {});
  const r = await run(s, "2026-08-31");
  assert.equal(obj(r)["posted"], 2, "June and August went through");
  assert.equal(obj(r)["skipped"], 1);
  const failures = obj(r)["failures"] as Row[];
  assert.equal(failures[0]!["date"], "2026-07-01");
  assert.match(String(failures[0]!["error"]), /2026-07/);
  // and July is still offered, so it isn't quietly lost
  const dates = (obj(await due(s, "2026-08-31"))["due"] as Row[]).map((d) => d["date"]);
  assert.deepEqual(dates, ["2026-07-01"]);
});

test("a template carrying a class posts it, and a bad one is refused", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/dimensions", {
    key: "class", label: "Line of business", values: ["Studio", "Onsite"],
  });
  await define(s, {
    lines: [
      { account_code: "6300", side: "DEBIT", amount_minor: "350000",
        dimensions: { class: "Studio" } },
      { account_code: "1000", side: "CREDIT", amount_minor: "350000" },
    ],
  });
  await run(s, "2026-06-30");
  const entries = obj(await call(s, "GET", "/t/acme/entries"))["entries"] as Row[];
  assert.deepEqual((entries[0]!["lines"] as Row[])[0]!["dimensions"], { class: "Studio" });

  await define(s, {
    id: "typo",
    lines: [
      { account_code: "6300", side: "DEBIT", amount_minor: "1000",
        dimensions: { class: "Studeo" } },
      { account_code: "1000", side: "CREDIT", amount_minor: "1000" },
    ],
  });
  const r = await run(s, "2026-06-30", "typo");
  assert.equal(obj(r)["posted"], 0);
  assert.match(String((obj(r)["failures"] as Row[])[0]!["error"]), /not allowed/);
});

test("a template can be removed and stops being due", async () => {
  const s = await ready();
  await define(s);
  assert.equal((await call(s, "DELETE", "/t/acme/recurring/rent")).status, 200);
  assert.equal((obj(await due(s, "2026-08-31"))["due"] as Row[]).length, 0);
  assert.equal((obj(await call(s, "GET", "/t/acme/recurring"))["recurring"] as Row[]).length, 0);
});

test("one tenant's recurring transactions are invisible to another", async () => {
  const s = await ready();
  await call(s, "POST", "/t/beta/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  await define(s);
  assert.equal(
    (obj(await call(s, "GET", "/t/beta/recurring"))["recurring"] as Row[]).length, 0,
  );
  assert.equal((obj(await call(s, "GET", "/t/beta/recurring/due", "", {
    as_of: "2026-08-31",
  }))["due"] as Row[]).length, 0);
});
