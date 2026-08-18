import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Multi-entity consolidation.
 *
 * An owner with three LLCs has three sets of books, because they are three
 * legal entities. Consolidation is a *report* over them, never a fourth set of
 * books — nothing here posts anywhere.
 *
 * The interesting failure is not arithmetic. It is that entity A says it is
 * owed more than entity B says it owes, which every real group has, usually a
 * payment in transit at the period end. A consolidation that silently plugs
 * that difference is a lie with a total at the bottom, so the default is to
 * refuse and show it.
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

/** Two entities: a holding company and a trading one, with the same chart. */
async function ready(): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  for (const t of ["parent", "sub"]) {
    await call(s, "POST", `/t/${t}/accounts/seed`, { category: "PROFESSIONAL_SERVICES" });
    // an intercompany pair: due from affiliates / due to affiliates
    await call(s, "POST", `/t/${t}/accounts`, {
      code: "1900", name: "Due from affiliates", subtype: "OTHER_CURRENT_ASSET",
    });
    await call(s, "POST", `/t/${t}/accounts`, {
      code: "2900", name: "Due to affiliates", subtype: "OTHER_CURRENT_LIABILITY",
    });
  }
  await call(s, "POST", "/t/parent/consolidation/groups", {
    id: "group", name: "Harper Holdings",
    intercompany_codes: ["1900", "2900"],
    members: [
      { tenant_id: "parent", label: "Harper Holdings LLC" },
      { tenant_id: "sub", label: "Harper Build LLC" },
    ],
  });
  return s;
}

const post = (
  s: LedgerService, tenant: string, date: string,
  lines: Array<{ code: string; side: string; amount_minor: string }>, memo = "x",
) => call(s, "POST", `/t/${tenant}/entries`, { date, memo, lines });

/** The parent lends the sub 50,000; the sub records the matching payable. */
async function withIntercompany(s: LedgerService, subAmount = "5000000"): Promise<void> {
  await post(s, "parent", "2026-06-01", [
    { code: "1900", side: "DEBIT", amount_minor: "5000000" },
    { code: "1000", side: "CREDIT", amount_minor: "5000000" },
  ], "Loan to Harper Build");
  await post(s, "sub", "2026-06-01", [
    { code: "1000", side: "DEBIT", amount_minor: subAmount },
    { code: "2900", side: "CREDIT", amount_minor: subAmount },
  ], "Loan from Harper Holdings");
}

const report = (s: LedgerService, query: Record<string, string> = {}) =>
  call(s, "GET", "/t/parent/consolidation/groups/group/report", "", query);

// --- the group ---------------------------------------------------------------

test("a group needs at least two entities", async () => {
  const s = await ready();
  const r = await call(s, "POST", "/t/parent/consolidation/groups", {
    id: "solo", name: "Just me", members: [{ tenant_id: "parent" }],
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /at least two entities/);
});

test("a group only consolidates the entities it names", async () => {
  const s = await ready();
  await call(s, "POST", "/t/other/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  await post(s, "other", "2026-06-01", [
    { code: "1000", side: "DEBIT", amount_minor: "9999999" },
    { code: "3000", side: "CREDIT", amount_minor: "9999999" },
  ]);
  const rows = obj(await report(s))["rows"] as Row[];
  const cash = rows.find((r) => r["account_code"] === "1000");
  assert.equal(cash, undefined, "a tenant nobody listed is nowhere in the report");
});

// --- combining ---------------------------------------------------------------

test("the worksheet shows every entity, the combined total and the consolidated one", async () => {
  const s = await ready();
  await withIntercompany(s);
  await post(s, "parent", "2026-06-10", [
    { code: "1000", side: "DEBIT", amount_minor: "1000000" },
    { code: "4100", side: "CREDIT", amount_minor: "1000000" },
  ], "Management fee from a third party");
  await post(s, "sub", "2026-06-12", [
    { code: "1000", side: "DEBIT", amount_minor: "3000000" },
    { code: "4100", side: "CREDIT", amount_minor: "3000000" },
  ], "Construction income");

  const r = await report(s);
  assert.equal(r.status, 200, JSON.stringify(r.body));
  const body = obj(r);
  assert.equal(body["contract"], "consolidation/1");
  const revenue = (body["rows"] as Row[]).find((x) => x["account_code"] === "4100")!;
  assert.equal((revenue["by_entity"] as Row)["parent"], "-1000000");
  assert.equal((revenue["by_entity"] as Row)["sub"], "-3000000");
  assert.equal(revenue["combined_minor"], "-4000000");
  assert.equal(revenue["consolidated_minor"], "-4000000", "third-party revenue survives");
});

test("intercompany balances are eliminated, and the group's cash is not", async () => {
  const s = await ready();
  await withIntercompany(s);
  const body = obj(await report(s));
  const rows = body["rows"] as Row[];

  const due = rows.find((x) => x["account_code"] === "1900")!;
  assert.equal(due["combined_minor"], "5000000");
  assert.equal(due["consolidated_minor"], "0", "the group cannot owe itself");
  const owed = rows.find((x) => x["account_code"] === "2900")!;
  assert.equal(owed["consolidated_minor"], "0");

  const cash = rows.find((x) => x["account_code"] === "1000")!;
  assert.equal(cash["consolidated_minor"], "0", "the cash moved inside the group");
  assert.equal(body["balanced"], true);
});

