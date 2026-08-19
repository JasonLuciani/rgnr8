import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Debt — loans, amortization, and the auto-split payment.
 *
 * The wedge QuickBooks Online and Xero don't build: a loan payment splits itself
 * into principal and interest from the live balance, posts a balanced entry, and
 * the amortization schedule pays off to exactly zero. Extra principal and early
 * payoff fall out for free because interest is `balance × periodic rate`, never a
 * frozen schedule row.
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

async function ready(): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "CONTRACTOR_TRADES" });
  return s;
}

const balances = async (s: LedgerService): Promise<Record<string, bigint>> => {
  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  const out: Record<string, bigint> = {};
  for (const row of tb["rows"] as Row[]) {
    out[String(row["code"])] =
      BigInt(String(row["debit_minor"])) - BigInt(String(row["credit_minor"]));
  }
  return out;
};

// A $10,000 term loan at 6.00% for 12 monthly payments, proceeds into checking.
const openLoan = (s: LedgerService, extra: Record<string, unknown> = {}) =>
  call(s, "POST", "/t/acme/debt/loans", {
    id: "truck", lender: "First Bank", kind: "TERM",
    original_principal_minor: "1000000", annual_rate_micro: "60000",
    start_date: "2026-01-15", term_periods: 12, frequency: "MONTHLY",
    proceeds_to_code: "1000", ...extra,
  });

test("opening a loan posts the proceeds — cash in, liability up", async () => {
  const s = await ready();
  const opened = await openLoan(s);
  assert.equal(opened.status, 201, JSON.stringify(opened.body));
  const bal = await balances(s);
  assert.equal(bal["1000"], 1000000n, "checking up by the loan amount");
  assert.equal(bal["2700"], -1000000n, "Long-Term Debt is a credit balance of the loan");
});

test("the amortization schedule pays off to exactly zero", async () => {
  const s = await ready();
  await openLoan(s);
  const detail = obj(await call(s, "GET", "/t/acme/debt/loans/truck"));
  const schedule = detail["schedule"] as Row[];
  assert.equal(schedule.length, 12);
  // level payment $860.66; first period interest is 10000 × 0.5% = $50.00
  assert.equal(schedule[0]!["payment_minor"], "86066");
  assert.equal(schedule[0]!["interest_minor"], "5000");
  assert.equal(schedule[0]!["principal_minor"], "81066");
  // principal across the schedule sums to exactly the loan; last balance is zero
  const sumP = schedule.reduce((a, r) => a + BigInt(String(r["principal_minor"])), 0n);
  assert.equal(sumP, 1000000n, "principal repayments sum to the loan, no dust");
  assert.equal(schedule[11]!["balance_minor"], "0");
  // due dates advance one month from the start date
  assert.equal(schedule[0]!["due_date"], "2026-02-15");
  assert.equal(schedule[11]!["due_date"], "2027-01-15");
});

test("a payment splits itself into principal and interest and posts a balanced entry", async () => {
  const s = await ready();
  await openLoan(s);
  const paid = await call(s, "POST", "/t/acme/debt/payments", {
    loan_id: "truck", date: "2026-02-15", amount_minor: "86066", paid_from_code: "1000",
  });
  assert.equal(paid.status, 201, JSON.stringify(paid.body));
  assert.equal(obj(paid)["interest_minor"], "5000", "interest = balance × 0.5%");
  assert.equal(obj(paid)["principal_minor"], "81066", "the rest reduces principal");
  assert.equal((obj(paid)["loan"] as Row)["current_principal_minor"], "918934");

  const bal = await balances(s);
  assert.equal(bal["2700"], -918934n, "liability down by the principal portion only");
  assert.equal(bal["6950"], 5000n, "Interest Expense carries the interest");
  assert.equal(bal["1000"], 1000000n - 86066n, "cash down by the whole payment");
});

test("extra principal just lowers next period's interest — no schedule to catch up", async () => {
  const s = await ready();
  await openLoan(s);
  // pay $1,000 — $50 interest, $950 principal (vs the scheduled $810.66)
  const paid = await call(s, "POST", "/t/acme/debt/payments", {
    loan_id: "truck", date: "2026-02-15", amount_minor: "100000", paid_from_code: "1000",
  });
  assert.equal(obj(paid)["interest_minor"], "5000");
  assert.equal(obj(paid)["principal_minor"], "95000");
  assert.equal((obj(paid)["loan"] as Row)["current_principal_minor"], "905000");
  // next month interest is now 905000 × 0.5% = 4525, lower than the schedule assumed
  const next = await call(s, "POST", "/t/acme/debt/payments", {
    loan_id: "truck", date: "2026-03-15", amount_minor: "86066", paid_from_code: "1000",
  });
  assert.equal(obj(next)["interest_minor"], "4525");
});

