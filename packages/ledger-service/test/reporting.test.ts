import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * General ledger detail and budget vs actual.
 *
 * The GL is the report you reach for when the trial balance surprises you, so
 * what matters is that it reconciles: opening plus the window's movement equals
 * closing, every time.
 *
 * Budget vs actual turns on one word — *favourable*. Spending less than planned
 * is good; earning less is not. Getting that backwards makes a report that
 * cheerfully congratulates a business on missing its revenue target.
 */

const NOW = "2026-08-20T00:00:00Z";

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

async function post(
  s: LedgerService, date: string, memo: string, debit: string, credit: string, minor: string,
): Promise<void> {
  const r = await call(s, "POST", "/t/acme/entries", {
    date, memo,
    lines: [
      { code: debit, side: "DEBIT", amount_minor: minor },
      { code: credit, side: "CREDIT", amount_minor: minor },
    ],
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
}

/** July and August activity, so date windows have something to bite on. */
async function ready(): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  await post(s, "2026-07-20", "July retainer", "1000", "4100", "400000");
  await post(s, "2026-08-03", "August retainer", "1000", "4100", "1200000");
  await post(s, "2026-08-09", "Studio rent", "6300", "1000", "350000");
  await post(s, "2026-08-21", "Software", "6500", "1000", "24900");
  return s;
}

const accounts = (r: ServiceResponse): Row[] => obj(r)["accounts"] as Row[];
const find = (r: ServiceResponse, code: string): Row =>
  accounts(r).find((a) => a["code"] === code)!;

// --- general ledger ----------------------------------------------------------

test("the general ledger shows every posting, per account, with a running balance", async () => {
  const s = await ready();
  const r = await call(s, "GET", "/t/acme/gl");
  assert.equal(r.status, 200);
  assert.equal(obj(r)["contract"], "gl-detail/1");

  const cash = find(r, "1000");
  assert.equal(cash["rows"] instanceof Array, true);
  const rows = cash["rows"] as Row[];
  assert.equal(rows.length, 4);
  assert.equal(rows[0]!["memo"], "July retainer");
  // the running balance walks: 4,000 → 16,000 → 12,500 → 12,251
  assert.deepEqual(
    rows.map((x) => x["balance_minor"]),
    ["400000", "1600000", "1250000", "1225100"],
  );
  assert.equal(cash["closing_minor"], "1225100");
});

test("opening plus the window's movement equals closing, on every account", async () => {
  const s = await ready();
  const r = await call(s, "GET", "/t/acme/gl", "", { from: "2026-08-01", to: "2026-08-31" });
  for (const a of accounts(r)) {
    const opening = BigInt(String(a["opening_minor"]));
    const closing = BigInt(String(a["closing_minor"]));
    const movement = BigInt(String(a["total_debit_minor"])) - BigInt(String(a["total_credit_minor"]));
    assert.equal(opening + movement, closing, `${String(a["code"])} does not reconcile`);
  }
});

test("a date window excludes what came before it but carries it as opening", async () => {
  const s = await ready();
  const r = await call(s, "GET", "/t/acme/gl", "", { from: "2026-08-01", to: "2026-08-31" });
  const cash = find(r, "1000");
  assert.equal((cash["rows"] as Row[]).length, 3, "July is out of the window");
  assert.equal(cash["opening_minor"], "400000", "but July is still the opening balance");
  assert.equal(cash["closing_minor"], "1225100");
});

test("the ledger balances: total debits equal total credits", async () => {
  const s = await ready();
  const r = await call(s, "GET", "/t/acme/gl");
  assert.equal(obj(r)["total_debit_minor"], obj(r)["total_credit_minor"]);
});

test("it can be narrowed to the accounts you actually care about", async () => {
  const s = await ready();
  const r = await call(s, "GET", "/t/acme/gl", "", { codes: "6300,6500" });
  assert.deepEqual(accounts(r).map((a) => a["code"]).sort(), ["6300", "6500"]);
  assert.equal((await call(s, "GET", "/t/acme/gl", "", { codes: "9999" })).status, 400);
  assert.equal((await call(s, "GET", "/t/acme/gl", "", { from: "August" })).status, 400);
});

test("accounts with no activity are left out rather than padded with zeroes", async () => {
  const s = await ready();
  const r = await call(s, "GET", "/t/acme/gl");
  assert.equal(accounts(r).length, 4, "only the four accounts that were touched");
  assert.ok(!accounts(r).some((a) => a["code"] === "1010"));
});

// --- budget vs actual --------------------------------------------------------

const BUDGET = {
  period: "2026-08",
  lines: [
    { account_code: "4100", amount_minor: "1500000" },   // hoped for 15,000
    { account_code: "6300", amount_minor: "350000" },    // rent, known exactly
    { account_code: "6500", amount_minor: "40000" },     // budgeted 400 for software
  ],
};

test("under-spending is favourable and under-earning is not", async () => {
  const s = await ready();
  assert.equal((await call(s, "POST", "/t/acme/budget", BUDGET)).status, 201);
  const r = await call(s, "GET", "/t/acme/budget/2026-08");
  assert.equal(r.status, 200);
  assert.equal(obj(r)["contract"], "budget-vs-actual/1");

  const lines = obj(r)["lines"] as Row[];
  const by = (code: string): Row => lines.find((l) => l["code"] === code)!;

  // revenue: budgeted 15,000, earned 12,000 — that is a MISS
  assert.equal(by("4100")["budget_minor"], "1500000");
  assert.equal(by("4100")["actual_minor"], "1200000");
  assert.equal(by("4100")["favorable"], false);

  // software: budgeted 400, spent 249 — that is a WIN
  assert.equal(by("6500")["actual_minor"], "24900");
  assert.equal(by("6500")["favorable"], true);

  // rent: exactly on plan
  assert.equal(by("6300")["variance_minor"], "0");
});

test("only income and expense accounts appear — a bank balance was never planned", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/budget", BUDGET);
  const lines = obj(await call(s, "GET", "/t/acme/budget/2026-08"))["lines"] as Row[];
  const codes = lines.map((l) => String(l["code"]));
  assert.ok(!codes.includes("1000"), "cash is not a budget line");
  assert.ok(codes.includes("4100") && codes.includes("6300"));
});