test("nothing is posted anywhere by consolidating", async () => {
  const s = await ready();
  await withIntercompany(s);
  await report(s);
  const parent = obj(await call(s, "GET", "/t/parent/entries"))["entries"] as Row[];
  const sub = obj(await call(s, "GET", "/t/sub/entries"))["entries"] as Row[];
  assert.equal(parent.length, 1);
  assert.equal(sub.length, 1);
});

// --- the mismatch ------------------------------------------------------------

test("intercompany that does not agree is refused rather than plugged", async () => {
  const s = await ready();
  await withIntercompany(s, "4958800");     // the sub is 412.00 light
  const body = obj(await report(s));
  assert.equal(body["consolidated"], null, "no total, because a total would be a plug");
  assert.equal(body["mismatch_minor"], "41200");
  assert.match(String(body["refused"]), /payment in transit/);
});

test("asked for anyway, the difference is shown on its own line", async () => {
  const s = await ready();
  await withIntercompany(s, "4958800");
  const body = obj(await report(s, { allow_mismatch: "1" }));
  assert.notEqual(body["consolidated"], null);
  assert.equal(body["mismatch_minor"], "41200");
  const named = (body["eliminations"] as Row[])
    .find((e) => String(e["reason"]).includes("unexplained"));
  assert.ok(named, "it is labelled as what it is");
  assert.equal(body["balanced"], true, "the statements still balance");
});

// --- manual eliminations -----------------------------------------------------

test("a manual elimination has to balance", async () => {
  const s = await ready();
  const r = await call(s, "POST", "/t/parent/consolidation/groups/group/eliminations", {
    description: "Half an entry",
    lines: [{ account_code: "4100", side: "DEBIT", amount_minor: "100000" }],
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /doesn't balance/);
});

test("a manual elimination removes intercompany revenue and the matching cost", async () => {
  const s = await ready();
  // the sub billed the parent 20,000 for services; both sides are in the books
  await post(s, "sub", "2026-06-15", [
    { code: "1900", side: "DEBIT", amount_minor: "2000000" },
    { code: "4100", side: "CREDIT", amount_minor: "2000000" },
  ], "Billed the parent");
  await post(s, "parent", "2026-06-15", [
    { code: "6600", side: "DEBIT", amount_minor: "2000000" },
    { code: "2900", side: "CREDIT", amount_minor: "2000000" },
  ], "Billed by the sub");

  await call(s, "POST", "/t/parent/consolidation/groups/group/eliminations", {
    id: "E1", description: "Intercompany management services",
    lines: [
      { account_code: "4100", side: "DEBIT", amount_minor: "2000000" },
      { account_code: "6600", side: "CREDIT", amount_minor: "2000000" },
    ],
  });

  const rows = obj(await report(s))["rows"] as Row[];
  assert.equal(rows.find((x) => x["account_code"] === "4100")!["consolidated_minor"], "0",
    "the group did not earn anything from itself");
  assert.equal(rows.find((x) => x["account_code"] === "6600")!["consolidated_minor"], "0");
});

// --- statements --------------------------------------------------------------

test("consolidated statements are built from the consolidated balances", async () => {
  const s = await ready();
  await withIntercompany(s);
  await post(s, "parent", "2026-06-10", [
    { code: "1000", side: "DEBIT", amount_minor: "1000000" },
    { code: "4100", side: "CREDIT", amount_minor: "1000000" },
  ]);
  await post(s, "sub", "2026-06-12", [
    { code: "1000", side: "DEBIT", amount_minor: "3000000" },
    { code: "4100", side: "CREDIT", amount_minor: "3000000" },
  ]);
  const r = await call(
    s, "GET", "/t/parent/consolidation/groups/group/statements", "",
    { from: "2026-06-01", to: "2026-06-30" },
  );
  assert.equal(r.status, 200, JSON.stringify(r.body));
  const body = obj(r);
  const income = body["income_statement"] as Row;
  assert.equal(String(income["total_revenue"]), "4000000");
});

test("statements refuse to be produced from books that do not agree", async () => {
  const s = await ready();
  await withIntercompany(s, "4958800");
  const r = await call(
    s, "GET", "/t/parent/consolidation/groups/group/statements", "",
    { from: "2026-06-01", to: "2026-06-30" },
  );
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /Reconcile the entities first/);
});

test("an entity with no books at all is named rather than silently skipped", async () => {
  const s = await ready();
  await call(s, "POST", "/t/parent/consolidation/groups", {
    id: "group2", name: "With a stranger",
    members: [{ tenant_id: "parent" }, { tenant_id: "nobody" }],
  });
  const r = await call(s, "GET", "/t/parent/consolidation/groups/group2/report");
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /nobody has no chart of accounts/);
});

test("one tenant's groups are invisible to another", async () => {
  const s = await ready();
  assert.deepEqual((obj(await call(s, "GET", "/t/sub/consolidation/groups"))["groups"] as Row[]), []);
  assert.equal((await call(s, "GET", "/t/sub/consolidation/groups/group/report")).status, 400);
});
