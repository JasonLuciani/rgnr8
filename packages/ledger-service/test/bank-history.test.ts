import { test } from "node:test";
import assert from "node:assert/strict";
import { newDb } from "pg-mem";
import { InMemoryBackend, LedgerService, PostgresBackend, type LedgerBackend, type ServiceResponse } from "../src/index.js";

/**
 * QuickBooks' bank and card history, kept line by line beside a mirror whose
 * books hold only totals. It posts nothing; each import replaces one window.
 */

const NOW = "2026-10-29T00:00:00Z";

const call = (
  s: LedgerService, method: string, path: string, body: unknown = "", query: Record<string, string> = {},
): Promise<ServiceResponse> =>
  s.handle({ method, path, query, body: typeof body === "string" ? body : JSON.stringify(body), headers: {} });

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;

const line = (id: string, date: string, amount: string, over: Record<string, unknown> = {}) => ({
  id, date, account_code: "151", account_name: "Mercury Checking", account_kind: "bank",
  txn_type: "Expense", number: "", name: "Vendor", memo: "", split: "Software", amount_minor: amount, ...over,
});

const OCTOBER = {
  start: "2026-10-01", end: "2026-10-31", imported_at: "2026-10-29T10:00:00Z",
  lines: [
    line("a", "2026-10-02", "-12000"),
    line("b", "2026-10-05", "250000", { txn_type: "Deposit", name: "Northwind" }),
    line("c", "2026-10-20", "-4500", { account_code: "168", account_name: "AMEX", account_kind: "card" }),
  ],
  tie_out: [{ account_code: "151", account_name: "Mercury Checking", opening_minor: "1000000",
    movement_minor: "238000", closing_minor: "1238000" }],
};

function pgBackend(): PostgresBackend {
  const pool = new (newDb().adapters.createPg().Pool)();
  return new PostgresBackend(pool as never, { enforceRls: false });
}

for (const [name, make] of [
  ["in-memory", () => new InMemoryBackend()],
  ["postgres", () => pgBackend()],
] as Array<[string, () => LedgerBackend]>) {
  test(`${name}: an import round-trips with coverage, monthly totals and a tie-out`, async () => {
    const backend = make();
    await backend.migrate();
    const s = new LedgerService(backend, { now: () => NOW });
    assert.equal(obj(await call(s, "GET", "/t/acme/mirror/bank-history"))["covered_from"], "");
    const put = await call(s, "POST", "/t/acme/mirror/bank-history", OCTOBER);
    assert.equal(put.status, 200, JSON.stringify(put.body));
    assert.equal(obj(put)["imported"], 3);

    const got = obj(await call(s, "GET", "/t/acme/mirror/bank-history"));
    assert.equal(got["covered_from"], "2026-10-01");
    assert.equal(got["covered_to"], "2026-10-31");
    assert.equal(got["count"], 3);
    const lines = got["lines"] as Record<string, unknown>[];
    assert.deepEqual(lines.map((l) => l["id"]), ["a", "b", "c"]);
    const periods = got["periods"] as Record<string, unknown>[];
    assert.deepEqual(periods.map((p) => [p["period"], p["account_code"], p["in_minor"], p["out_minor"]]), [
      ["2026-10", "151", "250000", "12000"],
      ["2026-10", "168", "0", "4500"],
    ]);
    const tie = (got["tie_out"] as Record<string, unknown>[])[0]!;
    assert.equal(tie["difference_minor"], "0");
  });

  test(`${name}: re-importing a window replaces it and leaves other months alone`, async () => {
    const backend = make();
    await backend.migrate();
    const s = new LedgerService(backend, { now: () => NOW });
    await call(s, "POST", "/t/acme/mirror/bank-history", {
      start: "2026-09-01", end: "2026-09-30", lines: [line("s1", "2026-09-15", "-100")], tie_out: [],
    });
    await call(s, "POST", "/t/acme/mirror/bank-history", OCTOBER);
    // QuickBooks now shows October differently: one line deleted, one changed.
    await call(s, "POST", "/t/acme/mirror/bank-history", {
      ...OCTOBER, lines: [line("a", "2026-10-02", "-13000")],
    });
    const got = obj(await call(s, "GET", "/t/acme/mirror/bank-history"));
    const lines = got["lines"] as Record<string, unknown>[];
    assert.deepEqual(lines.map((l) => [l["id"], l["amount_minor"]]), [["s1", "-100"], ["a", "-13000"]]);
    assert.equal(got["covered_from"], "2026-09-01");
    assert.equal(got["covered_to"], "2026-10-31");
  });

  test(`${name}: filters by date and account, groups by week, and can leave the lines out`, async () => {
    const backend = make();
    await backend.migrate();
    const s = new LedgerService(backend, { now: () => NOW });
    await call(s, "POST", "/t/acme/mirror/bank-history", OCTOBER);
    const one = obj(await call(s, "GET", "/t/acme/mirror/bank-history", "", { account: "168" }));
    assert.equal(one["count"], 1);
    const early = obj(await call(s, "GET", "/t/acme/mirror/bank-history", "", { to: "2026-10-04" }));
    assert.equal(early["count"], 1);
    const weeks = obj(await call(s, "GET", "/t/acme/mirror/bank-history", "", { group: "week", lines: "0" }));
    assert.equal(weeks["lines"], undefined);
    // Oct 2 2026 is a Friday: its week starts Monday Sep 28. Oct 5 is a Monday.
    assert.deepEqual((weeks["periods"] as Record<string, unknown>[]).map((p) => p["period"]),
      ["2026-09-28", "2026-10-05", "2026-10-19"]);
  });

  test(`${name}: tenants don't see each other's history`, async () => {
    const backend = make();
    await backend.migrate();
    const s = new LedgerService(backend, { now: () => NOW });
    await call(s, "POST", "/t/acme/mirror/bank-history", OCTOBER);
    assert.equal(obj(await call(s, "GET", "/t/bright/mirror/bank-history"))["count"], 0);
  });
}

test("a malformed import is refused and changes nothing", async () => {
  const backend = new InMemoryBackend();
  await backend.migrate();
  const s = new LedgerService(backend, { now: () => NOW });
  await call(s, "POST", "/t/acme/mirror/bank-history", OCTOBER);
  for (const bad of [
    { ...OCTOBER, start: "Oct 1" },
    { ...OCTOBER, start: "2026-11-01" },
    { ...OCTOBER, lines: [line("x", "2026-11-02", "-1")] },
    { ...OCTOBER, lines: [line("x", "2026-10-02", "1.50")] },
    { ...OCTOBER, lines: [line("x", "2026-10-02", "-1"), line("x", "2026-10-03", "-1")] },
    { ...OCTOBER, lines: [line("x", "2026-10-02", "-1", { account_kind: "loan" })] },
  ]) {
    const r = await call(s, "POST", "/t/acme/mirror/bank-history", bad);
    assert.equal(r.status, 400, JSON.stringify(bad));
  }
  assert.equal(obj(await call(s, "GET", "/t/acme/mirror/bank-history"))["count"], 3);
});

test("importing history posts nothing to the books", async () => {
  const backend = new InMemoryBackend();
  await backend.migrate();
  const s = new LedgerService(backend, { now: () => NOW });
  const before = obj(await call(s, "GET", "/t/acme/entries"));
  await call(s, "POST", "/t/acme/mirror/bank-history", OCTOBER);
  assert.deepEqual(obj(await call(s, "GET", "/t/acme/entries")), before);
});