test("the budget covers the period only, not the balance carried into it", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/budget", BUDGET);
  const r = await call(s, "GET", "/t/acme/budget/2026-08");
  const revenue = (obj(r)["lines"] as Row[]).find((l) => l["code"] === "4100")!;
  // July's 4,000 is deliberately absent — August earned 12,000
  assert.equal(revenue["actual_minor"], "1200000");
});

test("a budget can be revised, and the newest figure wins", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/budget", BUDGET);
  await call(s, "POST", "/t/acme/budget", {
    period: "2026-08", lines: [{ account_code: "4100", amount_minor: "1200000" }],
  });
  const r = await call(s, "GET", "/t/acme/budget/2026-08");
  const revenue = (obj(r)["lines"] as Row[]).find((l) => l["code"] === "4100")!;
  assert.equal(revenue["budget_minor"], "1200000");
  assert.equal(revenue["variance_minor"], "0", "we hit the revised number exactly");
  // the other lines are untouched by a partial revision
  assert.equal((obj(r)["lines"] as Row[]).length >= 3, true);
});

test("a budget is refused on a bad period, a bad amount, or an unknown account", async () => {
  const s = await ready();
  const bad = async (body: unknown): Promise<number> =>
    (await call(s, "POST", "/t/acme/budget", body)).status;
  assert.equal(await bad({ period: "August", lines: BUDGET.lines }), 400);
  assert.equal(await bad({ period: "2026-08", lines: [] }), 400);
  assert.equal(await bad({ period: "2026-08", lines: [{ account_code: "9999", amount_minor: "1" }] }), 400);
  assert.equal(await bad({ period: "2026-08", lines: [{ account_code: "4100", amount_minor: "1.50" }] }), 400);
  assert.equal((await call(s, "GET", "/t/acme/budget/nope")).status, 400);
});

test("a period with no budget reports the actuals rather than failing", async () => {
  const s = await ready();
  const r = await call(s, "GET", "/t/acme/budget/2026-08");
  assert.equal(r.status, 200);
  assert.equal(obj(r)["budgeted_accounts"], 0);
});

test("a budget can be cleared", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/budget", BUDGET);
  assert.equal((await call(s, "DELETE", "/t/acme/budget/2026-08")).status, 200);
  assert.equal(obj(await call(s, "GET", "/t/acme/budget/2026-08"))["budgeted_accounts"], 0);
});

// --- isolation ---------------------------------------------------------------

test("one tenant's ledger and budget are invisible to another", async () => {
  const s = await ready();
  await call(s, "POST", "/t/beta/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  await call(s, "POST", "/t/acme/budget", BUDGET);

  assert.equal((accounts(await call(s, "GET", "/t/beta/gl"))).length, 0);
  assert.equal(obj(await call(s, "GET", "/t/beta/budget/2026-08"))["budgeted_accounts"], 0);
});
