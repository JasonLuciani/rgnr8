import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Management KPIs / financial ratios.
 *
 * Built from the same balances the statements use, so the health read can't
 * disagree with the books. Verified on a hand-built set of books where every
 * ratio is checkable by hand.
 */

const NOW = "2026-09-01T00:00:00Z";

const call = (
  s: LedgerService, method: string, rawPath: string, body: unknown = "",
): Promise<ServiceResponse> => {
  const [path, qs] = rawPath.split("?");
  const query: Record<string, string> = {};
  if (qs) for (const pair of qs.split("&")) {
    const [k, v] = pair.split("=");
    if (k) query[k] = decodeURIComponent(v ?? "");
  }
  return s.handle({
    method, path: path!, query,
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: {},
  });
};

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;
type Row = Record<string, unknown>;

// Post a simple balanced journal entry by account codes.
async function post(
  s: LedgerService, date: string, key: string, lines: [string, "DEBIT" | "CREDIT", string][],
): Promise<void> {
  const res = await call(s, "POST", "/t/acme/entries", {
    idempotency_key: key, date,
    lines: lines.map(([code, side, amount_minor]) => ({ code, side, amount_minor })),
  });
  assert.equal(res.status, 201, JSON.stringify(res.body));
}

async function ready(): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "CONTRACTOR_TRADES" });
  return s;
}

test("ratios are computed from the books and read out in plain language", async () => {
  const s = await ready();
  // Build a small balance sheet + P&L:
  //  cash 1000 = 200,000 ; inventory 1300 = 100,000 ; equipment 1500 = 300,000
  //  AP 2000 = 100,000 (current) ; long-term debt 2700 = 200,000
  //  owner equity via a capital injection; revenue and costs for a profit
  await post(s, "2026-01-01", "seed-cash", [["1000", "DEBIT", "300000"], ["3000", "CREDIT", "300000"]]);
  await post(s, "2026-01-02", "buy-inv", [["1300", "DEBIT", "100000"], ["1000", "CREDIT", "100000"]]);
  await post(s, "2026-01-03", "buy-equip", [["1500", "DEBIT", "300000"], ["2700", "CREDIT", "200000"], ["3000", "CREDIT", "100000"]]);
  await post(s, "2026-01-04", "ap-bill", [["6300", "DEBIT", "100000"], ["2000", "CREDIT", "100000"]]);
  // revenue 500,000; COGS 200,000; opex already 100,000 rent above
  await post(s, "2026-02-01", "sale", [["1000", "DEBIT", "500000"], ["4000", "CREDIT", "500000"]]);
  await post(s, "2026-02-02", "cogs", [["5000", "DEBIT", "200000"], ["1300", "CREDIT", "100000"], ["1000", "CREDIT", "100000"]]);
  // interest expense 10,000 (for coverage) and depreciation 20,000
  await post(s, "2026-02-03", "interest", [["6950", "DEBIT", "10000"], ["1000", "CREDIT", "10000"]]);
  await post(s, "2026-02-04", "deprec", [["6900", "DEBIT", "20000"], ["1510", "CREDIT", "20000"]]);

  const r = obj(await call(s, "GET", "/t/acme/ratios?as_of=2026-12-31"));
  const inputs = r["inputs"] as Row;

  // net income = 500,000 rev − 200,000 COGS − (100,000 rent + 10,000 int + 20,000 dep) = 170,000
  assert.equal(inputs["revenue_minor"], "500000");
  assert.equal(inputs["cogs_minor"], "200000");
  assert.equal(inputs["net_income_minor"], "170000");
  assert.equal(inputs["interest_expense_minor"], "10000");
  assert.equal(inputs["depreciation_minor"], "20000");

  const liq = r["liquidity"] as Row;
  // current assets: cash + inventory. cash = 300−100+500−100−10 = 590,000 ; inv = 0 (sold) → 590,000
  // current liabilities: AP 100,000
  assert.equal(inputs["current_liabilities_minor"], "100000");
  assert.equal(liq["current_ratio"], "5.90", "590,000 / 100,000");

  const prof = r["profitability"] as Row;
  // gross margin = (500,000 − 200,000)/500,000 = 60%
  assert.equal(prof["gross_margin_pct"], "60.00");
  // net margin = 170,000 / 500,000 = 34%
  assert.equal(prof["net_margin_pct"], "34.00");
  assert.equal(prof["health"], "strong — over 10% of revenue reaches the bottom line");

  const lev = r["leverage"] as Row;
  // interest coverage = EBIT / interest = (170,000 + 10,000) / 10,000 = 18.00
  assert.equal(lev["interest_coverage"], "18.00");
});

test("DSCR uses the debt module's annual service and flags a covenant breach", async () => {
  const s = await ready();
  // profit that yields a modest EBITDA
  await post(s, "2026-01-01", "cap", [["1000", "DEBIT", "1000000"], ["3000", "CREDIT", "1000000"]]);
  await post(s, "2026-02-01", "sale", [["1000", "DEBIT", "120000"], ["4000", "CREDIT", "120000"]]);
  await post(s, "2026-02-02", "opex", [["6300", "DEBIT", "60000"], ["1000", "CREDIT", "60000"]]);
  // EBITDA ≈ net income 60,000 (no interest/dep yet)

  // a loan whose annual service is high relative to EBITDA, with a min-DSCR covenant of 1.25×
  await call(s, "POST", "/t/acme/debt/loans", {
    id: "note", lender: "Bank", kind: "TERM", original_principal_minor: "1000000",
    annual_rate_micro: "0", start_date: "2026-01-15", term_periods: 12, frequency: "MONTHLY",
    min_dscr_micro: "1250000",
  });
  const r = obj(await call(s, "GET", "/t/acme/ratios?as_of=2026-12-31"));
  const lev = r["leverage"] as Row;
  // annual service ≈ 1,000,000 (0% loan over 12 months). EBITDA 60,000 → DSCR ~0.06
  assert.ok(BigInt(String(lev["dscr_micro"])) < 1_000_000n, "coverage well under 1×");
  assert.match(String(lev["health"]), /at risk/);

  const covenants = r["covenants"] as Row[];
  assert.equal(covenants.length, 1);
  assert.equal(covenants[0]!["breached"], true, "DSCR below the 1.25× covenant");
});

test("no revenue and no liabilities yields nulls, not fake ratios", async () => {
  const s = await ready();
  await post(s, "2026-01-01", "cap", [["1000", "DEBIT", "50000"], ["3000", "CREDIT", "50000"]]);
  const r = obj(await call(s, "GET", "/t/acme/ratios"));
  assert.equal((r["liquidity"] as Row)["current_ratio"], null, "no current liabilities → no ratio");
  assert.equal((r["profitability"] as Row)["net_margin_pct"], null, "no revenue → no margin");
});