test("an overpayment pays the loan off and never books negative principal", async () => {
  const s = await ready();
  await openLoan(s);
  // throw the whole balance plus a cushion at it
  const paid = await call(s, "POST", "/t/acme/debt/payments", {
    loan_id: "truck", date: "2026-02-15", amount_minor: "2000000", paid_from_code: "1000",
  });
  assert.equal(obj(paid)["interest_minor"], "5000");
  assert.equal(obj(paid)["principal_minor"], "1000000", "principal capped at the balance");
  assert.equal((obj(paid)["loan"] as Row)["current_principal_minor"], "0", "paid off");
  assert.equal((await balances(s))["2700"], 0n, "liability cleared");
});

test("a duplicate payment id posts once", async () => {
  const s = await ready();
  await openLoan(s);
  const body = { id: "feb", loan_id: "truck", date: "2026-02-15", amount_minor: "86066" };
  await call(s, "POST", "/t/acme/debt/payments", body);
  await call(s, "POST", "/t/acme/debt/payments", body);
  const detail = obj(await call(s, "GET", "/t/acme/debt/loans/truck"));
  assert.equal((detail["payments"] as Row[]).length, 1, "the retry did not double-post");
  assert.equal((detail["loan"] as Row)["current_principal_minor"], "918934");
});

test("a line of credit is drawn and repaid; a term loan refuses a draw", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/debt/loans", {
    id: "loc", lender: "Credit Union", kind: "LINE_OF_CREDIT",
    original_principal_minor: "0", annual_rate_micro: "90000",
    start_date: "2026-01-01", frequency: "MONTHLY", liability_account_code: "2700",
  });
  const drawn = await call(s, "POST", "/t/acme/debt/draws", {
    loan_id: "loc", date: "2026-02-01", amount_minor: "500000", deposit_to_code: "1000",
  });
  assert.equal(drawn.status, 201, JSON.stringify(drawn.body));
  assert.equal((obj(drawn)["loan"] as Row)["current_principal_minor"], "500000");
  assert.equal((await balances(s))["1000"], 500000n);

  await openLoan(s); // a term loan
  const badDraw = await call(s, "POST", "/t/acme/debt/draws", {
    loan_id: "truck", date: "2026-02-01", amount_minor: "1000",
  });
  assert.equal(badDraw.status, 400);
  assert.match(String(obj(badDraw)["error"]), /line of credit or card/);
});

test("the dashboard totals debt, the blended rate, and monthly service", async () => {
  const s = await ready();
  await openLoan(s); // $10k @ 6%, ~$860.66/mo
  await call(s, "POST", "/t/acme/debt/loans", {
    id: "loc", lender: "CU", kind: "LINE_OF_CREDIT", original_principal_minor: "0",
    annual_rate_micro: "120000", start_date: "2026-01-01", frequency: "MONTHLY",
  });
  await call(s, "POST", "/t/acme/debt/draws", {
    loan_id: "loc", date: "2026-01-05", amount_minor: "1000000", deposit_to_code: "1000",
  });
  const dash = obj(await call(s, "GET", "/t/acme/debt?as_of=2026-02-01"));
  const totals = dash["totals"] as Row;
  assert.equal(totals["total_principal_minor"], "2000000", "$10k term + $10k drawn");
  // blended rate: (1,000,000×60000 + 1,000,000×120000) / 2,000,000 = 90000 (9.00%)
  assert.equal(totals["weighted_avg_rate_micro"], "90000");
  assert.equal(totals["loan_count"], 2);
  // the term loan's next payment shows on the dashboard
  const truck = (dash["loans"] as Row[]).find((l) => l["id"] === "truck")!;
  assert.equal(truck["next_payment_date"], "2026-02-15");
  assert.equal(truck["payoff_date"], "2027-01-15");
});

test("payoff planning shows the periods and interest an extra payment saves", async () => {
  const s = await ready();
  await openLoan(s);
  const plan = obj(await call(
    s, "GET", "/t/acme/debt/loans/truck/payoff?extra_per_period_minor=20000"));
  // a strict equal-payment run finishes in the loan's term give or take a tiny
  // trailing payment (the schedule trues its final payment up; a fixed payment
  // can leave a small final stub) — 12 or 13 periods
  assert.ok([12, 13].includes(Number(plan["base_periods"])), `base was ${plan["base_periods"]}`);
  assert.ok(Number(plan["accelerated_periods"]) < Number(plan["base_periods"]), "an extra $200/mo finishes sooner");
  assert.ok(Number(plan["periods_saved"]) > 0);
  assert.ok(BigInt(String(plan["interest_saved_minor"])) > 0n, "and saves interest");
});
