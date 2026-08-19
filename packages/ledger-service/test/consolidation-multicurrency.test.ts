import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Multi-currency consolidation.
 *
 * A group whose members keep their books in different currencies must translate
 * each entity to the group's base before combining. Assets and liabilities take
 * the current rate; equity takes its historical rate, and the gap between them is
 * the cumulative translation adjustment — booked to equity so the consolidated
 * balance sheet still foots. Turning the option off, or omitting a rate, refuses
 * the report rather than silently combining mismatched currencies.
 */

const NOW = "2026-09-01T00:00:00Z";

const call = (
  s: LedgerService, method: string, path: string, body: unknown = "", query: Record<string, string> = {},
): Promise<ServiceResponse> =>
  s.handle({
    method, path, query,
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: {},
  });

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;
type Row = Record<string, unknown>;

const post = (
  s: LedgerService, tenant: string, date: string,
  lines: Array<{ code: string; side: string; amount_minor: string }>, memo = "x",
) => call(s, "POST", `/t/${tenant}/entries`, { date, memo, lines });

async function twoEntities(): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  for (const t of ["parent", "sub"]) {
    await call(s, "POST", `/t/${t}/accounts/seed`, { category: "PROFESSIONAL_SERVICES" });
  }
  // parent books: cash 100 / equity 100 (USD)
  await post(s, "parent", "2026-06-01", [
    { code: "1000", side: "DEBIT", amount_minor: "10000000" },
    { code: "3000", side: "CREDIT", amount_minor: "10000000" },
  ], "Parent opening");
  // sub books, kept in EUR: cash 100 / equity 100 (EUR)
  await post(s, "sub", "2026-06-01", [
    { code: "1000", side: "DEBIT", amount_minor: "10000000" },
    { code: "3000", side: "CREDIT", amount_minor: "10000000" },
  ], "Sub opening");
  return s;
}

const statements = (s: LedgerService) =>
  call(s, "GET", "/t/parent/consolidation/groups/group/statements", "",
       { from: "2026-06-01", to: "2026-06-30" });

test("a member in another currency is translated at its rate and the sheet balances", async () => {
  const s = await twoEntities();
  // enable multi-currency, base USD
  await call(s, "POST", "/t/parent/settings", { multi_currency_enabled: true, base_currency: "USD" });
  await call(s, "POST", "/t/parent/consolidation/groups", {
    id: "group", name: "Global Holdings",
    members: [
      { tenant_id: "parent", label: "US Parent" },
      // sub is in EUR at 1.10 current, 1.00 historical for equity → a CTA arises
      { tenant_id: "sub", label: "EU Sub", currency: "EUR", rate: "1.10", equity_rate: "1.00" },
    ],
  });
  const r = await statements(s);
  assert.equal(r.status, 200, JSON.stringify(r.body));
  const bs = obj(r)["balance_sheet"] as Row;
  // parent cash 100 + sub cash 100×1.10 = 110 → total assets 210 (USD)
  assert.equal(bs["total_assets"], 21000000, "sub's cash translated at the current rate");
  assert.equal(bs["balanced"], true, "the consolidated sheet foots after the CTA");
  // equity = parent 100 + sub equity 100×1.00 (historical) + CTA 10 = 210
  assert.equal(bs["total_equity"], 21000000);
  const cta = (bs["equity"] as Row[]).find((l) => l["label"] === "Cumulative translation adjustment");
  assert.ok(cta, "the cumulative translation adjustment is on the statement");
  assert.equal(cta!["amount_minor"], 1000000, "CTA = 110 asset − 100 historical equity = 10");
});

test("a single rate translates cleanly with no CTA", async () => {
  const s = await twoEntities();
  await call(s, "POST", "/t/parent/settings", { multi_currency_enabled: true });
  await call(s, "POST", "/t/parent/consolidation/groups", {
    id: "group", name: "Global Holdings",
    members: [
      { tenant_id: "parent", label: "US Parent" },
      { tenant_id: "sub", label: "EU Sub", currency: "EUR", rate: "1.25" },  // one rate for all
    ],
  });
  const bs = obj(await statements(s))["balance_sheet"] as Row;
  assert.equal(bs["total_assets"], 22500000, "100 + 100×1.25 = 225");
  assert.equal(bs["balanced"], true);
  const cta = (bs["equity"] as Row[]).find((l) => l["label"] === "Cumulative translation adjustment");
  assert.equal(cta, undefined, "one rate scales a balanced book, so there is no CTA");
});

test("a foreign member with multi-currency OFF is refused, not silently combined", async () => {
  const s = await twoEntities();
  // multi-currency left off (default)
  await call(s, "POST", "/t/parent/consolidation/groups", {
    id: "group", name: "Global Holdings",
    members: [
      { tenant_id: "parent" },
      { tenant_id: "sub", currency: "EUR", rate: "1.10" },
    ],
  });
  const r = await statements(s);
  assert.equal(r.status, 400, JSON.stringify(r.body));
  assert.match(String(obj(r)["error"]), /multi-currency consolidation is off/);
});

test("a foreign member with no rate configured is refused", async () => {
  const s = await twoEntities();
  await call(s, "POST", "/t/parent/settings", { multi_currency_enabled: true });
  await call(s, "POST", "/t/parent/consolidation/groups", {
    id: "group", name: "Global Holdings",
    members: [
      { tenant_id: "parent" },
      { tenant_id: "sub", currency: "EUR" },   // no rate
    ],
  });
  const r = await statements(s);
  assert.equal(r.status, 400, JSON.stringify(r.body));
  assert.match(String(obj(r)["error"]), /no translation rate/);
});

test("a nonsensical rate is refused at save time", async () => {
  const s = await twoEntities();
  const r = await call(s, "POST", "/t/parent/consolidation/groups", {
    id: "group", name: "Global Holdings",
    members: [
      { tenant_id: "parent" },
      { tenant_id: "sub", currency: "EUR", rate: "abc" },
    ],
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /rate/);
});
